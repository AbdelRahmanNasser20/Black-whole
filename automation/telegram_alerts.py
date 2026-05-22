"""Send Telegram messages from the listing_automation app.

Reuses the same TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars the
auction_extractors scrapers already use — loaded by ``automation.config``.

Only one capability for now: ``send_message(text)``. Returns (ok, error_str).
Designed to never raise; alert delivery is best-effort and the caller
shouldn't have to wrap it in try/except.
"""
from __future__ import annotations

import asyncio

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


_API_TIMEOUT = 15.0


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


async def send_message(text: str) -> tuple[bool, str | None]:
    """Send a plain-text message. Returns (ok, error_str)."""
    if not is_configured():
        return False, "telegram_not_configured"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    try:
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            r = await client.post(url, json=payload)
        if r.status_code == 200:
            return True, None
        return False, f"http_{r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def send_message_sync(text: str) -> tuple[bool, str | None]:
    """Sync convenience wrapper for CLI / non-async call sites."""
    return asyncio.run(send_message(text))
