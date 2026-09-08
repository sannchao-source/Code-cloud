"""Normalised shapes shared by every collector.

Graph returns a different JSON shape for a comment, a Messenger message and
a Page recommendation. Collectors flatten all of them into ``Item`` so the
digest can sort and group without knowing where anything came from.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# Source kinds, in the order a digest presents them. Direct messages and
# reviews lead because they are the ones that go unanswered longest.
KIND_MESSENGER_DM = "messenger_dm"
KIND_INSTAGRAM_DM = "instagram_dm"
KIND_REVIEW = "review"
KIND_VISITOR_POST = "visitor_post"
KIND_PAGE_COMMENT = "page_comment"
KIND_AD_COMMENT = "ad_comment"
KIND_INSTAGRAM_COMMENT = "instagram_comment"

# Ad comments lead. Everything else is seen by whoever happens to visit;
# a comment under a running ad is served to every future person the ad
# reaches, so it costs money for as long as it stands.
KIND_ORDER = [
    KIND_AD_COMMENT,
    KIND_MESSENGER_DM,
    KIND_INSTAGRAM_DM,
    KIND_REVIEW,
    KIND_VISITOR_POST,
    KIND_PAGE_COMMENT,
    KIND_INSTAGRAM_COMMENT,
]

KIND_LABELS = {
    KIND_MESSENGER_DM: "Messenger DMs",
    KIND_INSTAGRAM_DM: "Instagram DMs",
    KIND_REVIEW: "Page recommendations",
    KIND_VISITOR_POST: "Visitor posts",
    KIND_AD_COMMENT: "Comments on ads",
    KIND_PAGE_COMMENT: "Comments on Page posts",
    KIND_INSTAGRAM_COMMENT: "Comments on Instagram posts",
}


@dataclass
class Item:
    """One thing a human may need to look at."""

    kind: str
    id: str
    account: str
    created_time: datetime | None
    author: str = "unknown"
    text: str = ""
    permalink: str = ""
    # Where this turned up -- the post it is a comment on, the conversation
    # a message belongs to. Gives the digest a line of context.
    context: str = ""
    # Set by fbmonitor.triage. A hint for ordering the digest, never a
    # filter -- see that module.
    severity: int = 0
    attention_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_attention(self) -> bool:
        return self.severity > 0

    @property
    def sort_key(self) -> datetime:
        return self.created_time or datetime.min.replace(tzinfo=timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out["created_time"] = (
            self.created_time.isoformat() if self.created_time else None
        )
        out["needs_attention"] = self.needs_attention
        return out


@dataclass
class CollectionResult:
    """What one collector produced, including how it failed."""

    kind: str
    account: str
    items: list[Item] = field(default_factory=list)
    # Set when the source could not be read at all -- a missing scope, a
    # Page with reviews turned off. Reported to the operator rather than
    # silently yielding zero items, because "no new comments" and "we
    # cannot see comments" mean very different things.
    error: str | None = None
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_time(raw: str | int | None) -> datetime | None:
    """Parse the several time formats Graph uses across its edges."""
    if raw is None or raw == "":
        return None
    # Messenger message timestamps come back as epoch milliseconds.
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000.0, tz=timezone.utc)
    if isinstance(raw, str) and raw.isdigit():
        return datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc)
    text = str(raw).strip()
    # Graph's ISO variant is like 2026-09-08T10:15:00+0000.
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
