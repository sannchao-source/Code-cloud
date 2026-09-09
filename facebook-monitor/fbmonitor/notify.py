"""Deliver a digest to a chat channel.

A digest written to a file on a machine nobody logs into is not a monitor,
so the run can post itself to Slack, Discord or Telegram instead. Delivery
is deliberately quiet: a run with nothing new sends nothing at all, because
a channel that pings every fifteen minutes with "no change" is one people
mute within a day -- and a muted channel is worse than no channel.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

import requests

from .models import KIND_LABELS
from .monitor import Report

log = logging.getLogger(__name__)

# Discord hard-caps a message at 2000 characters. Staying under it for
# every provider keeps one code path and one set of surprises.
MAX_MESSAGE = 1800
TIMEOUT = 15


class NotifyError(RuntimeError):
    pass


def should_send(report: Report) -> bool:
    """Only speak when there is something worth interrupting someone for.

    New items qualify. So does a source that broke -- a dead token must not
    look like a quiet day. A source that is merely unconfigured does not,
    since that reports identically on every run forever.
    """
    if report.total_new:
        return True
    return any(
        account.fatal or any(r.error for r in account.results)
        for account in report.accounts
    )


def render_chat(report: Report) -> str:
    """A short, scannable message. The digest file holds the full detail."""
    lines: list[str] = []

    if report.total_complaints:
        lines.append(
            f"🚨 *{report.total_complaints} possible complaint(s)* on live ads "
            "or posts")
    elif report.total_new:
        lines.append(f"*{report.total_new} new item(s)*")

    for account_report in report.accounts:
        items = account_report.new_items
        if account_report.fatal:
            lines.append(f"\n⚠️ *{account_report.account.name}*: "
                         f"{account_report.fatal}")
            continue
        broken = [r for r in account_report.results if r.error]
        if broken:
            for result in broken:
                lines.append(f"\n⚠️ *{account_report.account.name}* — "
                             f"{KIND_LABELS[result.kind]}: {result.error}")
        if not items:
            continue

        lines.append(f"\n*{account_report.account.name}* ({len(items)} new)")
        for item in items:
            marker = {2: "🚨", 1: "❓"}.get(item.severity, "•")
            text = " ".join((item.text or "(no text)").split())
            if len(text) > 160:
                text = text[:159] + "…"
            lines.append(f"{marker} _{item.author}_: {text}")
            if item.context:
                lines.append(f"    ↳ {item.context}")
            if item.permalink:
                lines.append(f"    {item.permalink}")

    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE] + "\n… truncated — see digest.txt"
    return message


def send(report: Report, url: str, *, session=None) -> None:
    """Post the digest to a Slack, Discord or Telegram webhook."""
    if not url:
        raise NotifyError("no webhook URL configured")

    message = render_chat(report)
    payload, target = _payload_for(url, message)
    http = session or requests

    try:
        resp = http.post(target, json=payload, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise NotifyError(f"could not reach the chat webhook: {exc}") from exc

    # Discord answers 204 with no body; Slack answers 200 with "ok".
    if resp.status_code not in (200, 201, 204):
        body = (resp.text or "")[:200]
        raise NotifyError(
            f"chat webhook rejected the message (HTTP {resp.status_code}): {body}")


def _payload_for(url: str, message: str) -> tuple[dict, str]:
    """Each provider wants a different field name for the same string."""
    host = (urlparse(url).hostname or "").lower()

    if "discord.com" in host or "discordapp.com" in host:
        return {"content": message}, url
    if "telegram.org" in host:
        # Telegram takes the chat id in the URL as ...?chat_id=<id>; the
        # message rides in the body.
        return {"text": message, "parse_mode": "Markdown"}, url
    # Slack incoming webhooks, and the many services that copy their shape.
    return {"text": message}, url


def describe_target(url: str) -> str:
    """Name the destination for a log line, without leaking the secret path."""
    host = (urlparse(url).hostname or "unknown").lower()
    if "discord" in host:
        return "Discord"
    if "slack" in host:
        return "Slack"
    if "telegram" in host:
        return "Telegram"
    return host
