"""Provider-agnostic email-sender interface for the alert blast (BLACKWHOLE-10).

This module defines the *shape* a provider must satisfy (`EmailSender` /
`EmailSenderProtocol`) and stays vendor-free itself — it imports no SDK and
reads no credentials. Providers register into it at runtime.

Two senders exist:
  - `DryRunEmailSender` (name ``"dry_run"``) — the default. Logs what *would*
    go out and never touches the network. Always available.
  - `ResendEmailSender` (name ``"resend"``, in `resend_sender.py`) — Abdel's
    picked provider (Resend free tier). Self-registers on import but is only
    *instantiated* when send is actually enabled, so importing this package
    still pulls in no credentials.

Send is OFF by default — a hard requirement of this ticket. `build_email_sender`
returns the dry-run sender unless **both** ``ALERTS_SEND_ENABLED=1`` is set AND
a real registered provider is named (``ALERTS_EMAIL_PROVIDER`` defaults to
``resend``). To go live, Abdel sets ``ALERTS_SEND_ENABLED=1`` + ``RESEND_API_KEY``.

Adding another provider later is three steps, no matcher/blast changes:
  1. Write a class with ``send(EmailMessage) -> SendResult`` (subclass
     `EmailSender` or just match the Protocol).
  2. Register it: ``register_provider("name", TheSender)``.
  3. Point ``ALERTS_EMAIL_PROVIDER=name`` and set its creds + ``ALERTS_SEND_ENABLED=1``.
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
