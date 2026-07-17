"""Resend (resend.com) email provider adapter for the alert blast (BLACKWHOLE-10).

Abdel picked **Resend free tier** as the alerts email provider (3,000/mo, 100/day
— covers tens of sends per blast, a few blasts a month, forever; smallest REST
API of the candidates; first-class `List-Unsubscribe` passthrough). This module
implements the one method the blast pipeline needs — `send(EmailMessage) ->
SendResult` — against Resend's `POST /emails` endpoint, and self-registers under
the name ``"resend"`` at import time.

SEND STAYS OFF BY DEFAULT. Registering the *factory* here imports no network and
reads no credentials. `ResendEmailSender` is only *instantiated* when
`build_email_sender(provider="resend", send_enabled=True)` is called — i.e. only
once Abdel sets both ``ALERTS_SEND_ENABLED=1`` and ``RESEND_API_KEY``. Until
then the default dry-run sender is used and this class never touches the wire.
"""
from __future__ import annotations

import logging

import httpx

from .. import config
from . import email_sender as es

log = logging.getLogger("automation.alerts.email.resend")

RESEND_API_URL = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 15.0


class ResendEmailSender(es.EmailSender):
    """Sends one email per call through Resend's REST API.

    Reads `RESEND_API_KEY` from config at construction and fails loudly if it's
    missing — a misconfigured live send should error, never silently no-op.
    """

    name = "resend"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_url: str = RESEND_API_URL,
        timeout: float = _TIMEOUT_SECONDS,
    ) -> None:
        key = (api_key if api_key is not None else config.RESEND_API_KEY) or ""
        key = key.strip()
        if not key:
            raise RuntimeError(
                "RESEND_API_KEY is not set — required to send via Resend. "
                "Set it in the environment (Render) or leave ALERTS_SEND_ENABLED "
                "unset to stay in dry-run."
            )
        self._api_key = key
        self._api_url = api_url
        self._timeout = timeout

    def _payload(self, message: es.EmailMessage) -> dict:
        from_email = (message.from_email or config.ALERTS_FROM_EMAIL or "").strip()
        body: dict = {
            "from": from_email,
            "to": [message.to],
            "subject": message.subject,
            "html": message.html,
            "text": message.text,
        }
        if message.reply_to:
            body["reply_to"] = message.reply_to
        if message.headers:
            # Resend passes custom headers straight through — this is how the
            # RFC 8058 List-Unsubscribe one-click headers reach Gmail.
            body["headers"] = dict(message.headers)
        return body

    def send(self, message: es.EmailMessage) -> es.SendResult:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = httpx.post(
                self._api_url,
                json=self._payload(message),
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:  # network/timeout — retryable, report failure
            log.warning("Resend send failed (transport) to=%s: %s", message.to, exc)
            return es.SendResult(ok=False, error=f"transport error: {exc}")

        if 200 <= resp.status_code < 300:
            message_id = None
            try:
                message_id = (resp.json() or {}).get("id")
            except ValueError:
                pass
            return es.SendResult(ok=True, provider_message_id=message_id)

        # Non-2xx: surface Resend's error message; flag known suppression codes.
        detail = _error_detail(resp)
        suppressed = resp.status_code in (403, 422) and _looks_suppressed(detail)
        log.warning(
            "Resend send rejected to=%s status=%s: %s",
            message.to, resp.status_code, detail,
        )
        return es.SendResult(
            ok=False,
            error=f"resend {resp.status_code}: {detail}",
            suppressed=suppressed,
        )


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:300] or "unknown error"
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data)[:300]
    return str(data)[:300]


def _looks_suppressed(detail: str) -> bool:
    d = detail.lower()
    return any(k in d for k in ("suppress", "bounce", "complaint", "blocked"))


# Self-register so `build_email_sender(provider="resend", ...)` resolves. The
# factory is the class itself — instantiation (and the credential read) is
# deferred until an actual live send is requested.
es.register_provider("resend", ResendEmailSender)
