"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import ConfigError, load_accounts
from .digest import render_json, render_text
from . import notify
from .monitor import run
from .models import KIND_ORDER
from .state import State


# Overridden in tests so they never reach the network.
check_tokens_client_factory = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fbmonitor",
        description=(
            "Read new Facebook/Instagram comments, DMs, visitor posts and "
            "recommendations. Read-only: it never replies or posts."
        ),
    )
    parser.add_argument("-c", "--config", default="accounts.yaml",
                        help="account config file (default: accounts.yaml)")
    parser.add_argument("-s", "--state", default="state.json",
                        help="seen-item state file (default: state.json)")
    parser.add_argument("-f", "--format", choices=["text", "json"], default="text",
                        help="output format (default: text)")
    parser.add_argument("--source", action="append", choices=KIND_ORDER,
                        metavar="KIND", dest="sources",
                        help="limit to one source; repeatable. One of: "
                             + ", ".join(KIND_ORDER))
    parser.add_argument("--account", action="append", dest="only_accounts",
                        metavar="SLUG",
                        help="limit to one account by slug; repeatable")
    parser.add_argument("--preview", action="store_true",
                        help="show what would be reported without marking it "
                             "seen, so the next real run still reports it")
    parser.add_argument("--api-version", default=None,
                        help="Graph API version, e.g. v21.0")
    parser.add_argument("--notify", action="store_true",
                        help="post the digest to the chat webhook in "
                             "$FBMONITOR_WEBHOOK_URL, but only when there is "
                             "something new or something broke")
    parser.add_argument("--check-tokens", action="store_true",
                        help="test every configured token and report which "
                             "work, without printing any of them")
    parser.add_argument("--test-notify", action="store_true",
                        help="send a sample alert to the chat webhook and "
                             "exit, to prove delivery works before relying "
                             "on it")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _force_utf8_output() -> None:
    """Stop a Windows console killing the run.

    The digest uses ‼, ⚠ and ↳ to mark severity. A default Windows console
    is cp1252, which cannot encode them, so printing raised
    UnicodeEncodeError -- and only on runs that actually found something,
    which is the worst possible time to fail. errors="replace" means a
    console that genuinely cannot render a glyph shows "?" instead of
    taking the run down with it.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Not a real stream (captured in tests, or redirected oddly).
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # urllib3 logs each request's full URL at DEBUG, and Graph carries the
    # access token in the query string -- so --verbose printed live
    # credentials to the console and into digest.txt. Our own debug lines
    # carry everything useful for diagnosis without the secret.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.test_notify:
        return _test_notify()

    if args.check_tokens:
        return _check_tokens(args.config, args.api_version,
                             client_factory=check_tokens_client_factory)

    try:
        accounts = load_accounts(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.only_accounts:
        wanted = set(args.only_accounts)
        accounts = [a for a in accounts if a.slug in wanted]
        if not accounts:
            print(f"no account matched {sorted(wanted)}", file=sys.stderr)
            return 2

    state = State(args.state)
    report = run(
        accounts,
        state,
        sources=args.sources,
        api_version=args.api_version,
        record=not args.preview,
    )

    if not args.preview:
        state.save()

    if args.format == "json":
        print(render_json(report))
    else:
        print(render_text(report))

    if args.notify:
        _notify(report, state, record=not args.preview)

    # Exit 1 when something could not be checked, so a scheduled run can
    # surface a broken token instead of looking like a quiet day.
    return 1 if report.has_problems else 0


def _check_tokens(config_path: str, api_version: str | None,
                  client_factory=None) -> int:
    """Say which tokens still work, and never print one.

    A dead token makes every source report as failing, which looks like a
    dozen unrelated faults rather than one cause. Naming the dead token
    turns that into a single answer.
    """
    from .graph import GraphClient, GraphError

    # Injected so the tests never reach the network.
    build = client_factory or (lambda token, **kw: GraphClient(token, **kw))

    try:
        accounts = load_accounts(config_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    kwargs = {"api_version": api_version} if api_version else {}
    checked: set[str] = set()
    failures = 0

    for account in accounts:
        for label, token in (("page token", account.token),
                             ("ads token", account.ads_token)):
            env = (account.token_env if label == "page token"
                   else account.ads_token_env)
            if not env or env in checked:
                continue
            checked.add(env)

            if not token:
                print(f"  {env:32} NOT SET")
                failures += 1
                continue
            try:
                who = build(token, **kwargs).get("me", {"fields": "id,name"})
            except GraphError as exc:
                # The code is what distinguishes an expiry from a block, and
                # they need completely different fixes.
                code = f" (code {exc.code})" if exc.code else ""
                print(f"  {env:32} DEAD{code}: {exc}")
                failures += 1
            else:
                print(f"  {env:32} OK -> {who.get('name', who.get('id', '?'))}")

    if failures:
        print(f"\n{failures} token(s) need attention. Regenerating them is "
              "step 4 of README.md.", file=sys.stderr)
        return 1
    print("\nAll tokens working.")
    return 0


def _test_notify() -> int:
    """Send one sample alert, so delivery is proven rather than assumed.

    A monitor whose alarm is silently misconfigured is worse than none: it
    buys confidence it has not earned. This makes the alarm testable on
    demand, without waiting for a real complaint to find out.
    """
    from datetime import datetime, timezone

    from .models import KIND_AD_COMMENT, CollectionResult, Item
    from .monitor import AccountReport, Report
    from .config import Account
    from . import triage

    url = os.environ.get("FBMONITOR_WEBHOOK_URL", "")
    if not url:
        print("FBMONITOR_WEBHOOK_URL is not set", file=sys.stderr)
        return 2

    sample = Item(
        kind=KIND_AD_COMMENT, id="test", account="test",
        created_time=datetime.now(timezone.utc),
        author="Test Delivery",
        text=("This is a test alert. If you can read this, complaints on "
              "your live ads will reach you here."),
        context="test message — no action needed")
    account = Account(name="Delivery test", slug="test", token_env="UNUSED",
                      facebook_page_id="0")
    report_account = AccountReport(account=account)
    report_account.results = [CollectionResult(
        kind=KIND_AD_COMMENT, account="test", items=triage.apply([sample]))]

    try:
        notify.send(Report(accounts=[report_account]), url)
    except notify.NotifyError as exc:
        print(f"delivery FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"sent a test alert to {notify.describe_target(url)} — "
          "check that it arrived")
    return 0


def _notify(report, state, *, record: bool = True) -> None:
    """Post to chat. A delivery failure must not lose the digest, which has
    already been printed by the time we get here."""
    url = os.environ.get("FBMONITOR_WEBHOOK_URL", "")
    if not url:
        print("--notify given but $FBMONITOR_WEBHOOK_URL is not set",
              file=sys.stderr)
        return
    if not notify.should_send(
            report, reported_problems=state.reported_problems()):
        return
    try:
        notify.send(report, url)
    except notify.NotifyError as exc:
        # Do not record the problems as announced -- the message never
        # arrived, so the next run should still try.
        print(f"chat delivery failed: {exc}", file=sys.stderr)
        return

    print(f"posted to {notify.describe_target(url)}", file=sys.stderr)
    if record:
        state.set_reported_problems(notify.problem_signature(report))
        state.save()


if __name__ == "__main__":
    raise SystemExit(main())
