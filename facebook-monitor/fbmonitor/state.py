"""Seen-item tracking.

A monitor that reports the same twenty comments every run is worse than no
monitor, so each run records the IDs it has already shown and reports only
what is new. State is a single JSON file; it is written atomically so an
interrupted run cannot corrupt it into an unreadable state that would cause
the next run to re-report everything.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Per account+source. Comfortably more than any single run will see, while
# keeping the file small enough to rewrite cheaply.
MAX_SEEN_IDS = 2000


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "accounts": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            # A corrupt state file must not wedge the tool. Start clean and
            # accept one noisy run rather than failing every run from here on.
            log.warning("state file %s unreadable (%s); starting fresh",
                        self.path, exc)
            return {"version": 1, "accounts": {}}
        if not isinstance(data, dict) or "accounts" not in data:
            log.warning("state file %s has unexpected shape; starting fresh",
                        self.path)
            return {"version": 1, "accounts": {}}
        return data

    # -- queries ---------------------------------------------------------

    def _bucket(self, account: str, kind: str) -> dict:
        accounts = self._data.setdefault("accounts", {})
        sources = accounts.setdefault(account, {}).setdefault("sources", {})
        return sources.setdefault(kind, {"seen_ids": [], "last_run": None})

    def is_new(self, account: str, kind: str, item_id: str) -> bool:
        return item_id not in set(self._bucket(account, kind)["seen_ids"])

    def filter_new(self, account: str, kind: str, items: list) -> list:
        """Return only items not seen before, without recording them yet.

        Recording is a separate step so that a run which dies partway
        through does not mark items as seen that were never shown.
        """
        seen = set(self._bucket(account, kind)["seen_ids"])
        return [item for item in items if item.id not in seen]

    def last_run(self, account: str, kind: str) -> datetime | None:
        raw = self._bucket(account, kind).get("last_run")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    # -- updates ---------------------------------------------------------

    def record(self, account: str, kind: str, item_ids: list[str]) -> None:
        bucket = self._bucket(account, kind)
        seen = bucket["seen_ids"]
        known = set(seen)
        for item_id in item_ids:
            if item_id not in known:
                seen.append(item_id)
                known.add(item_id)
        # Keep the newest IDs; the oldest are long past being re-reported.
        if len(seen) > MAX_SEEN_IDS:
            bucket["seen_ids"] = seen[-MAX_SEEN_IDS:]
        bucket["last_run"] = datetime.now(timezone.utc).isoformat()

    def mark_run(self, account: str, kind: str) -> None:
        """Stamp a source that ran cleanly but produced nothing new."""
        self._bucket(account, kind)["last_run"] = (
            datetime.now(timezone.utc).isoformat())

    # -- what has already been announced --------------------------------

    def reported_problems(self) -> str:
        return self._data.get("reported_problems", "")

    def set_reported_problems(self, signature: str) -> None:
        self._data["reported_problems"] = signature

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: the reader either sees the old file or the new
        # one, never a half-written mixture.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
