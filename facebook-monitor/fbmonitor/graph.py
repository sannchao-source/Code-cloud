"""Read-only client for the Meta Graph API.

This module deliberately exposes no way to write. There is a single ``get``
method; nothing here can create a comment, send a message, or mutate any
object on a Page. That is the structural guarantee behind the monitor's
"notify only" contract -- it is enforced by the absence of code rather than
by a runtime flag someone can flip.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger(__name__)

DEFAULT_API_VERSION = "v26.0"
GRAPH_HOST = "graph.facebook.com"

# Graph returns these when a token lacks a scope or a Page has a feature
# switched off. They are worth surfacing to the operator rather than
# crashing the whole run, because one missing scope should not stop the
# other six collectors from reporting.
_PERMISSION_CODES = {10, 200, 230, 803}


class GraphError(RuntimeError):
    """A Graph API call failed."""

    def __init__(self, message: str, *, code: int | None = None,
                 subcode: int | None = None, path: str = ""):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.path = path

    @property
    def is_permission_error(self) -> bool:
        return self.code in _PERMISSION_CODES


class GraphClient:
    """Minimal, GET-only Graph API client with paging and retry."""

    def __init__(
        self,
        token: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        timeout: int = 30,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        if not token:
            raise ValueError("a Graph API access token is required")
        self._token = token
        self.api_version = api_version
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or requests.Session()

    # -- the only request path in this codebase --------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a Graph edge or node. ``path`` is relative, e.g. ``"me/feed"``."""
        url = f"https://{GRAPH_HOST}/{self.api_version}/{path.lstrip('/')}"
        return self._request(url, dict(params or {}))

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_pages: int = 10,
    ) -> Iterator[dict[str, Any]]:
        """Yield each item across cursor-paged results, newest page first."""
        params = dict(params or {})
        payload = self.get(path, params)
        pages = 0
        while True:
            for item in payload.get("data", []):
                yield item
            pages += 1
            if pages >= max_pages:
                log.debug("stopping paging for %s at max_pages=%d", path, max_pages)
                return
            nxt = (payload.get("paging") or {}).get("next")
            if not nxt:
                return
            # Follow Graph's own cursor URL, but never off-host: a "next"
            # pointing anywhere else would leak the access token.
            host = urlparse(nxt).hostname or ""
            if host != GRAPH_HOST:
                log.warning("refusing to follow off-host paging cursor: %s", host)
                return
            payload = self._request(nxt, None)

    # -- internals -------------------------------------------------------

    def _request(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        # Graph's own paging cursors arrive with the token already in the
        # query string. Splitting the URL and rebuilding the params means
        # exactly one access_token goes out -- appending a second copy to a
        # cursor URL makes Graph reject the call.
        base, _, existing = url.partition("?")
        send = {k: v[0] for k, v in parse_qs(existing).items()} if existing else {}
        send.update(params or {})
        send["access_token"] = self._token
        # Every error below reports `base`, which by construction has no
        # query string, so a token can never reach a log or a traceback.

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._session.get(base, params=send, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                self._backoff(attempt, "network error: %s" % exc)
                continue

            if resp.status_code == 200:
                return resp.json()

            # 429 is a rate limit; 500-class is transient. Retry those.
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = GraphError(
                    f"HTTP {resp.status_code} from Graph", path=base)
                self._backoff(attempt, f"HTTP {resp.status_code}")
                continue

            raise self._error_from(resp, base)

        raise GraphError(
            f"giving up after {self.max_retries} attempts: {last_error}", path=base)

    def _error_from(self, resp: requests.Response, url: str) -> GraphError:
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        message = err.get("message") or f"HTTP {resp.status_code} from Graph"
        return GraphError(
            message,
            code=err.get("code"),
            subcode=err.get("error_subcode"),
            path=url,
        )

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        delay = 2 ** attempt
        log.debug("retrying in %ss (%s)", delay, reason)
        time.sleep(delay)
