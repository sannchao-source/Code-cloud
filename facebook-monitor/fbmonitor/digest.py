"""Render a report for a human to read."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import KIND_LABELS, KIND_ORDER
from .monitor import Report

BULLET = "  •"


def render_text(report: Report, *, width: int = 78) -> str:
    lines: list[str] = []
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"Facebook / Instagram inbox — {stamp}")
    lines.append("=" * width)

    if report.total_new == 0 and not report.has_problems:
        lines.append("")
        lines.append("Nothing new since the last check.")
        return "\n".join(lines)

    for account_report in report.accounts:
        account = account_report.account
        items = account_report.new_items
        lines.append("")
        lines.append(f"{account.name}  ({len(items)} new)")
        lines.append("-" * width)

        if account_report.fatal:
            lines.append(f"  ⚠ {account_report.fatal}")
            continue

        if not items:
            lines.append("  Nothing new.")
        else:
            for kind in KIND_ORDER:
                grouped = [i for i in items if i.kind == kind]
                if not grouped:
                    continue
                lines.append("")
                lines.append(f"  {KIND_LABELS[kind]} ({len(grouped)})")
                for item in grouped:
                    lines.extend(_render_item(item, width))

        problems = account_report.problems
        if problems:
            lines.append("")
            lines.append("  Not checked:")
            for problem in problems:
                reason = problem.error or problem.skipped_reason
                lines.append(f"    - {KIND_LABELS[problem.kind]}: {reason}")

    lines.append("")
    lines.append("=" * width)
    lines.append(
        f"{report.total_new} new item(s). This tool only reads — nothing has "
        "been replied to or posted.")
    return "\n".join(lines)


def _render_item(item, width: int) -> list[str]:
    when = (
        item.created_time.strftime("%d %b %H:%M")
        if item.created_time else "unknown time"
    )
    out = [f"{BULLET} {item.author} · {when}"]
    body = " ".join((item.text or "(no text)").split())
    for chunk in _wrap(body, width - 6):
        out.append(f"      {chunk}")
    if item.context:
        out.append(f"      ↳ {item.context}")
    if item.permalink:
        out.append(f"      {item.permalink}")
    return out


def _wrap(text: str, width: int) -> list[str]:
    if not text:
        return []
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def render_json(report: Report) -> str:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_new": report.total_new,
        "accounts": [
            {
                "name": ar.account.name,
                "slug": ar.account.slug,
                "fatal": ar.fatal,
                "new_items": [i.to_dict() for i in ar.new_items],
                "problems": [
                    {"kind": p.kind, "reason": p.error or p.skipped_reason}
                    for p in ar.problems
                ],
            }
            for ar in report.accounts
        ],
    }
    return json.dumps(payload, indent=2)
