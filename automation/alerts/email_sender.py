"""Provider-agnostic email-sender interface for the alert blast (BLACKWHOLE-10).

Abdel has **not** picked an email provider yet (Resend / SendGrid / SES / …),
so this module deliberately hardcodes none of them and requires none of their
credentials. It defines the *shape* a provider must satisfy and ships exactly
one implementation — `DryRunEmailSender` — which is the default and never sends
a real email.

Wiring a real provider later is three steps, no changes to the matcher or blast
orchestration:

  1. Write a class with a ``send(EmailMessage) -> SendResult`` method (subclass
     `EmailSender` or just match the Protocol).
  2. Register it: ``register_provider("resend", ResendEmailSender)``.
  3. Set env ``ALERTS_EMAIL_PROVIDER=resend`` **and** ``ALERTS_SEND_ENABLED=1``
     plus whatever creds that class reads (e.g. ``RESEND_API_KEY``).

Until *both* env flags are set, `build_email_sender()` returns the dry-run
sender. Send is OFF by default — that is a hard requirement of this ticket.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

log = logging.getLogger("automation.alerts.email")


@dataclass
class EmailMessage:
    """One composed email, provider-independent.

    `headers` carries RFC 8058 one-click unsubscribe (`List-Unsubscribe` +
    `List-Unsubscribe-Post`) so a real provider can pass them straight through
    for Gmail's native unsubscribe. `reply_to` routes replies to the operator's
    inbox (PRD §8: email replies land on operator Gmail).
    """
    to: str
    subject: str
    html: str
    text: str
    from_email: str | None = None
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SendResult:
    """Outcome of one send attempt — maps onto an `alert_sends` row."""
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    suppressed: bool = False  # provider-side suppression (bounce/complaint list)


@runtime_checkable
class EmailSenderProtocol(Protocol):
    """Structural type a provider adapter must satisfy."""
    name: str

    def send(self, message: EmailMessage) -> SendResult: ...


class EmailSender(ABC):
    """Base class for provider adapters (subclassing is optional — the
    Protocol above is the real contract; this just saves boilerplate)."""

    name: str = "abstract"

    @abstractmethod
    def send(self, message: EmailMessage) -> SendResult:  # pragma: no cover
        raise NotImplementedError


class DryRunEmailSender(EmailSender):
    """The default. Logs what *would* be sent and returns success — no network,
    no provider, no real email. `sent` records every message so the blast
    preview and tests can assert on it.
    """

    name = "dry_run"

    def __init__(self, *, log_level: int = logging.INFO) -> None:
        self.sent: list[EmailMessage] = []
        self._log_level = log_level

    def send(self, message: EmailMessage) -> SendResult:
        self.sent.append(message)
        log.log(
            self._log_level,
            "DRY-RUN email suppressed | to=%s subject=%r (SEND DISABLED)",
            message.to,
            message.subject,
        )
        return SendResult(ok=True, provider_message_id=None, error=None)


# ── provider registry ───────────────────────────────────────────────────────
# Empty by design. A real provider is added at runtime via `register_provider`
# so this file never imports or depends on any vendor SDK. `dry_run` is always
# available.
ProviderFactory = Callable[[], EmailSenderProtocol]
_PROVIDERS: dict[str, ProviderFactory] = {
    "dry_run": DryRunEmailSender,
}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register a provider adapter under `name` (e.g. 'resend', 'sendgrid')."""
    _PROVIDERS[name.strip().lower()] = factory


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def build_email_sender(
    *, provider: str | None, send_enabled: bool
) -> EmailSenderProtocol:
    """Resolve the sender to use for a blast.

    HARD RULE: returns `DryRunEmailSender` unless `send_enabled` is true AND a
    real (non-dry-run) provider is both named and registered. A named-but-
    unregistered provider raises, so a misconfiguration fails loudly instead of
    silently doing nothing — but the *default* (no flags) is always dry-run.
    """
    name = (provider or "").strip().lower()
    if not send_enabled or not name or name == "dry_run":
        if send_enabled and name and name not in _PROVIDERS:
            raise RuntimeError(
                f"ALERTS_EMAIL_PROVIDER={name!r} is not registered. "
                f"Known: {available_providers()}. "
                "Register it with alerts.email_sender.register_provider(...)."
            )
        return DryRunEmailSender()
    if name not in _PROVIDERS:
        raise RuntimeError(
            f"ALERTS_EMAIL_PROVIDER={name!r} is not registered. "
            f"Known: {available_providers()}. "
            "Register it with alerts.email_sender.register_provider(...)."
        )
    return _PROVIDERS[name]()
