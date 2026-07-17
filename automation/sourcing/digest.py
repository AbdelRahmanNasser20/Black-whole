"""DMV sourcing-alert digest + dry-run orchestration — BLACKWHOLE-19.

Formats the matched DMV chair lots into one Telegram-style digest (same shape as
``deals.saved_search_alerts.format_search_alert``) and runs the alert job.

Two hard guarantees, mirroring the buyer-alert blast (BLACKWHOLE-10):
  - **Send is OFF by default.** With no config, ``run_dmv_sourcing_alert`` uses a
    dry-run sender: it composes the digest and reports what *would* go out, and
    sends nothing.
  - **Dry-run touches no DB writes.** The optional lot loader only *reads*
    ``deal_lots``; nothing is written. Injecting ``lots=`` bypasses the DB
    entirely (tests run with zero database).

A real Telegram send happens only when ``send_enabled=True`` AND Telegram is
configured (``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import dmv
from .alerts import SourcingMatch, filter_dmv_lots

log = logging.getLogger("automation.sourcing.digest")

# A sender is `(text) -> (ok, error)`, exactly matching send_message_sync.
Sender = Callable[[str], "tuple[bool, str | None]"]


def _govdeals_url(lot: dict) -> str:
    asset, account = lot.get("asset_id"), lot.get("account_id")
    if asset and account:
        return f"https://www.govdeals.com/en/asset/{asset}/{account}"
    return lot.get("url") or lot.get("hero_image_url") or ""


def format_sourcing_digest(matches: list[SourcingMatch]) -> str:
    """Telegram-ready text for a batch of DMV chair-lot matches."""
    if not matches:
        return "🏛️ DMV sourcing: no new chair lots within 100 mi of DC."
    lines = [f"🏛️ DMV sourcing: {len(matches)} new chair lot(s) "
             f"within {dmv.RADIUS_MILES} mi of DC:"]
    for m in matches[:20]:
        lot = m.lot
        title = (lot.get("title") or "untitled")[:55]
        bid = float(lot.get("current_bid") or 0)
        nbids = lot.get("bid_count") or 0
        city = lot.get("city") or "?"
        state = lot.get("state") or "?"
        dist = m.reason.get("distance_miles")
        dist_str = f"{dist:.0f} mi" if isinstance(dist, (int, float)) else "in-state"
        url = _govdeals_url(lot)
        lines.append(f"• {title} — ${bid:.0f} ({nbids} bids), "
                     f"{city}, {state} [{dist_str}] — {url}")
    if len(matches) > 20:
        lines.append(f"…+{len(matches) - 20} more")
    return "\n".join(lines)


@dataclass
class SourcingReport:
    dry_run: bool
    total_lots: int = 0
    matched: int = 0
    sent: bool = False
    error: str | None = None
    digest: str = ""
    matches: list[dict] = field(default_factory=list)
    skips: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ── DB loader (bypassed when `lots=` is injected) ────────────────────────────

def load_dmv_lots(states: frozenset[str] = dmv.DMV_STATES,
                  limit: int = 500) -> list[dict]:
    """Read candidate DMV lots from ``deal_lots`` (READ-ONLY; never writes).

    Coarse SQL pre-filter by state; the precise 100-mi radius + chair-type gate
    is applied in Python by ``filter_dmv_lots`` (deal_lots has real lat/lng but
    no chair_type/source column, so the fine filtering can't be pushed to SQL).
    """
    from automation import db  # lazy: importing must not require a DB

    placeholders = ", ".join(["%s"] * len(states))
    sql = (f"SELECT * FROM deal_lots WHERE upper(btrim(state)) IN ({placeholders}) "
           f"AND outcome_complete IS NOT TRUE ORDER BY first_seen_at DESC LIMIT %s")
    args = [s.upper() for s in states] + [limit]
    return db.fetch_all(sql, args)


def run_dmv_sourcing_alert(
    *,
    lots: list[dict] | None = None,
    since: Any = None,
    send_enabled: bool = False,
    sender: Sender | None = None,
    anchor: tuple[float, float] = dmv.DC_ANCHOR,
    radius_miles: int = dmv.RADIUS_MILES,
    states: frozenset[str] = dmv.DMV_STATES,
) -> SourcingReport:
    """Filter DMV chair lots within radius and (optionally) alert.

    Dry-run by default: composes the digest, sends nothing, writes nothing. A
    live Telegram send happens only when ``send_enabled=True`` and a real
    ``sender`` resolves (Telegram configured).
    """
    if lots is None:
        lots = load_dmv_lots(states=states)

    matches, skips = filter_dmv_lots(
        lots, since=since, anchor=anchor, radius_miles=radius_miles, states=states
    )
    digest = format_sourcing_digest(matches)

    # Resolve the sender. Off by default; even when enabled, an unconfigured
    # Telegram degrades to dry-run rather than raising.
    resolved: Sender | None = sender
    if send_enabled and resolved is None:
        from automation import telegram_alerts as tg
        if tg.is_configured():
            resolved = tg.send_message_sync
    dry_run = not (send_enabled and resolved is not None)

    report = SourcingReport(
        dry_run=dry_run,
        total_lots=len(lots),
        matched=len(matches),
        digest=digest,
        matches=[{"lot_key": m.lot.get("asset_id") or m.lot.get("title"),
                  "title": m.lot.get("title"), "state": m.lot.get("state"),
                  "reason": m.reason} for m in matches],
        skips=[{"lot_key": s.lot_key, "reason": s.reason} for s in skips],
    )

    if dry_run:
        report.notes.append(
            "DRY-RUN: send disabled — digest composed, nothing sent to Telegram."
        )
        log.info("dmv sourcing preview: lots=%d matched=%d", len(lots), len(matches))
        return report

    if matches:
        ok, err = resolved(digest)  # type: ignore[misc]
        report.sent = ok
        report.error = err
        if not ok:
            report.notes.append(f"send failed: {err}")
    else:
        report.notes.append("no matches — skipped send")
    return report
