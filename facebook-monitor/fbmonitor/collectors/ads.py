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

# Reading comments costs one Graph call per ad post, per run, so the total
# is (posts x runs per day). Checking every post every fifteen minutes put
# this app past 1,500 calls an hour and got its API access blocked, so the
# work per run is capped and the posts rotate through the cap.
#
# The cap and the schedule have to be chosen together:
#
#   twice a day (the default)   150 x 2 x 2 accounts  =    600 calls/day
#   every hour                  150 x 24 x 2          =  7,200 calls/day
#   every 15 minutes            150 x 96 x 2          = 28,800 calls/day  <-- blocked
#
# At the default twice-daily cadence this covers every ad post in a typical
# account in a single run. Shorten the schedule and this must come down with
# it -- see "API call volume" in README.md.
MAX_COMMENT_CALLS = 150


def collect_ad_comments(client: GraphClient, account, page_client=None,
                        state=None, **_) -> CollectionResult:
    """Two tokens, because the two halves need different ones.

    Listing an ad account needs ads_read, which is a user-level permission.
    Reading the comments on the Page post behind an ad needs Page-level
    access. Using the user token for both looked like it worked, because
    Instagram comments do come back on a user token -- so the only ad
    comment that surfaced was an Instagram one, while every Facebook ad
    post failed.
    """
    result = CollectionResult(kind=KIND_AD_COMMENT, account=account.slug)
    # Fall back to the ads client only so a caller that passes one client
    # still functions; the monitor always passes both.
    reader = page_client or client
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

    # An ad account can promote more than one Page. A story ID is
    # "{page_id}_{post_id}", so posts belonging to another Page can be
    # identified and skipped before spending a call on them -- and, more
    # importantly, their absence is expected rather than a fault worth
    # reporting on every run forever.
    ours, theirs = _split_by_page(fb_stories, account.facebook_page_id)

    # Rotate through the posts rather than reading all of them every run.
    fb_slice, ig_slice, next_cursor = _rotate(
        ours, ig_media,
        cursor=state.ad_cursor(account.slug) if state else 0,
        budget=MAX_COMMENT_CALLS)
    if state:
        state.set_ad_cursor(account.slug, next_cursor)

    fb_unreadable = _read_facebook(reader, account, fb_slice, result)
    ig_unreadable = _read_instagram(reader, account, ig_slice, result)

    # A partial failure must be reported even when some comments did come
    # back. Reporting "3 new" while silently failing on 20 other ad posts
    # reads identically to "you have 3 comments", which is precisely the
    # false reassurance this tool exists to prevent.
    notes = list(problems)
    if fb_unreadable:
        notes.append(
            f"{fb_unreadable} of {len(fb_slice)} Facebook ad post(s) checked "
            "this run could not be read")
    if ig_unreadable and not any(
            i.extra.get("placement") == "instagram" for i in result.items):
        notes.append(
            f"none of the {ig_unreadable} Instagram ad post(s) could be read "
            "-- Instagram comment access may not be granted")
    if result.rate_limited:
        notes.append("Graph rate-limited this run -- the remaining ad posts "
                     "were left for the next one")
    if theirs:
        log.debug("skipped %d ad post(s) belonging to another Page", theirs)
    if notes:
        result.skipped_reason = "; ".join(notes)
    return result


def _rotate(fb: dict[str, str], ig: dict[str, str], *, cursor: int,
            budget: int):
    """Take the next `budget` posts, continuing where the last run stopped.

    Reading every ad post on every run is what triggered the rate block.
    Posts are ordered deterministically and walked in a loop, so each one is
    still checked regularly without the call count growing with the number
    of ads ever run.
    """
    ordered = ([("fb", sid, name) for sid, name in sorted(fb.items())]
               + [("ig", sid, name) for sid, name in sorted(ig.items())])
    if not ordered:
        return {}, {}, 0

    cursor = cursor % len(ordered)
    taken = [ordered[(cursor + n) % len(ordered)]
             for n in range(min(budget, len(ordered)))]

    fb_slice = {sid: name for kind, sid, name in taken if kind == "fb"}
    ig_slice = {sid: name for kind, sid, name in taken if kind == "ig"}
    return fb_slice, ig_slice, (cursor + len(taken)) % len(ordered)


def _split_by_page(stories: dict[str, str], page_id: str | None):
    """Keep the stories on our own Page; count the rest without reading them."""
    if not page_id:
        return stories, 0
    ours = {sid: name for sid, name in stories.items()
            if sid.split("_", 1)[0] == str(page_id)}
    return ours, len(stories) - len(ours)


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
            # ACTIVE only. A paused ad is not being served, so its
            # comments are not being shown to anyone new -- which is the
            # entire reason ad comments are worth watching. Including
            # paused ads multiplied the call volume for no benefit.
            "effective_status": '["ACTIVE"]',
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
    for story_id, ad_name in stories.items():
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
            if exc.is_rate_limited:
                # Stop immediately. Continuing after being throttled is what
                # escalates a temporary limit into a blocked app.
                log.warning("rate limited; abandoning the rest of this run")
                result.rate_limited = True
                break
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
    for media_id, ad_name in media.items():
        try:
            comments = list(client.paginate(
                f"{media_id}/comments",
                {"fields": "id,text,username,timestamp", "limit": COMMENT_LIMIT},
                max_pages=1,
            ))
        except GraphError as exc:
            if exc.is_rate_limited:
                log.warning("rate limited; abandoning the rest of this run")
                result.rate_limited = True
                break
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
