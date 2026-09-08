"""Comments on ads -- both placements, across every ad account.

This is the surface no publishing tool can see, and usually the one that
matters most: a hostile comment sitting under a running ad is shown to
every future person the ad reaches, so it costs money for as long as it
stands.

An ad's creative points at the content it runs as. Two IDs matter, and they
carry *separate* comment threads for the same ad:

  effective_object_story_id     -> the Facebook Page post
  effective_instagram_media_id  -> the Instagram media

An ad running Advantage+ placements shows on both, so reading only the
Facebook side silently misses every comment left on Instagram. Dark posts
behave the same way, except the Page post never appears in the Page feed at
all -- which is exactly why walking the ad account is the only way to find
them.
"""

from __future__ import annotations

import logging

from ..graph import GraphClient, GraphError
from ..models import KIND_AD_COMMENT, CollectionResult, Item, parse_time
from .facebook import _describe, _summarise

log = logging.getLogger(__name__)

AD_LIMIT = 50
COMMENT_LIMIT = 50
# Reading comments costs one call per distinct story. Ads share creatives
# heavily, so this covers far more than 60 ads.
MAX_STORIES = 60


def collect_ad_comments(client: GraphClient, account, **_) -> CollectionResult:
    result = CollectionResult(kind=KIND_AD_COMMENT, account=account.slug)
    if not account.ad_account_ids:
        result.skipped_reason = "no ad_account_ids configured"
        return result

    fb_stories: dict[str, str] = {}
    ig_media: dict[str, str] = {}
    problems: list[str] = []

    for ad_account_id in account.ad_account_ids:
        try:
            creatives = _creatives_for(client, ad_account_id)
        except GraphError as exc:
            if exc.is_permission_error:
                problems.append(f"{ad_account_id}: no access (needs ads_read)")
            else:
                problems.append(f"{ad_account_id}: {exc}")
            continue
        _merge_targets(creatives, fb_stories, ig_media)

    if not fb_stories and not ig_media:
        result.skipped_reason = (
            "; ".join(problems) if problems else "no ads with attached content")
        return result

    unreadable = 0
    unreadable += _read_facebook(client, account, fb_stories, result)
    unreadable += _read_instagram(client, account, ig_media, result)

    if problems:
        result.skipped_reason = "; ".join(problems)
    elif unreadable and not result.items:
        result.skipped_reason = (
            f"none of the {unreadable} ad post(s) could be read -- most often "
            "the token is a Page token for a different Page than the ads run "
            "under, or Instagram comment access is not granted")
    return result


def _creatives_for(client: GraphClient, ad_account_id: str) -> list[dict]:
    return list(client.paginate(
        f"{ad_account_id}/ads",
        {
            "fields": (
                "id,name,creative{effective_object_story_id,"
                "effective_instagram_media_id}"
            ),
            # Paused ads keep collecting comments on their post, and those
            # comments stay visible, so they are still worth reading.
            # Archived and deleted ads are not.
            "effective_status": '["ACTIVE","PAUSED"]',
            "limit": AD_LIMIT,
        },
        max_pages=2,
    ))


def _merge_targets(ads: list[dict], fb_stories: dict, ig_media: dict) -> None:
    """Collect the two content IDs per ad, deduplicated.

    Several ads routinely share one creative, and the comments live on the
    content rather than the ad. Deduplicating here keeps the call count down
    and stops one comment being reported once per ad that runs it.
    """
    for ad in ads:
        creative = ad.get("creative") or {}
        name = ad.get("name") or ""
        story_id = creative.get("effective_object_story_id")
        if story_id and story_id not in fb_stories:
            fb_stories[story_id] = name
        media_id = creative.get("effective_instagram_media_id")
        if media_id and media_id not in ig_media:
            ig_media[media_id] = name


def _read_facebook(client, account, stories: dict[str, str],
                   result: CollectionResult) -> int:
    unreadable = 0
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
            # One unreadable story must not sink the collector; a creative
            # referencing another Page's post is a normal case.
            unreadable += 1
            log.debug("skipping FB comments for %s: %s", story_id, exc)
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
                context=f"Facebook ad: {_summarise(ad_name, story_id)}",
                extra={
                    "placement": "facebook",
                    "story_id": story_id,
                    "is_reply": bool(comment.get("parent")),
                },
            ))
    return unreadable


def _read_instagram(client, account, media: dict[str, str],
                    result: CollectionResult) -> int:
    unreadable = 0
    for media_id, ad_name in list(media.items())[:MAX_STORIES]:
        try:
            comments = list(client.paginate(
                f"{media_id}/comments",
                {"fields": "id,text,username,timestamp", "limit": COMMENT_LIMIT},
                max_pages=1,
            ))
        except GraphError as exc:
            unreadable += 1
            log.debug("skipping IG comments for %s: %s", media_id, exc)
            continue

        for comment in comments:
            username = comment.get("username")
            result.items.append(Item(
                kind=KIND_AD_COMMENT,
                id=str(comment.get("id")),
                account=account.slug,
                created_time=parse_time(comment.get("timestamp")),
                author=f"@{username}" if username else "unknown",
                text=comment.get("text") or "",
                permalink="",
                context=f"Instagram ad: {_summarise(ad_name, media_id)}",
                extra={"placement": "instagram", "media_id": media_id},
            ))
    return unreadable
