"""Collectors for Instagram professional accounts."""

from __future__ import annotations

import logging

from ..graph import GraphClient, GraphError
from ..models import (
    KIND_INSTAGRAM_COMMENT,
    KIND_INSTAGRAM_DM,
    CollectionResult,
    Item,
    parse_time,
)
from .facebook import _collect_conversations, _describe, _summarise

log = logging.getLogger(__name__)

MEDIA_LIMIT = 25
COMMENT_LIMIT = 50


def collect_instagram_comments(client: GraphClient, account, **_) -> CollectionResult:
    """Comments on the account's own media."""
    result = CollectionResult(kind=KIND_INSTAGRAM_COMMENT, account=account.slug)
    ig_id = account.instagram_user_id
    if not ig_id:
        result.skipped_reason = "no instagram_user_id configured"
        return result

    fields = (
        "id,caption,permalink,timestamp,"
        f"comments.limit({COMMENT_LIMIT})"
        "{id,text,username,timestamp,parent_id}"
    )
    try:
        media = list(client.paginate(
            f"{ig_id}/media",
            {"fields": fields, "limit": MEDIA_LIMIT},
            max_pages=2,
        ))
    except GraphError as exc:
        if exc.is_permission_error:
            result.skipped_reason = (
                "Instagram media unavailable -- the token lacks instagram_basic "
                "or instagram_manage_comments")
            return result
        result.error = _describe(exc, "reading Instagram media")
        return result

    for post in media:
        caption = _summarise(post.get("caption"), "(no caption)")
        for comment in (post.get("comments") or {}).get("data", []):
            result.items.append(Item(
                kind=KIND_INSTAGRAM_COMMENT,
                id=str(comment.get("id")),
                account=account.slug,
                created_time=parse_time(comment.get("timestamp")),
                # Instagram gives a handle rather than a display name.
                author=f"@{comment['username']}" if comment.get("username")
                else "unknown",
                text=comment.get("text") or "",
                permalink=post.get("permalink") or "",
                context=f"on post: {caption}",
                extra={
                    "media_id": post.get("id"),
                    "is_reply": bool(comment.get("parent_id")),
                },
            ))
    return result


def collect_instagram_dms(client: GraphClient, account, **_) -> CollectionResult:
    """Incoming Instagram direct messages."""
    result = CollectionResult(kind=KIND_INSTAGRAM_DM, account=account.slug)

    # Instagram messaging is reachable through either the Instagram
    # professional account ID or the Page it is linked to, depending on how
    # the app was set up. Try the Instagram ID, then fall back.
    owner_ids = [i for i in (account.instagram_user_id, account.facebook_page_id) if i]
    if not owner_ids:
        result.skipped_reason = (
            "no instagram_user_id or facebook_page_id configured")
        return result

    for index, owner_id in enumerate(owner_ids):
        attempt = CollectionResult(kind=KIND_INSTAGRAM_DM, account=account.slug)
        items = _collect_conversations(
            client,
            owner_id=owner_id,
            platform="instagram",
            kind=KIND_INSTAGRAM_DM,
            account=account,
            result=attempt,
        )
        if attempt.ok and not attempt.skipped_reason:
            result.items = items
            return result
        # Only report the last attempt's failure -- an earlier ID failing is
        # expected when the account is wired up the other way round.
        if index == len(owner_ids) - 1:
            result.error = attempt.error
            result.skipped_reason = attempt.skipped_reason
    return result
