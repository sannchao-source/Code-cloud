"""Tests for the monitor. No network: Graph is faked at the client seam."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fbmonitor.collectors.ads import _story_ids, collect_ad_comments
from fbmonitor.collectors.facebook import (
    collect_messenger,
    collect_page_comments,
    collect_reviews,
)
from fbmonitor.collectors.instagram import collect_instagram_comments
from fbmonitor.config import Account, ConfigError, load_accounts
from fbmonitor.digest import render_json, render_text
from fbmonitor.graph import GraphClient, GraphError
from fbmonitor.models import KIND_PAGE_COMMENT, Item, parse_time
from fbmonitor.monitor import run
from fbmonitor.state import State


class FakeGraph:
    """Stands in for GraphClient. Records calls; returns canned payloads."""

    def __init__(self, responses: dict, errors: dict | None = None):
        self.responses = responses
        self.errors = errors or {}
        self.calls: list[str] = []

    def get(self, path, params=None):
        self.calls.append(path)
        if path in self.errors:
            raise self.errors[path]
        return self.responses.get(path, {"data": []})

    def paginate(self, path, params=None, *, max_pages=10):
        return iter(self.get(path, params).get("data", []))


def account(**kw):
    base = dict(name="Test Co", slug="test-co", token_env="TEST_TOKEN",
                facebook_page_id="100", instagram_user_id="200",
                ad_account_id="act_300")
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
        ads = [
            {"id": "a1", "name": "Spring", "creative": {
                "effective_object_story_id": "100_777"}},
            {"id": "a2", "name": "Spring copy", "creative": {
                "effective_object_story_id": "100_777"}},
            {"id": "a3", "name": "No creative"},
        ]
        stories = _story_ids(ads)
        self.assertEqual(list(stories), ["100_777"])

    def test_reads_comments_on_a_dark_post(self):
        graph = FakeGraph({
            "act_300/ads": {"data": [{
                "id": "a1", "name": "Father's Day",
                "creative": {"effective_object_story_id": "100_777"}}]},
            "100_777/comments": {"data": [{
                "id": "ac1", "message": "How much?",
                "from": {"name": "Chris"},
                "created_time": "2026-09-08T08:00:00+0000"}]},
        })
        result = collect_ad_comments(graph, account())
        self.assertEqual(len(result.items), 1)
        self.assertIn("Father's Day", result.items[0].context)

    def test_unreadable_story_does_not_fail_the_collector(self):
        graph = FakeGraph(
            {"act_300/ads": {"data": [{
                "id": "a1", "name": "X",
                "creative": {"effective_object_story_id": "100_777"}}]}},
            errors={"100_777/comments": GraphError("nope", code=100)},
        )
        result = collect_ad_comments(graph, account())
        self.assertTrue(result.ok)
        self.assertIn("could be read", result.skipped_reason)


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
        self.path.write_text("{ this is not json")
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
        self.assertEqual(load_accounts(self.path)[0].ad_account_id, "act_123")

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
        report = run([acct], self.state)
        self.assertTrue(report.has_problems)
        self.assertIn("no token", report.accounts[0].fatal)
        self.assertIn("DEFINITELY_NOT_SET_12345", render_text(report))

    def test_digest_says_so_when_nothing_is_new(self):
        report = run([], self.state)
        self.assertIn("Nothing new", render_text(report))

    def test_json_output_is_valid(self):
        acct = account(token_env="DEFINITELY_NOT_SET_12345")
        report = run([acct], self.state)
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


class TestGraphClientIsReadOnly(unittest.TestCase):
    def test_client_exposes_no_write_method(self):
        # The notify-only guarantee is structural: if someone adds a write
        # method later, this test is the thing that objects.
        public = {n for n in vars(GraphClient) if not n.startswith("_")}
        self.assertEqual(public, {"get", "paginate"})
        # And nothing anywhere in the package issues a non-GET request.
        package = Path(__file__).resolve().parent.parent / "fbmonitor"
        for source in package.rglob("*.py"):
            body = source.read_text()
            for verb in ("session.post", "session.put", "session.delete",
                         "requests.post", "requests.put", "requests.delete"):
                self.assertNotIn(verb, body, f"{source.name} can write to Graph")

    def test_token_is_required(self):
        with self.assertRaises(ValueError):
            GraphClient("")


if __name__ == "__main__":
    unittest.main(verbosity=2)
