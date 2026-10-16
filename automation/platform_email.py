"""Alert emails via Resend + signed unsubscribe tokens. Never raises."""
from __future__ import annotations

import html
import logging
import os

import requests
from itsdangerous import BadSignature, URLSafeTimedSerializer

from automation.web import auth

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
UNSUB_SALT = "platform-unsub"
UNSUB_MAX_AGE = 30 * 24 * 3600


def _ser() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(auth._signing_key(), salt=UNSUB_SALT)


def make_unsub_token(user_id: str) -> str:
    return _ser().dumps(str(user_id))


def verify_unsub_token(token: str, max_age: int = UNSUB_MAX_AGE) -> str | None:
    try:
        return _ser().loads(token, max_age=max_age)
    except BadSignature:  # includes SignatureExpired
        return None
    except Exception:
        return None


def _fmt_bid(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _lot_fields(lot: dict) -> dict:
    return {
        "title": str(lot.get("title") or "Untitled lot"),
        "bid": _fmt_bid(lot.get("current_bid")),
        "end": str(lot.get("end_utc") or "n/a"),
        "state": str(lot.get("state") or "n/a"),
        "url": str(lot.get("url") or ""),
    }


def _build(alert: dict, lots: list[dict], unsub_url: str) -> tuple[str, str, str]:
    n = len(lots)
    keyword = alert.get("keyword", "")
    subject = f"{n} new lots match “{keyword}”"
    text = [subject, ""]
    items = []
    for lot in lots:
        f = _lot_fields(lot)
        text.append(f"- {f['title']} | current bid {f['bid']} | closes {f['end']} | {f['state']}")
        if f["url"]:
            text.append(f"  {f['url']}")
        title = html.escape(f["title"])
        if f["url"]:
            title = f'<a href="{html.escape(f["url"], quote=True)}">{title}</a>'
        items.append(f"<li>{title}<br>Current bid {html.escape(f['bid'])} &middot; "
                     f"closes {html.escape(f['end'])} &middot; {html.escape(f['state'])}</li>")
    text += ["", f"Unsubscribe: {unsub_url}"]
    body = (f"<p>{html.escape(subject)}</p><ul>{''.join(items)}</ul>"
            f'<p><a href="{html.escape(unsub_url, quote=True)}">Unsubscribe</a></p>')
    return subject, "\n".join(text), body


def send_alert_email(to: str, alert: dict, lots: list[dict], unsub_url: str) -> bool:
    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key:
        logger.warning("platform-email: degraded (RESEND_API_KEY unset)")
        return False
    try:
        subject, text, body = _build(alert, lots, unsub_url)
        r = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"from": os.getenv("PLATFORM_EMAIL_FROM", "alerts@localhost"),
                  "to": [to], "subject": subject, "text": text, "html": body,
                  "headers": {"List-Unsubscribe": f"<{unsub_url}>"}},
            timeout=10,
        )
        if not 200 <= r.status_code < 300:
            logger.warning("platform-email: degraded (HTTP %s)", r.status_code)
            return False
        return True
    except Exception as e:  # never raise into the alert loop
        logger.warning("platform-email: degraded (%s: %s)", type(e).__name__, e)
        return False
