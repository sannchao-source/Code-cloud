"""Tests for the monitor. No network: Graph is faked at the client seam."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fbmonitor.collectors.ads import _merge_targets, collect_ad_comments
from fbmonitor.collectors.facebook import (
    collect_messenger,
    collect_page_comments,
    collect_reviews,
)
from fbmonitor.collectors.instagram import collect_instagram_comments
from fbmonitor.config import Account, ConfigError, load_accounts
from fbmonitor.digest import render_json, render_text
from fbmonitor.graph import GraphClient, GraphError
from fbmonitor import notify, tokens, triage
from fbmonitor.models import (KIND_AD_COMMENT, KIND_ORDER,
                             KIND_PAGE_COMMENT, Item, parse_time)
from fbmonitor.models import CollectionResult
from fbmonitor.monitor import AccountReport, Report, run
from fbmonitor.state import State


class FakeGraph:
    """Stands in for GraphClient. Records calls; returns canned payloads."""

    def __init__(self, responses: dict, errors: dict | None = None):
        self.responses = responses
        self.errors = errors or {}
        self.calls: list[str] = []
        self.last_params: dict[str, dict] = {}

    def get(self, path, params=None):
        self.calls.append(path)
        self.last_params[path] = dict(params or {})
        if path in self.errors:
            raise self.errors[path]
        return self.responses.get(path, {"data": []})

    def paginate(self, path, params=None, *, max_pages=10):
        return iter(self.get(path, params).get("data", []))


def account(**kw):
    base = dict(name="Test Co", slug="test-co", token_env="TEST_TOKEN",
                ads_token_env=None,
                facebook_page_id="100", instagram_user_id="200",
                ad_account_ids=["act_300"])
    base.update(kw)
    return Account(**base)


class TestParseTime(unittest.TestCase):
    def test_graph_iso_with_offset(self):
        parsed = parse_time("2026-09-08T10:15:00+0000")
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.hour, 10)

    def test_epoch_milliseconds(self):
        # Messenger hands back epoch millis rather than an ISO string.
        parsed = parse_time(1757325600000)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_junk_is_none_not_an_exception(self):
        self.assertIsNone(parse_time("not a date"))
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time(""))


class TestPageComments(unittest.TestCase):
    def test_extracts_comments_from_posts(self):
        graph = FakeGraph({
            "100/posts": {"data": [{
                "id": "100_1",
                "message": "Fade or scissor cut?",
                "permalink_url": "https://fb.com/100_1",
                "comments": {"data": [
                    {"id": "c1", "message": "Fade every time",
                     "from": {"name": "Dave"},
                     "created_time": "2026-09-08T09:00:00+0000"},
                    {"id": "c2", "message": "Scissors",
                     "from": {"name": "Sam"},
                     "created_time": "2026-09-08T10:00:00+0000",
                     "parent": {"id": "c1"}},
                ]},
            }]},
        })
        result = collect_page_comments(graph, account())
        self.assertTrue(result.ok)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.items[0].author, "Dave")
        self.assertIn("Fade or scissor cut?", result.items[0].context)
        # The second is a threaded reply, and should be flagged as one.
        self.assertTrue(result.items[1].extra["is_reply"])

    def test_post_with_no_comments_yields_nothing(self):
        graph = FakeGraph({"100/posts": {"data": [{"id": "100_1"}]}})
        result = collect_page_comments(graph, account())
        self.assertTrue(result.ok)
        self.assertEqual(result.items, [])

    def test_missing_page_id_is_skipped_not_failed(self):
        result = collect_page_comments(FakeGraph({}), account(facebook_page_id=None))
        self.assertTrue(result.ok)
        self.assertIn("no facebook_page_id", result.skipped_reason)


class TestMessenger(unittest.TestCase):
    def test_excludes_the_pages_own_replies(self):
        graph = FakeGraph({
            "100/conversations": {"data": [{
                "id": "conv1",
                "participants": {"data": [
                    {"id": "100", "name": "Republic of Barbers"},
                    {"id": "999", "name": "Jo Bloggs"},
                ]},
                "messages": {"data": [
                    {"id": "m1", "message": "Are you open Sunday?",
                     "from": {"id": "999", "name": "Jo Bloggs"},
                     "created_time": "2026-09-08T09:00:00+0000"},
                    {"id": "m2", "message": "Yes, 10-4.",
                     "from": {"id": "100", "name": "Republic of Barbers"},
                     "created_time": "2026-09-08T09:05:00+0000"},
                ]},
            }]},
        })
        result = collect_messenger(graph, account())
        self.assertEqual(len(result.items), 1, "our own reply must not be reported")
        self.assertEqual(result.items[0].text, "Are you open Sunday?")
        self.assertIn("Jo Bloggs", result.items[0].context)

    def test_missing_permission_is_a_skip_with_a_useful_reason(self):
        graph = FakeGraph({}, errors={
            "100/conversations": GraphError("no perm", code=200)})
        result = collect_messenger(graph, account())
        self.assertTrue(result.ok, "a missing scope is not a hard failure")
        self.assertIn("messaging permission", result.skipped_reason)


class TestReviews(unittest.TestCase):
    def test_recommendation_without_id_still_dedups(self):
        graph = FakeGraph({
            "100/ratings": {"data": [{
                "reviewer": {"id": "7", "name": "Pat"},
                "recommendation_type": "positive",
                "review_text": "Best fade in Adelaide",
                "created_time": "2026-09-07T09:00:00+0000",
            }]},
        })
        first = collect_reviews(graph, account()).items[0]
        second = collect_reviews(graph, account()).items[0]
        # A synthetic ID must be stable, or every run re-reports the review.
        self.assertEqual(first.id, second.id)
        self.assertIn("positive", first.context)


class TestInstagram(unittest.TestCase):
    def test_prefixes_handles_with_at(self):
        graph = FakeGraph({
            "200/media": {"data": [{
                "id": "m1", "caption": "Best in Class",
                "permalink": "https://instagram.com/p/x",
                "comments": {"data": [{
                    "id": "ic1", "text": "🔥", "username": "someone",
                    "timestamp": "2026-09-08T09:00:00+0000"}]},
            }]},
        })
        result = collect_instagram_comments(graph, account())
        self.assertEqual(result.items[0].author, "@someone")


class TestAdComments(unittest.TestCase):
    def test_dedups_creatives_shared_across_ads(self):
        fb, ig = {}, {}
        _merge_targets([
            {"id": "a1", "name": "Spring", "creative": {
                "effective_object_story_id": "100_777",
                "effective_instagram_media_id": "ig_888"}},
            {"id": "a2", "name": "Spring copy", "creative": {
                "effective_object_story_id": "100_777",
                "effective_instagram_media_id": "ig_888"}},
            {"id": "a3", "name": "No creative"},
        ], fb, ig)
        self.assertEqual(list(fb), ["100_777"])
        self.assertEqual(list(ig), ["ig_888"])

    def test_reads_both_placements_of_one_ad(self):
        # An ad on Advantage+ placements runs on Facebook and Instagram, and
        # each carries its own separate comment thread. Reading only the
        # Facebook side silently loses half the comments.
        graph = FakeGraph({
            "act_300/ads": {"data": [{
                "id": "a1", "name": "Father's Day",
                "creative": {"effective_object_story_id": "100_777",
                             "effective_instagram_media_id": "ig_888"}}]},
            "100_777/comments": {"data": [{
                "id": "fb1", "message": "How much?",
                "from": {"name": "Chris"},
                "created_time": "2026-09-08T08:00:00+0000"}]},
            "ig_888/comments": {"data": [{
                "id": "ig1", "text": "overpriced tbh", "username": "someone",
                "timestamp": "2026-09-08T09:00:00+0000"}]},
        })
        result = collect_ad_comments(graph, account())
        placements = {i.extra["placement"] for i in result.items}
        self.assertEqual(placements, {"facebook", "instagram"})
        self.assertEqual(len(result.items), 2)

    def test_walks_every_configured_ad_account(self):
        graph = FakeGraph({
            "act_300/ads": {"data": [{"id": "a1", "name": "A", "creative": {
                "effective_object_story_id": "100_1"}}]},
            "act_301/ads": {"data": [{"id": "a2", "name": "B", "creative": {
                "effective_object_story_id": "100_2"}}]},
            "100_1/comments": {"data": [{
                "id": "c1", "message": "one", "from": {"name": "X"},
                "created_time": "2026-09-08T08:00:00+0000"}]},
            "100_2/comments": {"data": [{
                "id": "c2", "message": "two", "from": {"name": "Y"},
                "created_time": "2026-09-08T08:00:00+0000"}]},
        })
        result = collect_ad_comments(
            graph, account(ad_account_ids=["act_300", "act_301"]))
        self.assertEqual({i.id for i in result.items}, {"c1", "c2"})

    def test_one_bad_ad_account_does_not_lose_the_others(self):
        graph = FakeGraph(
            {"act_301/ads": {"data": [{"id": "a2", "name": "B", "creative": {
                "effective_object_story_id": "100_2"}}]},
             "100_2/comments": {"data": [{
                 "id": "c2", "message": "two", "from": {"name": "Y"},
                 "created_time": "2026-09-08T08:00:00+0000"}]}},
            errors={"act_300/ads": GraphError("denied", code=200)},
        )
        result = collect_ad_comments(
            graph, account(ad_account_ids=["act_300", "act_301"]))
        self.assertEqual([i.id for i in result.items], ["c2"])
        self.assertIn("act_300", result.skipped_reason)

    def test_unreadable_story_does_not_fail_the_collector(self):
        graph = FakeGraph(
            {"act_300/ads": {"data": [{
                "id": "a1", "name": "X",
                "creative": {"effective_object_story_id": "100_777"}}]}},
            errors={"100_777/comments": GraphError("nope", code=100)},
        )
        result = collect_ad_comments(graph, account(facebook_page_id="100"))
        self.assertTrue(result.ok)
        self.assertIn("could not be read", result.skipped_reason)


class TestCallVolumeIsBounded(unittest.TestCase):
    """Reading every ad post every run got this app's API access blocked.

    At 60 posts per placement across three ad accounts every fifteen
    minutes, the design issued roughly 1,500 Graph calls an hour from an app
    created the day before. Meta blocked it. The work per run must therefore
    be bounded regardless of how many ads have ever run.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(Path(self.dir.name) / "state.json")

    def tearDown(self):
        self.dir.cleanup()

    def _graph_with(self, n_posts):
        ads = [{"id": f"a{i}", "name": f"Ad {i}",
                "creative": {"effective_object_story_id": f"100_{i}"}}
               for i in range(n_posts)]
        responses = {"act_300/ads": {"data": ads}}
        for i in range(n_posts):
            responses[f"100_{i}/comments"] = {"data": []}
        return FakeGraph(responses)

    def test_calls_are_capped_however_many_ads_exist(self):
        from fbmonitor.collectors.ads import MAX_COMMENT_CALLS
        graph = self._graph_with(200)
        collect_ad_comments(graph, account(facebook_page_id="100"),
                            state=self.state)
        comment_calls = [c for c in graph.calls if c.endswith("/comments")]
        self.assertLessEqual(len(comment_calls), MAX_COMMENT_CALLS)

    def test_successive_runs_walk_through_the_rest(self):
        # Capping alone would mean posts past the cap were never checked.
        seen = set()
        for _ in range(4):
            graph = self._graph_with(50)
            collect_ad_comments(graph, account(facebook_page_id="100"),
                                state=self.state)
            seen.update(c for c in graph.calls if c.endswith("/comments"))
        self.assertGreater(len(seen), 20,
                           "the rotation must reach posts beyond the cap")

    def test_the_cursor_wraps_rather_than_running_off_the_end(self):
        for _ in range(10):
            graph = self._graph_with(5)
            result = collect_ad_comments(
                graph, account(facebook_page_id="100"), state=self.state)
            self.assertTrue(result.ok)

    def test_paused_ads_are_not_requested(self):
        # A paused ad is not being served, so its comments are shown to
        # nobody new -- the entire reason for watching ad comments.
        graph = self._graph_with(1)
        collect_ad_comments(graph, account(facebook_page_id="100"),
                            state=self.state)
        params = graph.last_params.get("act_300/ads", {})
        self.assertEqual(params.get("effective_status"), '["ACTIVE"]')

    def test_throttling_stops_the_run_instead_of_pushing_through(self):
        # Continuing after Graph says slow down is what turns a temporary
        # limit into a blocked app.
        graph = FakeGraph(
            {"act_300/ads": {"data": [
                {"id": f"a{i}", "name": "x", "creative": {
                    "effective_object_story_id": f"100_{i}"}} for i in range(10)]}},
            errors={f"100_{i}/comments": GraphError("slow down", code=4)
                    for i in range(10)},
        )
        result = collect_ad_comments(graph, account(facebook_page_id="100"),
                                     state=self.state)
        self.assertTrue(result.rate_limited)
        self.assertEqual(
            len([c for c in graph.calls if c.endswith("/comments")]), 1,
            "must stop at the first throttle, not keep trying")
        self.assertIn("rate-limited", result.skipped_reason)

    def test_a_rate_limit_code_is_recognised(self):
        for code in (4, 17, 32, 613):
            self.assertTrue(GraphError("x", code=code).is_rate_limited,
                            f"code {code} is a Graph throttle")


class TestPartialAdFailureIsReported(unittest.TestCase):
    """Some comments arriving must not mask the rest failing.

    The live run returned one Instagram ad comment and reported no problem,
    while every Facebook ad post had in fact failed to read. "1 new comment"
    is indistinguishable from "you have 1 comment" -- exactly the false
    reassurance this tool exists to prevent.
    """

    def test_reports_failures_even_when_some_comments_came_back(self):
        graph = FakeGraph(
            {
                "act_300/ads": {"data": [
                    {"id": "a1", "name": "A", "creative": {
                        "effective_object_story_id": "100_1"}},
                    {"id": "a2", "name": "B", "creative": {
                        "effective_object_story_id": "100_2"}},
                ]},
                "100_1/comments": {"data": [{
                    "id": "ok", "message": "nice", "from": {"name": "X"},
                    "created_time": "2026-09-09T08:00:00+0000"}]},
            },
            errors={"100_2/comments": GraphError("nope", code=100)},
        )
        result = collect_ad_comments(graph, account(facebook_page_id="100"))
        self.assertEqual(len(result.items), 1)
        self.assertIsNotNone(result.skipped_reason,
                             "a silent partial failure is the whole problem")
        self.assertIn("1 of 2", result.skipped_reason)

    def test_uses_the_page_token_to_read_comments_not_the_ads_token(self):
        # Listing an ad account needs the user token (ads_read); reading the
        # comments on the Page post behind an ad needs the Page token. Using
        # the ads token for both meant only Instagram comments came back.
        used = []

        class Recording(FakeGraph):
            def __init__(self, responses, label):
                super().__init__(responses)
                self.label = label

            def paginate(self, path, params=None, *, max_pages=10):
                used.append((self.label, path))
                return super().paginate(path, params, max_pages=max_pages)

        ads_client = Recording(
            {"act_300/ads": {"data": [{"id": "a1", "name": "A", "creative": {
                "effective_object_story_id": "100_1"}}]}}, "ads")
        page_client = Recording(
            {"100_1/comments": {"data": [{
                "id": "c1", "message": "hi", "from": {"name": "X"},
                "created_time": "2026-09-09T08:00:00+0000"}]}}, "page")

        result = collect_ad_comments(
            ads_client, account(facebook_page_id="100"),
            page_client=page_client)

        self.assertEqual([i.id for i in result.items], ["c1"])
        self.assertIn(("ads", "act_300/ads"), used)
        self.assertIn(("page", "100_1/comments"), used)

    def test_other_pages_ads_are_skipped_without_a_call_or_a_warning(self):
        graph = FakeGraph({
            "act_300/ads": {"data": [
                {"id": "a1", "name": "Ours", "creative": {
                    "effective_object_story_id": "100_1"}},
                {"id": "a2", "name": "Theirs", "creative": {
                    "effective_object_story_id": "999_1"}},
            ]},
            "100_1/comments": {"data": [{
                "id": "c1", "message": "hi", "from": {"name": "X"},
                "created_time": "2026-09-09T08:00:00+0000"}]},
        })
        result = collect_ad_comments(graph, account(facebook_page_id="100"))
        self.assertEqual([i.id for i in result.items], ["c1"])
        # Never even attempted, so no wasted call and no standing warning.
        self.assertNotIn("999_1/comments", graph.calls)
        self.assertIsNone(result.skipped_reason)


class TestAdAccountSharedBetweenPages(unittest.TestCase):
    """One ad account can promote two Pages at once.

    Comments live on the Page post behind each ad, so a run using one
    business's Page token must pick up its own Page's ads from the shared
    account and quietly skip the other Page's, rather than failing.
    """

    def _graph(self):
        return FakeGraph(
            {
                "act_shared/ads": {"data": [
                    {"id": "a1", "name": "Barbers ad", "creative": {
                        "effective_object_story_id": "100_1"}},
                    {"id": "a2", "name": "Fitfable ad", "creative": {
                        "effective_object_story_id": "200_1"}},
                ]},
                "100_1/comments": {"data": [{
                    "id": "c_rob", "message": "how much?",
                    "from": {"name": "A"},
                    "created_time": "2026-09-08T08:00:00+0000"}]},
                "200_1/comments": {"data": [{
                    "id": "c_fit", "message": "love this",
                    "from": {"name": "B"},
                    "created_time": "2026-09-08T08:00:00+0000"}]},
            },
            # The other Page's post is invisible to this token.
            errors={"200_1/comments": GraphError("not visible", code=100)},
        )

    def test_reads_only_its_own_pages_ads(self):
        result = collect_ad_comments(
            self._graph(),
            account(slug="rob", facebook_page_id="100",
                    ad_account_ids=["act_shared"]))
        self.assertEqual([i.id for i in result.items], ["c_rob"])
        # Partial success must not be reported as a failure -- the other
        # Page's ads being unreadable here is expected, not a fault.
        self.assertTrue(result.ok)
        self.assertIsNone(result.skipped_reason)


class TestTriage(unittest.TestCase):
    def _item(self, text, kind=KIND_AD_COMMENT):
        return Item(kind=kind, id="x", account="a", created_time=None, text=text)

    def test_complaint_outranks_a_question(self):
        # The whole point of severity: a complaint under a live ad must not
        # sit below a newer, more ordinary comment.
        complaint, _ = triage.assess(self._item("total rip off, never again"))
        question, _ = triage.assess(self._item("how much for a fade?"))
        ordinary, _ = triage.assess(self._item("nice one lads"))
        self.assertGreater(complaint, question)
        self.assertGreater(question, ordinary)
        self.assertEqual(complaint, triage.SEVERITY_COMPLAINT)

    def test_complaint_reason_names_the_match(self):
        _, reason = triage.assess(self._item("absolute rip off"))
        self.assertIn("rip off", reason)

    def test_ordinary_page_comment_is_not_flagged(self):
        severity, _ = triage.assess(
            self._item("looks great", kind=KIND_PAGE_COMMENT))
        self.assertEqual(severity, triage.SEVERITY_NONE)

    def test_complaint_on_a_page_comment_is_still_flagged(self):
        severity, _ = triage.assess(
            self._item("staff were rude", kind=KIND_PAGE_COMMENT))
        self.assertEqual(severity, triage.SEVERITY_COMPLAINT)

    def test_plain_question_anywhere_is_a_lead(self):
        severity, _ = triage.assess(
            self._item("are you open sunday?", kind=KIND_PAGE_COMMENT))
        self.assertEqual(severity, triage.SEVERITY_LEAD)

    def test_triage_never_drops_items(self):
        items = [self._item("fine", KIND_PAGE_COMMENT), self._item("scam")]
        self.assertEqual(len(triage.apply(items)), 2,
                         "triage orders the digest, it must never filter it")

    def test_sorts_complaints_above_newer_ordinary_comments(self):
        from datetime import datetime, timedelta, timezone
        old_complaint = Item(kind=KIND_AD_COMMENT, id="a", account="x",
                             created_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
                             text="absolute rip off")
        new_praise = Item(kind=KIND_AD_COMMENT, id="b", account="x",
                          created_time=datetime(2026, 9, 8, tzinfo=timezone.utc),
                          text="great work")
        ar = AccountReport(account=account())
        ar.results = [CollectionResult(
            kind=KIND_AD_COMMENT, account="x",
            items=triage.apply([new_praise, old_complaint]))]
        self.assertEqual([i.id for i in ar.new_items], ["a", "b"])


class TestPriorityOrder(unittest.TestCase):
    def test_ad_comments_come_first(self):
        self.assertEqual(KIND_ORDER[0], KIND_AD_COMMENT)


class TestState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"

    def tearDown(self):
        self.dir.cleanup()

    def _items(self, *ids):
        return [Item(kind=KIND_PAGE_COMMENT, id=i, account="test-co",
                     created_time=datetime.now(timezone.utc)) for i in ids]

    def test_filters_out_already_seen(self):
        state = State(self.path)
        first = state.filter_new("test-co", KIND_PAGE_COMMENT, self._items("a", "b"))
        self.assertEqual(len(first), 2)
        state.record("test-co", KIND_PAGE_COMMENT, ["a", "b"])
        second = state.filter_new(
            "test-co", KIND_PAGE_COMMENT, self._items("a", "b", "c"))
        self.assertEqual([i.id for i in second], ["c"])

    def test_survives_a_round_trip_to_disk(self):
        state = State(self.path)
        state.record("test-co", KIND_PAGE_COMMENT, ["a"])
        state.save()
        reloaded = State(self.path)
        self.assertFalse(reloaded.is_new("test-co", KIND_PAGE_COMMENT, "a"))
        self.assertTrue(reloaded.is_new("test-co", KIND_PAGE_COMMENT, "z"))

    def test_corrupt_state_file_does_not_crash(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        state = State(self.path)
        self.assertTrue(state.is_new("test-co", KIND_PAGE_COMMENT, "a"))

    def test_seen_ids_are_capped(self):
        from fbmonitor.state import MAX_SEEN_IDS
        state = State(self.path)
        state.record("test-co", KIND_PAGE_COMMENT,
                     [str(i) for i in range(MAX_SEEN_IDS + 500)])
        stored = state._bucket("test-co", KIND_PAGE_COMMENT)["seen_ids"]
        self.assertEqual(len(stored), MAX_SEEN_IDS)
        # The newest must survive; the oldest are the ones to drop.
        self.assertIn(str(MAX_SEEN_IDS + 499), stored)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "accounts.yaml"

    def tearDown(self):
        self.dir.cleanup()

    def test_loads_multiple_accounts(self):
        self.path.write_text(
            "accounts:\n"
            "  - name: Republic of Barbers\n"
            "    token_env: ROB\n"
            "    facebook_page_id: 106828815133188\n"
            "  - name: Raising Thinkers\n"
            "    token_env: RT\n"
            "    facebook_page_id: 222\n"
        )
        accounts = load_accounts(self.path)
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0].slug, "republic-of-barbers")
        # YAML parses a bare page ID as an int; it must survive as a string.
        self.assertEqual(accounts[0].facebook_page_id, "106828815133188")
        self.assertIsInstance(accounts[0].facebook_page_id, str)

    def test_ad_account_gets_act_prefix(self):
        self.path.write_text(
            "accounts:\n  - name: A\n    token_env: T\n    ad_account_id: 123\n")
        self.assertEqual(load_accounts(self.path)[0].ad_account_ids, ["act_123"])

    def test_accepts_a_list_of_ad_accounts_and_dedups(self):
        # This business runs ads from several accounts at once.
        self.path.write_text(
            "accounts:\n  - name: A\n    token_env: T\n"
            "    ad_account_ids: [3178575389134669, act_1192428111245965,"
            " 3178575389134669]\n")
        self.assertEqual(
            load_accounts(self.path)[0].ad_account_ids,
            ["act_3178575389134669", "act_1192428111245965"])

    def test_duplicate_slugs_rejected(self):
        self.path.write_text(
            "accounts:\n"
            "  - name: A\n    slug: same\n    token_env: T\n    facebook_page_id: 1\n"
            "  - name: B\n    slug: same\n    token_env: U\n    facebook_page_id: 2\n"
        )
        with self.assertRaises(ConfigError):
            load_accounts(self.path)

    def test_account_with_no_ids_rejected(self):
        self.path.write_text("accounts:\n  - name: A\n    token_env: T\n")
        with self.assertRaises(ConfigError):
            load_accounts(self.path)

    def test_token_comes_from_the_environment(self):
        acct = account(token_env="A_TEST_TOKEN_VAR")
        os.environ.pop("A_TEST_TOKEN_VAR", None)
        self.assertIsNone(acct.token)
        os.environ["A_TEST_TOKEN_VAR"] = "secret"
        try:
            self.assertEqual(acct.token, "secret")
        finally:
            del os.environ["A_TEST_TOKEN_VAR"]


class TestRunAndDigest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(Path(self.dir.name) / "state.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_token_is_reported_not_crashed(self):
        acct = account(token_env="DEFINITELY_NOT_SET_12345")
        os.environ.pop("DEFINITELY_NOT_SET_12345", None)
        report = run([acct], self.state, token_checker=lambda *a, **k: None)
        self.assertTrue(report.has_problems)
        self.assertIn("no token", report.accounts[0].fatal)
        self.assertIn("DEFINITELY_NOT_SET_12345", render_text(report))

    def test_digest_says_so_when_nothing_is_new(self):
        report = run([], self.state, token_checker=lambda *a, **k: None)
        self.assertIn("Nothing new", render_text(report))

    def test_json_output_is_valid(self):
        acct = account(token_env="DEFINITELY_NOT_SET_12345")
        report = run([acct], self.state, token_checker=lambda *a, **k: None)
        payload = json.loads(render_json(report))
        self.assertEqual(payload["total_new"], 0)
        self.assertEqual(payload["accounts"][0]["slug"], "test-co")


class FakeSession:
    """Captures outgoing requests so we can assert on what Graph would see."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, dict(params or {})))
        payload = self.pages.pop(0)

        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return payload

        return Resp()


class TestPagingSendsOneToken(unittest.TestCase):
    def test_cursor_url_does_not_duplicate_the_token(self):
        # Graph's own "next" cursor arrives with access_token already in the
        # query string. Appending a second copy makes Graph reject the call,
        # so every second page of every collector would have failed.
        cursor = ("https://graph.facebook.com/v21.0/100/posts"
                  "?access_token=SECRET&after=CURSOR123&limit=25")
        session = FakeSession([
            {"data": [{"id": "1"}], "paging": {"next": cursor}},
            {"data": [{"id": "2"}]},
        ])
        client = GraphClient("SECRET", session=session)
        items = list(client.paginate("100/posts", {"limit": 25}, max_pages=5))

        self.assertEqual([i["id"] for i in items], ["1", "2"])
        second_url, second_params = session.requests[1]
        self.assertNotIn("?", second_url, "query must be rebuilt, not appended to")
        self.assertEqual(second_params["access_token"], "SECRET")
        self.assertEqual(second_params["after"], "CURSOR123",
                         "the cursor itself must survive the rebuild")

    def test_refuses_to_follow_an_off_host_cursor(self):
        # A "next" pointing elsewhere would send the token to that host.
        session = FakeSession([
            {"data": [{"id": "1"}],
             "paging": {"next": "https://evil.example.com/steal?access_token=S"}},
        ])
        client = GraphClient("SECRET", session=session)
        items = list(client.paginate("100/posts", max_pages=5))
        self.assertEqual(len(items), 1)
        self.assertEqual(len(session.requests), 1, "must not call the other host")


class TestErrorsDoNotLeakTokens(unittest.TestCase):
    def test_error_path_is_stripped_of_the_query_string(self):
        class ErrSession:
            def get(self, url, params=None, timeout=None):
                class Resp:
                    status_code = 400

                    @staticmethod
                    def json():
                        return {"error": {"message": "bad", "code": 100}}

                return Resp()

        client = GraphClient("SUPERSECRET", session=ErrSession())
        with self.assertRaises(GraphError) as ctx:
            client.get("100/posts", {"limit": 1})
        self.assertNotIn("SUPERSECRET", ctx.exception.path)
        self.assertNotIn("SUPERSECRET", str(ctx.exception))


class TestAdsNeedAUserToken(unittest.TestCase):
    """A Page token cannot read an ad account.

    ads_read is a user-level permission, so /act_<id>/ads with a Page token
    returns "(#100) Unsupported get request". The monitor was built assuming
    one token per business covered everything; it does not, and the symptom
    was ad comments silently reporting zero -- the one source that matters
    most failing in the way least likely to be noticed.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(Path(self.dir.name) / "state.json")
        os.environ["PAGE_TOKEN_T"] = "page-token"
        os.environ.pop("ADS_TOKEN_T", None)

    def tearDown(self):
        self.dir.cleanup()
        os.environ.pop("PAGE_TOKEN_T", None)
        os.environ.pop("ADS_TOKEN_T", None)

    def test_says_so_when_no_ads_token_is_configured(self):
        acct = account(token_env="PAGE_TOKEN_T", ads_token_env=None)
        report = run([acct], self.state, sources=[KIND_AD_COMMENT],
                     token_checker=lambda *a, **k: None)
        problems = report.accounts[0].problems
        self.assertEqual(len(problems), 1)
        self.assertIn("Page token cannot read an ad account",
                      problems[0].skipped_reason)
        # Naming the fix in the message is the point -- this failure is
        # otherwise indistinguishable from "you have no ad comments".
        self.assertIn("ads_token_env", problems[0].skipped_reason)

    def test_ad_collector_gets_the_ads_token_others_get_the_page_token(self):
        os.environ["ADS_TOKEN_T"] = "ads-token"
        seen = {}

        def spy(kind):
            def collect(client, account, **_):
                seen[kind] = client._token
                return CollectionResult(kind=kind, account=account.slug)
            return collect

        acct = account(token_env="PAGE_TOKEN_T", ads_token_env="ADS_TOKEN_T")
        from fbmonitor import monitor as monitor_module
        original = dict(monitor_module.COLLECTORS)
        monitor_module.COLLECTORS.update({
            KIND_AD_COMMENT: spy(KIND_AD_COMMENT),
            KIND_PAGE_COMMENT: spy(KIND_PAGE_COMMENT),
        })
        try:
            run([acct], self.state,
                sources=[KIND_AD_COMMENT, KIND_PAGE_COMMENT],
                token_checker=lambda *a, **k: None)
        finally:
            monitor_module.COLLECTORS.clear()
            monitor_module.COLLECTORS.update(original)

        self.assertEqual(seen[KIND_AD_COMMENT], "ads-token")
        self.assertEqual(seen[KIND_PAGE_COMMENT], "page-token")

    def test_config_reads_the_ads_token_from_its_own_env_var(self):
        acct = account(token_env="PAGE_TOKEN_T", ads_token_env="ADS_TOKEN_T")
        self.assertIsNone(acct.ads_token)
        os.environ["ADS_TOKEN_T"] = "ads-token"
        self.assertEqual(acct.ads_token, "ads-token")


class TestVerboseDoesNotLeakTokens(unittest.TestCase):
    def test_urllib3_request_logging_is_suppressed(self):
        # urllib3 logs each request's full URL at DEBUG, and Graph puts the
        # access token in the query string -- so --verbose printed live
        # credentials to the console and into digest.txt.
        import logging as logging_module

        from fbmonitor.cli import main

        logging_module.getLogger("urllib3").setLevel(logging_module.NOTSET)
        main(["--config", "/nonexistent-config-for-test.yaml", "--verbose"])
        self.assertGreaterEqual(
            logging_module.getLogger("urllib3").level, logging_module.WARNING,
            "urllib3 must not log request URLs; they carry the access token")


class TestTelegramDelivery(unittest.TestCase):
    """Telegram differs from Slack and Discord in ways that fail quietly."""

    URL = "https://api.telegram.org/bot123:ABC/sendMessage?chat_id=-100999"

    def _report(self, text="absolute rip off"):
        item = Item(kind=KIND_AD_COMMENT, id="i1", account="test-co",
                    created_time=datetime(2026, 9, 9, tzinfo=timezone.utc),
                    author="Someone", text=text)
        ar = AccountReport(account=account(name="Republic of Barbers"))
        ar.results = [CollectionResult(kind=KIND_AD_COMMENT, account="test-co",
                                       items=triage.apply([item]))]
        return Report(accounts=[ar])

    def test_chat_id_moves_from_the_url_into_the_body(self):
        # Telegram reads parameters from the JSON body and ignores the query
        # string, so a chat_id left in the URL is dropped and the call fails.
        payload, target = notify._payload_for(self.URL, "hello")
        self.assertEqual(payload["chat_id"], "-100999")
        self.assertNotIn("chat_id", target)
        self.assertTrue(target.endswith("/sendMessage"))

    def test_missing_chat_id_says_what_to_add(self):
        with self.assertRaises(notify.NotifyError) as ctx:
            notify._payload_for("https://api.telegram.org/bot123:ABC/sendMessage",
                                "hello")
        self.assertIn("chat_id", str(ctx.exception))

    def test_emphasis_is_stripped_so_names_cannot_break_the_message(self):
        # A customer called "some_one" would make Telegram reject the whole
        # message as malformed markup, losing a complaint entirely.
        payload, _ = notify._payload_for(self.URL, "*bold* and _italic_ text")
        self.assertEqual(payload["text"], "bold and italic text")

    def test_a_refusal_returned_as_http_200_is_not_treated_as_success(self):
        class Refusing:
            @staticmethod
            def post(url, json=None, timeout=None):
                class Resp:
                    status_code = 200
                    text = '{"ok": false, "description": "chat not found"}'

                    @staticmethod
                    def json():
                        return {"ok": False, "description": "chat not found"}
                return Resp()

        with self.assertRaises(notify.NotifyError) as ctx:
            notify.send(self._report(), self.URL, session=Refusing())
        self.assertIn("chat not found", str(ctx.exception))

    def test_a_genuine_success_passes(self):
        sent = {}

        class Accepting:
            @staticmethod
            def post(url, json=None, timeout=None):
                sent.update(json)

                class Resp:
                    status_code = 200
                    text = '{"ok": true}'

                    @staticmethod
                    def json():
                        return {"ok": True}
                return Resp()

        notify.send(self._report(), self.URL, session=Accepting())
        self.assertEqual(sent["chat_id"], "-100999")
        self.assertIn("rip off", sent["text"])


class TestTokenExpiryWarning(unittest.TestCase):
    """The ads token dies after ~60 days, and dies silently.

    When it expires, ad comments simply stop being reported: the run still
    succeeds and the channel stays quiet, which is what this tool uses to
    mean "nothing wrong". Warning in advance is the only thing that stops a
    silent expiry looking exactly like good news.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(Path(self.dir.name) / "state.json")

    def tearDown(self):
        self.dir.cleanup()

    def _client(self, expires_in_days=None):
        expires_at = 0
        if expires_in_days is not None:
            when = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            expires_at = int(when.timestamp())
        return FakeGraph({"debug_token": {"data": {"expires_at": expires_at}}})

    def test_reads_the_remaining_days(self):
        # Truncated rather than rounded, so a token with 29 days and 23
        # hours left reports 29. For a deadline warning that errs the safe
        # way: early rather than late.
        self.assertEqual(
            tokens.days_until_expiry(self._client(30), "tok"), 29)

    def test_zero_expiry_means_never_expires(self):
        # Graph reports a non-expiring token as 0, the same as an absent
        # field. Treating that as "expired today" would cry wolf forever.
        self.assertIsNone(tokens.days_until_expiry(self._client(None), "tok"))

    def test_silent_while_the_deadline_is_far_off(self):
        self.assertIsNone(tokens.warning_for(45, label="ads token"))

    def test_warns_inside_each_threshold(self):
        for days in (14, 7, 3, 1):
            self.assertIsNotNone(tokens.warning_for(days, label="ads token"),
                                 f"expected a warning at {days} days")

    def test_says_what_the_silence_will_look_like(self):
        # The message has to explain the symptom, because the symptom is
        # nothing happening.
        warning = tokens.warning_for(3, label="ads token")
        self.assertIn("ad comments stop being reported", warning)
        self.assertIn("nothing looks wrong", warning)

    def test_an_expired_token_is_stated_in_the_past_tense(self):
        warning = tokens.warning_for(0, label="ads token")
        self.assertIn("has expired", warning)

    def test_an_uninspectable_token_is_reported_not_swallowed(self):
        graph = FakeGraph({}, errors={"debug_token": GraphError("nope", code=190)})
        warning = tokens.check(graph, "tok", label="ads token")
        self.assertIn("could not check", warning)

    def test_the_warning_reaches_the_report_and_the_chat(self):
        acct = account(token_env="PAGE_T", ads_token_env="ADS_T",
                       disabled_sources=list(KIND_ORDER))
        os.environ["PAGE_T"] = "page"
        os.environ["ADS_T"] = "ads"
        try:
            report = run([acct], self.state,
                         token_checker=lambda *a, **k: "the ads token expires in 3 days")
        finally:
            os.environ.pop("PAGE_T", None)
            os.environ.pop("ADS_T", None)

        self.assertIn("expires in 3 days", report.warnings[0])
        self.assertTrue(report.has_problems)
        # It must survive the quiet rule, or the alert never arrives.
        self.assertTrue(notify.should_send(report, reported_problems=""))
        self.assertIn("Action needed", notify.render_chat(report))

    def test_checked_once_a_day_not_every_run(self):
        calls = []

        def counting(*a, **k):
            calls.append(1)
            return None

        acct = account(token_env="PAGE_T", ads_token_env="ADS_T",
                       disabled_sources=list(KIND_ORDER))
        os.environ["PAGE_T"] = "page"
        os.environ["ADS_T"] = "ads"
        try:
            for _ in range(3):
                run([acct], self.state, token_checker=counting)
        finally:
            os.environ.pop("PAGE_T", None)
            os.environ.pop("ADS_T", None)
        self.assertEqual(len(calls), 1,
                         "an answer measured in weeks needs one call a day")

    def test_the_warning_persists_between_daily_checks(self):
        acct = account(token_env="PAGE_T", ads_token_env="ADS_T",
                       disabled_sources=list(KIND_ORDER))
        os.environ["PAGE_T"] = "page"
        os.environ["ADS_T"] = "ads"
        try:
            first = run([acct], self.state,
                        token_checker=lambda *a, **k: "expires in 2 days")
            second = run([acct], self.state,
                         token_checker=lambda *a, **k: "should not be called")
        finally:
            os.environ.pop("PAGE_T", None)
            os.environ.pop("ADS_T", None)
        self.assertIn("expires in 2 days", first.warnings[0])
        # Without this the warning would vanish for 24 hours after showing
        # once, which is when it matters most.
        self.assertIn("expires in 2 days", second.warnings[0])


class TestExitCodeMeansSomething(unittest.TestCase):
    """Unavailable is not the same as broken.

    Visitor posts are switched off on both Pages and Instagram is not
    granted -- standing facts that report identically forever. Counting
    those as failures pinned the exit code to 1 permanently, so it could no
    longer distinguish a healthy run from a broken one. On Windows that code
    is LastTaskResult, the only health signal the scheduler gives you.
    """

    def _report(self, **kwargs):
        ar = AccountReport(account=account())
        ar.results = [CollectionResult(kind=KIND_PAGE_COMMENT,
                                       account="test-co", **kwargs)]
        return Report(accounts=[ar])

    def test_an_unavailable_source_is_not_a_failure(self):
        report = self._report(
            skipped_reason="visitor posts unavailable -- turned off on the Page")
        self.assertFalse(report.has_problems,
                         "a switched-off feature must not read as unhealthy")

    def test_an_unavailable_source_is_still_shown(self):
        # Not a failure, but the operator should still see the gap.
        report = self._report(skipped_reason="no instagram_user_id configured")
        self.assertTrue(report.has_unavailable_sources)
        self.assertIn("Not checked", render_text(report))

    def test_a_real_error_is_a_failure(self):
        report = self._report(error="(#190) Access token has expired")
        self.assertTrue(report.has_problems)

    def test_a_missing_token_is_a_failure(self):
        ar = AccountReport(account=account())
        ar.fatal = "no token -- set $ROB_PAGE_TOKEN"
        self.assertTrue(Report(accounts=[ar]).has_problems)

    def test_an_expiring_token_is_a_failure(self):
        report = Report(warnings=["the ads token expires in 3 days"])
        self.assertTrue(report.has_problems)

    def test_a_clean_run_with_a_standing_gap_exits_zero(self):
        # The exact case on the live box: nothing new, visitor posts off.
        report = self._report(skipped_reason="visitor posts unavailable")
        self.assertEqual(report.total_new, 0)
        self.assertFalse(report.has_problems)


class TestStandingProblemsAreNotRepeated(unittest.TestCase):
    """A fault that recurs unchanged must be announced once, not forever.

    The live install posted to Telegram on a run with zero new items,
    because an Instagram permission error recurs on every run. At a
    fifteen-minute interval that is ~96 identical alerts a day about
    something that will never change -- and a muted channel costs the
    complaint that arrives next week.
    """

    def _report(self, error=None, items=()):
        ar = AccountReport(account=account(name="Republic of Barbers"))
        ar.results = [CollectionResult(
            kind=KIND_AD_COMMENT, account="test-co",
            items=triage.apply(list(items)), error=error)]
        return Report(accounts=[ar])

    def _item(self):
        return Item(kind=KIND_AD_COMMENT, id="i1", account="test-co",
                    created_time=datetime(2026, 9, 9, tzinfo=timezone.utc),
                    author="Someone", text="absolute rip off")

    def test_a_new_problem_is_announced(self):
        report = self._report(error="(#230) Requires instagram_manage_messages")
        self.assertTrue(notify.should_send(report, reported_problems=""))

    def test_the_same_problem_is_not_announced_again(self):
        report = self._report(error="(#230) Requires instagram_manage_messages")
        signature = notify.problem_signature(report)
        self.assertFalse(
            notify.should_send(report, reported_problems=signature),
            "an unchanged standing fault must not re-alert every run")

    def test_a_changed_problem_is_announced(self):
        first = self._report(error="(#230) Requires instagram_manage_messages")
        worse = self._report(error="(#190) Access token has expired")
        self.assertTrue(notify.should_send(
            worse, reported_problems=notify.problem_signature(first)))

    def test_new_items_are_always_announced_even_with_a_standing_problem(self):
        report = self._report(error="(#230) still broken", items=[self._item()])
        signature = notify.problem_signature(report)
        self.assertTrue(
            notify.should_send(report, reported_problems=signature),
            "a real comment must never be suppressed by problem dedup")

    def test_a_missing_permission_is_a_skip_not_an_error(self):
        # Code 230 is Instagram's missing-permission code. Classifying it as
        # an error made it a recurring alert instead of a standing note.
        self.assertTrue(GraphError("nope", code=230).is_permission_error)

    def test_state_remembers_what_was_announced(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            state = State(path)
            self.assertEqual(state.reported_problems(), "")
            state.set_reported_problems("something:broken")
            state.save()
            self.assertEqual(State(path).reported_problems(), "something:broken")


class TestNotifySelfTest(unittest.TestCase):
    """--test-notify proves the alarm works rather than assuming it."""

    def tearDown(self):
        os.environ.pop("FBMONITOR_WEBHOOK_URL", None)

    def test_reports_a_config_error_when_no_webhook_is_set(self):
        os.environ.pop("FBMONITOR_WEBHOOK_URL", None)
        from fbmonitor.cli import main
        self.assertEqual(main(["--test-notify"]), 2)

    def test_returns_nonzero_when_delivery_fails(self):
        os.environ["FBMONITOR_WEBHOOK_URL"] = (
            "https://api.telegram.org/bot1:A/sendMessage")  # no chat_id
        from fbmonitor.cli import main
        self.assertEqual(main(["--test-notify"]), 1,
                         "a broken webhook must not report success")

    def test_sends_a_sample_that_survives_the_quiet_rule(self):
        # should_send() suppresses runs with nothing new, so the sample has
        # to look like a real finding or the test would post nothing and
        # still claim success.
        from fbmonitor import cli
        captured = {}

        def fake_send(report, url, **kw):
            captured["message"] = notify.render_chat(report)

        original = notify.send
        notify.send = fake_send
        os.environ["FBMONITOR_WEBHOOK_URL"] = "https://hooks.slack.com/x"
        try:
            self.assertEqual(cli.main(["--test-notify"]), 0)
        finally:
            notify.send = original
        self.assertIn("test alert", captured["message"].lower())


class TestWindowsEncoding(unittest.TestCase):
    """Windows defaults to cp1252, not UTF-8.

    Every one of these was a real failure found by running on the target
    machine rather than the machine the code was written on.
    """

    PACKAGE = Path(__file__).resolve().parent.parent / "fbmonitor"

    def test_no_file_io_without_an_explicit_encoding(self):
        # config.py read accounts.yaml with the locale encoding. The shipped
        # accounts.example.yaml contains an em-dash, so on Windows the tool
        # refused to start with a misleading config error.
        offenders = []
        for source in self.PACKAGE.rglob("*.py"):
            for number, line in enumerate(
                    source.read_text(encoding="utf-8").splitlines(), 1):
                if "encoding=" in line or line.lstrip().startswith("#"):
                    continue
                if ("read_text()" in line or "write_text(" in line
                        or "fdopen(" in line):
                    offenders.append(f"{source.name}:{number}")
        self.assertEqual(offenders, [],
                         "file I/O must name its encoding, or Windows uses cp1252")

    def test_digest_survives_a_console_that_cannot_encode_it(self):
        # The digest marks severity with characters cp1252 has no mapping
        # for, so printing crashed on exactly the runs that found something.
        import io

        from fbmonitor.digest import render_text

        item = Item(kind=KIND_AD_COMMENT, id="i1", account="test-co",
                    created_time=datetime(2026, 9, 9, tzinfo=timezone.utc),
                    author="Someone", text="absolute rip off")
        ar = AccountReport(account=account())
        ar.results = [CollectionResult(kind=KIND_AD_COMMENT, account="test-co",
                                       items=triage.apply([item]))]
        text = render_text(Report(accounts=[ar]))

        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                   errors="replace")
        console.write(text)   # must not raise
        console.flush()

    def test_shipped_config_is_readable_as_utf8(self):
        root = self.PACKAGE.parent
        for name in ("accounts.example.yaml", ".env.example"):
            path = root / name
            if path.exists():
                path.read_text(encoding="utf-8")


class TestChatNotification(unittest.TestCase):
    def _report(self, items=(), fatal=None, error=None):
        ar = AccountReport(account=account(name="Republic of Barbers"))
        ar.fatal = fatal
        ar.results = [CollectionResult(
            kind=KIND_AD_COMMENT, account="test-co",
            items=triage.apply(list(items)), error=error)]
        return Report(accounts=[ar])

    def _item(self, text):
        return Item(kind=KIND_AD_COMMENT, id="i1", account="test-co",
                    created_time=datetime(2026, 9, 9, tzinfo=timezone.utc),
                    author="Someone", text=text)

    def test_stays_silent_when_nothing_is_new(self):
        # A channel that pings every 15 minutes with "no change" gets muted,
        # and a muted channel is worse than none.
        self.assertFalse(notify.should_send(self._report()))

    def test_speaks_when_there_is_something_new(self):
        self.assertTrue(notify.should_send(self._report([self._item("hi")])))

    def test_speaks_when_a_source_broke(self):
        # A dead token must not read as a quiet day.
        self.assertTrue(notify.should_send(self._report(error="token expired")))
        self.assertTrue(notify.should_send(self._report(fatal="no token")))

    def test_stays_silent_for_a_merely_unconfigured_source(self):
        # skipped_reason repeats identically forever; it is not news.
        ar = AccountReport(account=account())
        ar.results = [CollectionResult(
            kind=KIND_AD_COMMENT, account="test-co",
            skipped_reason="no instagram_user_id configured")]
        self.assertFalse(notify.should_send(Report(accounts=[ar])))

    def test_complaint_leads_the_message(self):
        report = self._report([self._item("absolute rip off, avoid")])
        message = notify.render_chat(report)
        self.assertTrue(message.startswith("🚨"), message[:40])
        self.assertIn("possible complaint", message)

    def test_message_is_capped_for_discord(self):
        many = [Item(kind=KIND_AD_COMMENT, id=f"i{n}", account="test-co",
                     created_time=datetime(2026, 9, 9, tzinfo=timezone.utc),
                     author=f"Person {n}", text="x" * 200) for n in range(60)]
        message = notify.render_chat(self._report(many))
        self.assertLessEqual(len(message), notify.MAX_MESSAGE + 40)
        self.assertIn("truncated", message)

    def test_uses_the_field_name_each_provider_expects(self):
        discord, _ = notify._payload_for(
            "https://discord.com/api/webhooks/1/abc", "hello")
        slack, _ = notify._payload_for(
            "https://hooks.slack.com/services/T/B/x", "hello")
        self.assertEqual(discord, {"content": "hello"})
        self.assertEqual(slack, {"text": "hello"})

    def test_reports_a_rejected_post(self):
        class Rejecting:
            @staticmethod
            def post(url, json=None, timeout=None):
                class Resp:
                    status_code = 404
                    text = "no such webhook"
                return Resp()

        with self.assertRaises(notify.NotifyError) as ctx:
            notify.send(self._report([self._item("hi")]),
                        "https://discord.com/api/webhooks/1/abc",
                        session=Rejecting())
        self.assertIn("404", str(ctx.exception))

    def test_accepts_discords_empty_204(self):
        posted = {}

        class Accepting:
            @staticmethod
            def post(url, json=None, timeout=None):
                posted["body"] = json

                class Resp:
                    status_code = 204
                    text = ""
                return Resp()

        notify.send(self._report([self._item("hi")]),
                    "https://discord.com/api/webhooks/1/abc",
                    session=Accepting())
        self.assertIn("content", posted["body"])

    def test_describes_target_without_leaking_the_secret_path(self):
        described = notify.describe_target(
            "https://discord.com/api/webhooks/123/SECRETTOKEN")
        self.assertEqual(described, "Discord")
        self.assertNotIn("SECRETTOKEN", described)


class TestGraphClientIsReadOnly(unittest.TestCase):
    def test_client_exposes_no_write_method(self):
        # The notify-only guarantee is structural: if someone adds a write
        # method later, this test is the thing that objects.
        public = {n for n in vars(GraphClient) if not n.startswith("_")}
        self.assertEqual(public, {"get", "paginate"})
        # And nothing anywhere in the package issues a non-GET request.
        package = Path(__file__).resolve().parent.parent / "fbmonitor"
        for source in package.rglob("*.py"):
            body = source.read_text(encoding="utf-8")
            for verb in ("session.post", "session.put", "session.delete",
                         "requests.post", "requests.put", "requests.delete"):
                self.assertNotIn(verb, body, f"{source.name} can write to Graph")

    def test_token_is_required(self):
        with self.assertRaises(ValueError):
            GraphClient("")


if __name__ == "__main__":
    unittest.main(verbosity=2)
