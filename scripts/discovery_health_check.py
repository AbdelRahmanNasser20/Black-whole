"""Daily watchdog for the discovery pipeline — alerts when auction data is
stale or degraded, INDEPENDENTLY of the scrape.

Why it exists: the only pre-existing degradation alert lived inside the Public
Surplus scraper, needed Telegram env the discovery cron didn't have, and only
fired when a PS run completed — so a 2-day 100%-null outage (2026-07-20/21)
went unnoticed. This runs as its own cron, reads Supabase directly, and pings
Telegram if ANY source is stale or degraded. A total scrape crash still trips
it, because it doesn't depend on the scrape running at all.

    .venv/bin/python scripts/discovery_health_check.py            # check + alert
    .venv/bin/python scripts/discovery_health_check.py --dry-run  # print, never send

Tuning (env):
  DISCOVERY_STALE_HOURS      newest row older than this -> STALE   (default 30)
  DISCOVERY_DEGRADED_FRAC    null-qty fraction >= this -> DEGRADED (default 0.5)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from automation import db, telegram_alerts          # noqa: E402

# link-fragment per source, matching automation/auctions_supabase.py
SOURCES = {"GovDeals": "govdeals.com", "Public Surplus": "publicsurplus.com"}


def assess_health(
    source_label: str,
    newest,
    recent_total: int,
    recent_null: int,
    now: datetime,
    *,
    stale_hours: float,
    degraded_frac: float,
) -> dict:
    """Classify one source as OK / STALE / DEGRADED. Pure — no DB, no clock.

    STALE  : no rows, or newest row older than stale_hours (scrape stopped
             producing / crashing).
    DEGRADED: fresh rows exist but >= degraded_frac of the recent ones have no
             quantity (the LLM is failing; Auctions tab hides them).
    """
    if newest is None:
        return {"source": source_label, "status": "STALE",
                "detail": "no rows in auction_listings at all"}
    age_h = (now - newest).total_seconds() / 3600.0
    if age_h > stale_hours:
        days = age_h / 24.0
        return {"source": source_label, "status": "STALE",
                "detail": f"newest row {days:.1f} days old "
                          f"(last seen {newest:%Y-%m-%d %H:%M UTC})"}
    if recent_total > 0 and (recent_null / recent_total) >= degraded_frac:
        pct = round(100 * recent_null / recent_total)
        return {"source": source_label, "status": "DEGRADED",
                "detail": f"{recent_null}/{recent_total} recent rows have no "
                          f"quantity ({pct}%) — quantity LLM is failing"}
    return {"source": source_label, "status": "OK",
            "detail": f"fresh; {recent_total} recent rows, {recent_null} without qty"}


def _summarize(frag: str, window_hours: float) -> dict:
    """Per-source counts from Supabase: newest last_seen_at + recent null-rate."""
    row = db.fetch_one(
        """
        SELECT
          max(last_seen_at) AS newest,
          count(*) FILTER (WHERE last_seen_at >= now() - (%s * interval '1 hour')) AS recent_total,
          count(*) FILTER (WHERE last_seen_at >= now() - (%s * interval '1 hour')
                             AND quantity IS NULL) AS recent_null
        FROM auction_listings
        WHERE link ILIKE %s
        """,
        (window_hours, window_hours, f"%{frag}%"),
    )
    return row or {"newest": None, "recent_total": 0, "recent_null": 0}


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dry_run = "--dry-run" in argv
    stale_hours = float(os.getenv("DISCOVERY_STALE_HOURS", "30"))
    degraded_frac = float(os.getenv("DISCOVERY_DEGRADED_FRAC", "0.5"))
    now = datetime.now(timezone.utc)

    results = []
    for label, frag in SOURCES.items():
        s = _summarize(frag, stale_hours)
        results.append(assess_health(
            label, s["newest"], int(s["recent_total"] or 0), int(s["recent_null"] or 0),
            now, stale_hours=stale_hours, degraded_frac=degraded_frac,
        ))

    for r in results:
        print(f"  [{r['status']:8s}] {r['source']}: {r['detail']}")

    unhealthy = [r for r in results if r["status"] != "OK"]
    if not unhealthy:
        print("all sources healthy — no alert sent.")
        return 0

    lines = [f"⚠️ DISCOVERY HEALTH — {len(unhealthy)} issue(s)"]
    for r in unhealthy:
        lines.append(f"• {r['source']}: {r['status']} — {r['detail']}")
    lines.append("Auctions tab may be showing few/zero lots. "
                 "Check the black-whole-discovery cron logs.")
    msg = "\n".join(lines)

    if dry_run:
        print("\n--dry-run — would send:\n" + msg)
        return 0
    if not telegram_alerts.is_configured():
        print("\nTELEGRAM NOT CONFIGURED — issues found but no alert could be "
              "sent. Set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.", file=sys.stderr)
        return 0
    ok, err = telegram_alerts.send_message_sync(msg)
    print(f"\nalert sent: {ok}" + (f" (error: {err})" if err else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
