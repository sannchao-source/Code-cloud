"""Flag the items a human should look at first.

This is deliberately a crude keyword heuristic, not a sentiment model. It
exists to *order* the digest, never to filter it -- every item collected is
always shown, because the cost of quietly hiding a real complaint is far
higher than the cost of showing an extra line. Treat a flag as "look here
first", and its absence as no information at all.
"""

from __future__ import annotations

from .models import KIND_AD_COMMENT, Item

# Complaints that damage a business in front of an audience. Substring
# matched against lowercased text, so short entries risk false positives --
# keep them distinctive.
NEGATIVE_PHRASES = (
    "scam", "rip off", "ripoff", "rip-off", "overpriced", "waste of money",
    "terrible", "awful", "horrible", "worst", "disgusting", "filthy", "dirty",
    "rude", "unprofessional", "disappointed", "disappointing", "never again",
    "avoid", "do not go", "don't go", "dont go", "wouldn't recommend",
    "would not recommend", "not worth", "butchered", "ruined", "complaint",
    "refund", "unhygienic", "shocking", "joke", "clueless",
)

# A question left hanging under a live ad is a lead walking away.
QUESTION_MARKERS = (
    "?", "how much", "how long", "do you", "can i", "are you open",
    "what time", "price", "cost", "book",
)


# Severity drives the ordering. A binary flag was not enough: flagging
# every ad comment made the flag meaningless and pushed the one actual
# complaint to the bottom of the list, which is precisely backwards.
SEVERITY_NONE = 0
SEVERITY_LEAD = 1
SEVERITY_COMPLAINT = 2


def assess(item: Item) -> tuple[int, str]:
    """Return (severity, reason)."""
    text = (item.text or "").lower()
    on_ad = item.kind == KIND_AD_COMMENT

    hit = next((p for p in NEGATIVE_PHRASES if p in text), None)
    if hit:
        where = "on a live ad" if on_ad else "posted publicly"
        return SEVERITY_COMPLAINT, f'possible complaint {where} (matched "{hit}")'

    if on_ad and any(marker in text for marker in QUESTION_MARKERS):
        return SEVERITY_LEAD, "question on a live ad -- an unanswered lead"

    if text.strip().endswith("?"):
        return SEVERITY_LEAD, "unanswered question"

    return SEVERITY_NONE, ""


def apply(items: list[Item]) -> list[Item]:
    """Annotate items in place and return them."""
    for item in items:
        severity, reason = assess(item)
        item.severity = severity
        item.attention_reason = reason
    return items
