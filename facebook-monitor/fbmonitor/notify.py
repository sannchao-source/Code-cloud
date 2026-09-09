"""Deliver a digest to a chat channel.

A digest written to a file on a machine nobody logs into is not a monitor,
so the run can post itself to Slack, Discord or Telegram instead. Delivery
is deliberately quiet: a run with nothing new sends nothing at all, because
a channel that pings every fifteen minutes with "no change" is one people
mute within a day -- and a muted channel is worse than no channel.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse, urlunparse

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


def problem_signature(report: Report) -> str:
    """A stable fingerprint of everything currently broken."""
    parts: list[str] = list(report.warnings)
    for account in report.accounts:
        if account.fatal:
            parts.append(f"{account.account.slug}:fatal:{account.fatal}")
        for result in account.results:
            if result.error:
                parts.append(f"{account.account.slug}:{result.kind}:{result.error}")
    return "|".join(sorted(parts))


def should_send(report: Report, *, reported_problems: str | None = None) -> bool:
    """Only speak when there is something worth interrupting someone for.

    New items always qualify. A broken source qualifies once -- a dead
    token must not read as a quiet day -- but not on every run thereafter:
    a fault that recurs unchanged every fifteen minutes is a standing
    condition, and a channel that repeats it gets muted, which costs the
    complaint that arrives next week. So a problem is announced when it
    appears or changes, and then goes quiet until it does.

    A source that is merely unconfigured never qualifies at all, since it
    would report identically forever.
    """
    if report.total_new:
        return True
    current = problem_signature(report)
    return bool(current) and current != (reported_problems or "")


def render_chat(report: Report) -> str:
    """A short, scannable message. The digest file holds the full detail."""
    lines: list[str] = []

    for warning in report.warnings:
        lines.append(f"⏳ *Action needed:* {warning}")

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

    # Telegram answers 200 even when it refused the message, putting the
    # real outcome in the body -- so a status check alone would report a
    # silent failure as a success.
    if "telegram.org" in (urlparse(url).hostname or ""):
        try:
            answer = resp.json()
        except ValueError:
            return
        if isinstance(answer, dict) and answer.get("ok") is False:
            raise NotifyError(
                f"Telegram refused the message: "
                f"{answer.get('description', 'no reason given')}")


def _payload_for(url: str, message: str) -> tuple[dict, str]:
    """Each provider wants a different shape for the same string."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if "discord.com" in host or "discordapp.com" in host:
        return {"content": message}, url

    if "telegram.org" in host:
        # Telegram reads its parameters from the JSON body when the request
        # carries one, and ignores the query string -- so a chat_id left in
        # the URL is silently dropped and the call fails. Move it into the
        # body and send a clean URL.
        chat_ids = parse_qs(parsed.query).get("chat_id")
        if not chat_ids:
            raise NotifyError(
                "the Telegram URL needs ?chat_id=<id> on the end, e.g. "
                "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=12345")
        clean = urlunparse(parsed._replace(query="", fragment=""))
        # No parse_mode: the digest carries customer names, and one stray
        # underscore or asterisk in a name makes Telegram reject the whole
        # message as malformed markup. A plain message that arrives beats a
        # formatted one that does not, so the emphasis marks are stripped.
        return {"chat_id": chat_ids[0], "text": _strip_emphasis(message)}, clean

    # Slack incoming webhooks, and the many services that copy their shape.
    return {"text": message}, url


def _strip_emphasis(message: str) -> str:
    """Remove the *bold* and _italic_ markers used for Slack and Discord."""
    message = re.sub(r"\*(.+?)\*", r"\1", message)
    return re.sub(r"_(.+?)_", r"\1", message)


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
