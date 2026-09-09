"""Collectors for Facebook Page surfaces.

Four separate edges, because Graph does not offer one "everything happening
on my Page" endpoint: published-post comments, visitor posts,
recommendations, and Messenger threads all live apart.
"""

from __future__ import annotations

import logging

from ..graph import GraphClient, GraphError
from ..models import (
    KIND_MESSENGER_DM,
    KIND_PAGE_COMMENT,
    KIND_REVIEW,
    KIND_VISITOR_POST,
    CollectionResult,
    Item,
    parse_time,
)

log = logging.getLogger(__name__)

# How far back to look on each run. Generous enough that a monitor which
# missed a few runs still catches up; dedup makes the overlap free.
POST_LIMIT = 25
COMMENT_LIMIT = 50
CONVERSATION_LIMIT = 25
MESSAGE_LIMIT = 15


def collect_page_comments(client: GraphClient, account, **_) -> CollectionResult:
    """Comments left on posts the Page published."""
    result = CollectionResult(kind=KIND_PAGE_COMMENT, account=account.slug)
    page_id = account.facebook_page_id
    if not page_id:
        result.skipped_reason = "no facebook_page_id configured"
        return result

    fields = (
        "id,message,created_time,permalink_url,"
        f"comments.limit({COMMENT_LIMIT})"
        "{id,message,from,created_time,permalink_url,parent}"
    )
    try:
        posts = list(client.paginate(
            f"{page_id}/posts",
            {"fields": fields, "limit": POST_LIMIT},
            max_pages=2,
        ))
    except GraphError as exc:
        result.error = _describe(exc, "reading Page posts")
        return result

    for post in posts:
        post_summary = _summarise(post.get("message"), "(no caption)")
        for comment in (post.get("comments") or {}).get("data", []):
            author = (comment.get("from") or {}).get("name") or "unknown"
            result.items.append(Item(
                kind=KIND_PAGE_COMMENT,
                id=str(comment.get("id")),
                account=account.slug,
                created_time=parse_time(comment.get("created_time")),
                author=author,
                text=comment.get("message") or "",
                permalink=comment.get("permalink_url")
                or post.get("permalink_url") or "",
                context=f"on post: {post_summary}",
                extra={
                    "post_id": post.get("id"),
                    # A comment carrying a parent is a reply in a thread.
                    "is_reply": bool(comment.get("parent")),
                },
            ))
    return result


def collect_visitor_posts(client: GraphClient, account, **_) -> CollectionResult:
    """Posts other people wrote onto the Page itself."""
    result = CollectionResult(kind=KIND_VISITOR_POST, account=account.slug)
    page_id = account.facebook_page_id
    if not page_id:
        result.skipped_reason = "no facebook_page_id configured"
        return result

    try:
        posts = list(client.paginate(
            f"{page_id}/visitor_posts",
            {
                "fields": "id,message,from,created_time,permalink_url",
                "limit": POST_LIMIT,
            },
            max_pages=2,
        ))
    except GraphError as exc:
        # Many Pages simply have visitor posting switched off. That is a
        # setting, not a fault, so say so plainly instead of erroring.
        if exc.is_permission_error:
            result.skipped_reason = (
                "visitor posts unavailable -- either turned off on the Page "
                "or the token lacks pages_read_user_content")
            return result
        result.error = _describe(exc, "reading visitor posts")
        return result

    for post in posts:
        result.items.append(Item(
            kind=KIND_VISITOR_POST,
            id=str(post.get("id")),
            account=account.slug,
            created_time=parse_time(post.get("created_time")),
            author=(post.get("from") or {}).get("name") or "unknown",
            text=post.get("message") or "",
            permalink=post.get("permalink_url") or "",
            context="posted directly to the Page",
        ))
    return result


def collect_reviews(client: GraphClient, account, **_) -> CollectionResult:
    """Page recommendations (what used to be star ratings)."""
    result = CollectionResult(kind=KIND_REVIEW, account=account.slug)
    page_id = account.facebook_page_id
    if not page_id:
        result.skipped_reason = "no facebook_page_id configured"
        return result

    try:
        ratings = list(client.paginate(
            f"{page_id}/ratings",
            {
                "fields": (
                    "reviewer,rating,review_text,created_time,"
                    "recommendation_type,open_graph_story"
                ),
                "limit": POST_LIMIT,
            },
            max_pages=2,
        ))
    except GraphError as exc:
        if exc.is_permission_error:
            result.skipped_reason = (
                "recommendations unavailable -- either turned off on the Page "
                "or the token lacks pages_read_engagement")
            return result
        result.error = _describe(exc, "reading recommendations")
        return result

    for rating in ratings:
        recommendation = rating.get("recommendation_type") or ""
        # Facebook replaced the 1-5 star rating with a yes/no
        # recommendation; older entries may still carry a number.
        verdict = recommendation or (
            f"{rating['rating']} stars" if rating.get("rating") else "unspecified")
        story = rating.get("open_graph_story") or {}
        result.items.append(Item(
            kind=KIND_REVIEW,
            id=str(story.get("id") or _synthetic_review_id(rating)),
            account=account.slug,
            created_time=parse_time(rating.get("created_time")),
            author=(rating.get("reviewer") or {}).get("name") or "unknown",
            text=rating.get("review_text") or "",
            permalink="",
            context=f"recommendation: {verdict}",
            extra={"recommendation_type": recommendation},
        ))
    return result


def collect_messenger(client: GraphClient, account, **_) -> CollectionResult:
    """Incoming Messenger messages, newest thread first."""
    result = CollectionResult(kind=KIND_MESSENGER_DM, account=account.slug)
    page_id = account.facebook_page_id
    if not page_id:
        result.skipped_reason = "no facebook_page_id configured"
        return result

    result.items = _collect_conversations(
        client,
        owner_id=page_id,
        platform="messenger",
        kind=KIND_MESSENGER_DM,
        account=account,
        result=result,
    )
    return result


# -- shared between Messenger and Instagram DMs --------------------------

def _collect_conversations(client, *, owner_id, platform, kind, account, result):
    """Read conversations and return only messages from the other party.

    The Page's own replies come back in the same thread; reporting them
    would mean every answered enquiry showed up as something needing
    attention.
    """
    fields = (
        "id,updated_time,participants,"
        f"messages.limit({MESSAGE_LIMIT})"
        "{id,message,from,created_time}"
    )
    try:
        conversations = list(client.paginate(
            f"{owner_id}/conversations",
            {"fields": fields, "platform": platform, "limit": CONVERSATION_LIMIT},
            max_pages=2,
        ))
    except GraphError as exc:
        if exc.is_permission_error:
            result.skipped_reason = (
                f"{platform} conversations unavailable -- the token lacks the "
                "messaging permission, or the app is not approved for it")
            return []
        result.error = _describe(exc, f"reading {platform} conversations")
        return []

    items: list[Item] = []
    for convo in conversations:
        other = _other_participant(convo, owner_id)
        for message in (convo.get("messages") or {}).get("data", []):
            sender = message.get("from") or {}
            if str(sender.get("id")) == str(owner_id):
                continue  # our own reply
            items.append(Item(
                kind=kind,
                id=str(message.get("id")),
                account=account.slug,
                created_time=parse_time(message.get("created_time")),
                author=sender.get("name") or other or "unknown",
                text=message.get("message") or "",
                permalink="",
                context=f"conversation with {other}",
                extra={"conversation_id": convo.get("id")},
            ))
    return items


def _other_participant(convo: dict, owner_id: str) -> str:
    for person in (convo.get("participants") or {}).get("data", []):
        if str(person.get("id")) != str(owner_id):
            return person.get("name") or person.get("username") or "unknown"
    return "unknown"


def _synthetic_review_id(rating: dict) -> str:
    """Recommendations do not always carry an ID; build a stable one so
    dedup still works across runs."""
    reviewer = (rating.get("reviewer") or {}).get("id", "anon")
    return f"review:{reviewer}:{rating.get('created_time', '')}"


def _summarise(text: str | None, fallback: str, width: int = 60) -> str:
    if not text:
        return fallback
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _describe(exc: GraphError, doing: str) -> str:
    code = f" (code {exc.code})" if exc.code else ""
    return f"{doing} failed{code}: {exc}"
