"""Collector registry.

Each collector takes (client, account) and returns a CollectionResult. They
are all read-only by construction -- they receive a GraphClient, which has
no write method.
"""

from ..models import (
    KIND_AD_COMMENT,
    KIND_INSTAGRAM_COMMENT,
    KIND_INSTAGRAM_DM,
    KIND_MESSENGER_DM,
    KIND_PAGE_COMMENT,
    KIND_REVIEW,
    KIND_VISITOR_POST,
)
from .ads import collect_ad_comments
from .facebook import (
    collect_messenger,
    collect_page_comments,
    collect_reviews,
    collect_visitor_posts,
)
from .instagram import collect_instagram_comments, collect_instagram_dms

COLLECTORS = {
    KIND_MESSENGER_DM: collect_messenger,
    KIND_INSTAGRAM_DM: collect_instagram_dms,
    KIND_REVIEW: collect_reviews,
    KIND_VISITOR_POST: collect_visitor_posts,
    KIND_AD_COMMENT: collect_ad_comments,
    KIND_PAGE_COMMENT: collect_page_comments,
    KIND_INSTAGRAM_COMMENT: collect_instagram_comments,
}

__all__ = ["COLLECTORS"]
