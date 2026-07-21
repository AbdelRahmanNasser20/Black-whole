"""Send Telegram messages from the listing_automation app.

Reuses the same TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars the
auction_extractors scrapers already use — loaded by ``automation.config``.

``send_message(text, topic=...)`` routes to a forum topic ("tab") of the
"BlackWhole Alerts" supergroup (BLACKWHOLE-24). ``topic`` is a logical name —
``"leads"`` / ``"deals"`` / ``"health"`` / ``"poller"`` — resolved to a numeric
``message_thread_id`` from the ``TELEGRAM_TOPIC_*`` env vars. An unknown or
unconfigured topic (or ``topic=None``) falls back to the group's General topic,
so callers keep working before the topic ids are set. Returns (ok, error_str)
and is designed to never raise; alert delivery is best-effort.
"""
from __future__ import annotations

import asyncio

import httpx

from .config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOPIC_LEADS,
    TELEGRAM_TOPIC_DEALS,
    TELEGRAM_TOPIC_HEALTH,
    TELEGRAM_TOPIC_POLLER,
)


_API_TIMEOUT = 15.0

# Logical channel name → configured message_thread_id (raw env value). Read
# once at import, mirroring the token/chat_id above.
_TOPICS = {
    "leads": TELEGRAM_TOPIC_LEADS,
    "deals": TELEGRAM_TOPIC_DEALS,
    "health": TELEGRAM_TOPIC_HEALTH,
    "poller": TELEGRAM_TOPIC_POLLER,
}


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def thread_id_for(topic: str | None) -> int | None:
    """Resolve a logical channel name to its numeric message_thread_id.

    Returns None for an unknown/unconfigured/blank topic — the caller then
    posts to the group's General topic (backward-compatible)."""
    if not topic:
        return None
    raw = _TOPICS.get(topic)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def send_message(text: str, *, topic: str | None = None) -> tuple[bool, str | None]:
    """Send a plain-text message, optionally to a forum topic. Returns (ok, error_str)."""
    if not is_configured():
        return False, "telegram_not_configured"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    thread = thread_id_for(topic)
    if thread is not None:
        payload["message_thread_id"] = thread
    try:
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            r = await client.post(url, json=payload)
        if r.status_code == 200:
            return True, None
        return False, f"http_{r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def send_message_sync(text: str, *, topic: str | None = None) -> tuple[bool, str | None]:
    """Sync convenience wrapper for CLI / non-async call sites."""
    return asyncio.run(send_message(text, topic=topic))
