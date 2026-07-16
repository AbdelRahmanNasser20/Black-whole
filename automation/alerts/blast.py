"""New-inventory alert blast job (BLACKWHOLE-10, PRD §5).

Given a sellable inventory lot, find the subscribers it actually matches
(`matcher.match_lot`), compose one email per matched subscriber, and send it
through the provider-agnostic sender (`email_sender`). Two hard guarantees:

  - **Send is OFF by default.** With no config, `run_blast` uses the dry-run
    sender: it logs/collects what *would* go out and emails nothing.
  - **Dry-run touches no DB rows.** Reads (lot + subscribers + prior sends) are
    needed to compute matches, but no `alert_sends` write happens unless send is
    explicitly enabled — so running the job against the live DB in its default
    mode is inert.

The dedupe invariant (`alert_sends UNIQUE(subscriber_id, lot_id, channel)`,
migration 002) means a subscriber is never alerted twice about the same lot.
In dry-run we read the existing pairs and report which recipients *would* be
suppressed; only a real send writes the rows.

DB access is injected (`lot=`, `subscribers=`, `already_sent=`) so tests run
with no database. In normal use those default to loaders over `automation.db`.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from .. import config, db
from . import email_sender as es
from .matcher import CHANNEL_EMAIL, Match, Skip, match_lot

log = logging.getLogger("automation.alerts.blast")


@dataclass
class Recipient:
    subscriber_id: Any
    email: str
    subject: str
    match_reason: dict
    would_suppress_dedupe: bool = False
    sent: bool = False
    provider_message_id: str | None = None
    error: str | None = None


@dataclass
class BlastReport:
    lot_id: str
    blast_id: str
    dry_run: bool
    provider: str
    total_subscribers: int = 0
    matched: int = 0
    suppressed_dedupe: int = 0
    sent: int = 0
    failed: int = 0
    capped: int = 0
    recipients: list[Recipient] = field(default_factory=list)
    skips: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


# ── DB loaders (bypassed in tests via injection) ────────────────────────────

def load_lot(lot_id: str) -> dict | None:
    return db.fetch_one("SELECT * FROM inventory WHERE lot_id = %s", (str(lot_id),))


def load_eligible_subscribers() -> list[dict]:
    """Active subscribers with an email — the email-blast candidate set.

    Uses `status <> 'unsubscribed'` so it keeps working on the shipped schema
    (whose CHECK has no 'bounced'/'invalid'); migration 002 widens the status
    set and this predicate still excludes those once present.
    """
    return db.fetch_all(
        """
        SELECT * FROM subscribers
        WHERE status <> 'unsubscribed'
          AND email IS NOT NULL AND btrim(email) <> ''
        ORDER BY created_at ASC
        """
    )


def load_already_sent_pairs(lot_id: str) -> set:
    """(subscriber_id, channel) already recorded in alert_sends for this lot.

    Returns an empty set if the alert_sends table doesn't exist yet (pre
    migration 002) — dry-run must never fail just because the table is absent.
    """
    try:
        rows = db.fetch_all(
            "SELECT subscriber_id, channel FROM alert_sends WHERE lot_id = %s",
            (str(lot_id),),
        )
    except Exception as exc:  # table missing / not migrated
        log.warning("alert_sends unavailable (pre-migration?): %s", exc)
        return set()
    return {(r["subscriber_id"], r["channel"]) for r in rows}


# ── composition ─────────────────────────────────────────────────────────────

def unsubscribe_url(sub: dict) -> str | None:
    token = (sub.get("unsubscribe_token") or "").strip()
    if not token:
        return None
    base = config.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/alerts/unsubscribe?token={token}"


def _lot_location(lot: dict) -> str:
    return " ".join(
        x for x in (lot.get("city"), lot.get("state"), lot.get("zip_code")) if x
    ) or "our warehouse"


def compose_email(sub: dict, lot: dict) -> es.EmailMessage:
    """Build a per-subscriber `EmailMessage` from a lot.

    Includes CAN-SPAM footer (postal address + unsubscribe) and RFC 8058
    `List-Unsubscribe` headers so a real provider gets Gmail one-tap for free.
    Unsubscribe URL is None when the row has no token yet (pre-migration) — the
    dry-run notes it; a real send would require the migration + backfill first.
    """
    name = (sub.get("name") or "there").strip() or "there"
    title = lot.get("title") or f"Lot {lot.get('lot_id')}"
    qty = lot.get("quantity_remaining")
    price = lot.get("price_per_chair")
    loc = _lot_location(lot)
    listing_url = f"{config.PUBLIC_BASE_URL.rstrip('/')}/listings/{lot.get('lot_id')}"
    unsub = unsubscribe_url(sub)

    subject = f"New chairs near you: {title}"
    lines = [
        f"Hi {name},",
        "",
        f"A new lot just landed — {title}.",
        f"  Location: {loc}",
    ]
    if qty:
        lines.append(f"  Quantity available: {qty}")
    if price:
        lines.append(f"  Price: ${price}/chair")
    lines += [
        "",
        f"See it here: {listing_url}",
        "",
        "— BLACKWHOLE",
    ]
    footer = []
    if config.ALERTS_POSTAL_ADDRESS:
        footer.append(config.ALERTS_POSTAL_ADDRESS)
    if unsub:
        footer.append(f"Unsubscribe: {unsub}")
    else:
        footer.append("Unsubscribe: (token pending — apply migration 002)")
    text = "\n".join(lines + ["", "-" * 20] + footer)

    esc = _html_escape
    html_body = [
        f"<p>Hi {esc(name)},</p>",
        f"<p>A new lot just landed — <strong>{esc(title)}</strong>.</p>",
        f"<p>Location: {esc(loc)}"
        + (f"<br>Quantity available: {esc(str(qty))}" if qty else "")
        + (f"<br>Price: ${esc(str(price))}/chair" if price else "")
        + "</p>",
        f'<p><a href="{esc(listing_url)}">See the listing</a></p>',
        "<p>— BLACKWHOLE</p>",
        "<hr>",
        "<p style='font-size:12px;color:#888'>"
        + (esc(config.ALERTS_POSTAL_ADDRESS) + "<br>" if config.ALERTS_POSTAL_ADDRESS else "")
        + (f'<a href="{esc(unsub)}">Unsubscribe</a>' if unsub else "Unsubscribe: token pending")
        + "</p>",
    ]

    headers: dict[str, str] = {}
    if unsub:
        headers["List-Unsubscribe"] = f"<{unsub}>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    return es.EmailMessage(
        to=sub["email"],
        subject=subject,
        html="".join(html_body),
        text=text,
        from_email=config.ALERTS_FROM_EMAIL,
        reply_to=config.ALERTS_REPLY_TO or None,
        headers=headers,
    )


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── orchestration ───────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def preview_blast(
    lot_id: str,
    *,
    lot: dict | None = None,
    subscribers: list[dict] | None = None,
    already_sent: set | None = None,
    default_radius_miles: int | None = None,
) -> BlastReport:
    """Run the matcher and report recipients + reasons. Sends nothing, writes
    nothing — this backs `POST /api/alerts/blast/{lot_id}/preview`."""
    return run_blast(
        lot_id,
        send_enabled=False,
        lot=lot,
        subscribers=subscribers,
        already_sent=already_sent,
        default_radius_miles=default_radius_miles,
        _preview_only=True,
    )


def run_blast(
    lot_id: str,
    *,
    send_enabled: bool | None = None,
    provider: str | None = None,
    sender: es.EmailSenderProtocol | None = None,
    max_per_day: int | None = None,
    lot: dict | None = None,
    subscribers: list[dict] | None = None,
    already_sent: set | None = None,
    default_radius_miles: int | None = None,
    _preview_only: bool = False,
) -> BlastReport:
    """Execute (or dry-run) a blast for one lot.

    `send_enabled` defaults to `config.ALERTS_SEND_ENABLED` (False in prod).
    When False — the default — nothing is emailed and no `alert_sends` row is
    written; the returned `BlastReport` describes exactly what *would* happen.
    Set `send_enabled=True` AND configure a provider to actually send.
    """
    if send_enabled is None:
        send_enabled = config.ALERTS_SEND_ENABLED
    if default_radius_miles is None:
        default_radius_miles = config.ALERTS_DEFAULT_RADIUS_MILES
    if max_per_day is None:
        max_per_day = config.MAX_ALERTS_PER_DAY
    provider = provider if provider is not None else config.ALERTS_EMAIL_PROVIDER

    if sender is None:
        sender = es.build_email_sender(provider=provider, send_enabled=send_enabled)
    dry_run = (not send_enabled) or getattr(sender, "name", "") == "dry_run" or _preview_only

    blast_id = str(uuid.uuid4())
    report = BlastReport(
        lot_id=str(lot_id), blast_id=blast_id, dry_run=dry_run,
        provider=getattr(sender, "name", "dry_run"),
    )

    if lot is None:
        lot = load_lot(lot_id)
    if lot is None:
        report.notes.append(f"lot {lot_id} not found")
        return report

    if subscribers is None:
        subscribers = load_eligible_subscribers()
    if already_sent is None:
        already_sent = load_already_sent_pairs(lot_id)

    report.total_subscribers = len(subscribers)
    matches, skips = match_lot(
        lot, subscribers, default_radius_miles=default_radius_miles
    )
    report.matched = len(matches)
    report.skips = [{"subscriber_id": s.subscriber_id, "reason": s.reason} for s in skips]

    sub_by_id = {s.get("id"): s for s in subscribers}

    for m in matches:
        sub = sub_by_id.get(m.subscriber_id, {})
        msg = compose_email(sub, lot)
        rec = Recipient(
            subscriber_id=m.subscriber_id, email=m.email,
            subject=msg.subject, match_reason=m.match_reason,
        )
        if (m.subscriber_id, CHANNEL_EMAIL) in already_sent:
            rec.would_suppress_dedupe = True
            report.suppressed_dedupe += 1
            report.recipients.append(rec)
            continue

        if report.sent >= max_per_day:
            report.capped += 1
            report.recipients.append(rec)
            continue

        if dry_run:
            # Preview only. Exercise the dry-run sender (logs, records nothing)
            # but never call a *real* sender that was injected alongside a
            # disabled send — and never write an alert_sends row.
            if getattr(sender, "name", "") == "dry_run":
                sender.send(msg)
            report.recipients.append(rec)
            continue

        # Live path: send, then record the alert_sends row (dedupe on conflict).
        result = sender.send(msg)
        rec.sent = result.ok
        rec.provider_message_id = result.provider_message_id
        rec.error = result.error
        if result.ok:
            report.sent += 1
        else:
            report.failed += 1
        _record_send(blast_id, lot_id, m, result)
        report.recipients.append(rec)

    if dry_run:
        report.notes.append(
            "DRY-RUN: send disabled — no emails sent, no alert_sends rows written."
        )
        log.info(
            "blast preview lot=%s matched=%d suppressed=%d capped=%d (provider=%s)",
            lot_id, report.matched, report.suppressed_dedupe, report.capped,
            report.provider,
        )
    return report


def _record_send(blast_id: str, lot_id: str, match: Match, result: es.SendResult) -> None:
    """Insert (or dedupe) an alert_sends row for a real send. Never called in
    dry-run. ON CONFLICT DO NOTHING enforces the never-twice invariant."""
    status = "sent" if result.ok else "failed"
    if result.suppressed:
        status = "suppressed"
    try:
        db.execute(
            """
            INSERT INTO alert_sends (
                blast_id, subscriber_id, lot_id, channel, status,
                provider_message_id, match_reason, error, sent_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (subscriber_id, lot_id, channel) DO NOTHING
            """,
            (
                blast_id, match.subscriber_id, str(lot_id), CHANNEL_EMAIL, status,
                result.provider_message_id, Jsonb(match.match_reason),
                result.error, _now_iso(),
            ),
        )
    except Exception as exc:  # pragma: no cover - live-only path
        log.error("failed to record alert_sends row: %s", exc)
