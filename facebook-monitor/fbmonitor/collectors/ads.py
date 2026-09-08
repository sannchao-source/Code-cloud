"""Comments on ads, including dark posts.

This is the surface no publishing tool can see. An ad's creative points at
a Page post via ``effective_object_story_id``; for a boosted post that is an
ordinary post, but for a dark post it is one that never appears in the
Page's feed. Either way the comments hang off that story ID, so the job is
to walk the ad account's creatives, collect the story IDs, and read the
comments on each.
"""

from __future__ import annotations

import logging

from ..graph import GraphClient, GraphError
from ..models import KIND_AD_COMMENT, CollectionResult, Item, parse_time
from .facebook import _describe, _summarise

log = logging.getLogger(__name__)

AD_LIMIT = 50
COMMENT_LIMIT = 50
# Reading comments costs one call per distinct story, so cap the fan-out.
# Ads share creatives heavily, so this covers far more than 40 ads.
MAX_STORIES = 40


def collect_ad_comments(client: GraphClient, account, **_) -> CollectionResult:
    result = CollectionResult(kind=KIND_AD_COMMENT, account=account.slug)
    ad_account_id = account.ad_account_id
    if not ad_account_id:
        result.skipped_reason = "no ad_account_id configured"
        return result

    try:
        ads = list(client.paginate(
            f"{ad_account_id}/ads",
            {
                "fields": "id,name,creative{effective_object_story_id}",
                # Paused ads keep collecting comments on their post, but
                # ads that are archived or deleted are not worth the calls.
                "effective_status": '["ACTIVE","PAUSED"]',
                "limit": AD_LIMIT,
            },
            max_pages=2,
        ))
    except GraphError as exc:
        if exc.is_permission_error:
            result.skipped_reason = (
                "ad account unavailable -- the token lacks ads_read, or has no "
                "access to this ad account")
            return result
        result.error = _describe(exc, "listing ads")
        return result

    stories = _story_ids(ads)
    if not stories:
        return result

    failures = 0
    for story_id, ad_name in list(stories.items())[:MAX_STORIES]:
        try:
            comments = list(client.paginate(
                f"{story_id}/comments",
                {
                    "fields": "id,message,from,created_time,permalink_url,parent",
                    "filter": "stream",
                    "limit": COMMENT_LIMIT,
                },
                max_pages=1,
            ))
        except GraphError as exc:
            # A single unreadable story should not sink the whole collector;
            # creatives referencing another Page's post are a normal case.
            failures += 1
            log.debug("skipping comments for story %s: %s", story_id, exc)
            continue

        for comment in comments:
            result.items.append(Item(
                kind=KIND_AD_COMMENT,
                id=str(comment.get("id")),
                account=account.slug,
                created_time=parse_time(comment.get("created_time")),
                author=(comment.get("from") or {}).get("name") or "unknown",
                text=comment.get("message") or "",
                permalink=comment.get("permalink_url") or "",
                context=f"on ad: {_summarise(ad_name, story_id)}",
                extra={"story_id": story_id, "is_reply": bool(comment.get("parent"))},
            ))

    if failures and not result.items:
        result.skipped_reason = (
            f"none of the {failures} ad post(s) could be read -- most often the "
            "token is a Page token for a different Page than the ads run under")
    return result


def _story_ids(ads: list[dict]) -> dict[str, str]:
    """Map story ID -> a representative ad name.

    Several ads routinely share one creative, and the comments live on the
    post, not the ad. Deduplicating here is what keeps the call count down
    and stops the same comment being reported once per ad.
    """
    stories: dict[str, str] = {}
    for ad in ads:
        story_id = (ad.get("creative") or {}).get("effective_object_story_id")
        if story_id and story_id not in stories:
            stories[story_id] = ad.get("name") or ""
    return stories
