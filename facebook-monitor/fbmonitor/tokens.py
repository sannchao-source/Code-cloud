"""Warn before a token expires, rather than after.

The ads user token lasts about 60 days. When it dies, ad comments simply
stop being reported -- the run still succeeds, the channel stays quiet, and
quiet is what this tool uses to mean "nothing to worry about". A silent
expiry is therefore the most dangerous failure it has: it looks exactly
like good news.

So the token is asked how long it has left, and the answer is escalated
into the alert channel while there is still time to act.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .graph import GraphClient, GraphError

log = logging.getLogger(__name__)

# Warn at each of these thresholds as the deadline approaches. Bucketing
# rather than warning daily keeps it from becoming background noise, while
# still growing more insistent.
WARN_DAYS = (14, 7, 3, 1)

# One call a day is plenty for something measured in weeks.
CHECK_EVERY_HOURS = 24


def days_until_expiry(client: GraphClient, token: str) -> int | None:
    """Days until this token expires, or None if it never expires.

    Raises GraphError if the token cannot be inspected at all.
    """
    payload = client.get("debug_token", {"input_token": token})
    data = payload.get("data") or {}

    expires_at = data.get("expires_at")
    # Graph reports a non-expiring token as 0 -- which is also what an
    # absent field looks like, so both are treated as "no expiry".
    if not expires_at:
        return None

    expiry = datetime.fromtimestamp(int(expires_at), tz=timezone.utc)
    remaining = expiry - datetime.now(timezone.utc)
    # Round down, so "0 days" means today rather than "some hours left".
    return max(0, remaining.days)


def warning_for(days: int | None, *, label: str) -> str | None:
    """The message to raise, or None if the deadline is not close yet."""
    if days is None:
        return None
    threshold = next((d for d in WARN_DAYS if days <= d), None)
    if threshold is None:
        return None

    if days <= 0:
        return (f"the {label} has expired -- ad comments are no longer being "
                "read. Renew it: see README.md step 4")
    plural = "" if days == 1 else "s"
    return (f"the {label} expires in {days} day{plural}. When it does, ad "
            "comments stop being reported and nothing looks wrong -- renew "
            "it: see README.md step 4")


def check(client: GraphClient, token: str, *, label: str) -> str | None:
    """Inspect a token and return a warning if it is nearly out of time."""
    try:
        days = days_until_expiry(client, token)
    except GraphError as exc:
        # Never let this break a run: the collectors matter more than the
        # advance warning, and a failure here is itself worth surfacing.
        log.debug("could not inspect %s: %s", label, exc)
        return f"could not check when the {label} expires ({exc})"
    if days is None:
        log.debug("%s does not expire", label)
        return None
    return warning_for(days, label=label)
