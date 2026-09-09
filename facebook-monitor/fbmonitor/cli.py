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
        _notify(report)

    # Exit 1 when something could not be checked, so a scheduled run can
    # surface a broken token instead of looking like a quiet day.
    return 1 if report.has_problems else 0


def _notify(report) -> None:
    """Post to chat. A delivery failure must not lose the digest, which has
    already been printed by the time we get here."""
    url = os.environ.get("FBMONITOR_WEBHOOK_URL", "")
    if not url:
        print("--notify given but $FBMONITOR_WEBHOOK_URL is not set",
              file=sys.stderr)
        return
    if not notify.should_send(report):
        return
    try:
        notify.send(report, url)
    except notify.NotifyError as exc:
        print(f"chat delivery failed: {exc}", file=sys.stderr)
    else:
        print(f"posted to {notify.describe_target(url)}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
