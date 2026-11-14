"""Keyword alert matcher: active platform_alerts x new deal_lots -> one email per (user, alert).

Lot identity = deal_lots (site, native_id), the pair the deals code keys on
(deals/models.py full_key, ux_deal_lots_site_native). It is stored in
platform_alert_sends as (source, lot_key).

Send ordering: sends rows are inserted BEFORE the email goes out, so a crash
loses an email rather than double-sending. A failed email keeps its rows.

Daily cap (PLATFORM_ALERTS_DAILY_CAP, default 20 EMAILS per user per UTC day):
every email's rows share one explicit sent_at (the pass time), so
count(DISTINCT (alert_id, sent_at)) over a user's sends today == emails
attempted today. Over-cap lots are not recorded (they retry tomorrow while
still inside the 2-day window) and are counted in skipped_dup.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from automation import db, platform_email

logger = logging.getLogger(__name__)

WINDOW_DAYS = 2
MAX_LOTS_PER_ALERT = 200

ALERTS_SQL = (
    "SELECT a.id, a.user_id, a.keyword, a.state_filter, a.source_filter, a.max_price, "
    "a.created_at, u.email FROM platform_alerts a "
    "JOIN platform_users u ON u.user_id = a.user_id "
    "WHERE a.active AND u.onboarded_at IS NOT NULL AND u.email IS NOT NULL ORDER BY a.id"
)

# first_seen_at window first (indexable / selective), then the cheap filters.
MATCH_SQL = (
    "SELECT d.site AS source, d.native_id AS lot_key, d.title, d.current_bid, d.end_utc, "
    "d.state, d.asset_id, d.account_id FROM deal_lots d "
    "WHERE d.first_seen_at > greatest(%s::timestamptz, %s::timestamptz - interval '2 days') "
    "AND d.end_utc > %s::timestamptz AND d.outcome_complete IS NOT TRUE "
    "AND d.title ILIKE %s ESCAPE '\\' "
    "AND (%s::text IS NULL OR d.state = %s::text) "
    "AND (%s::real IS NULL OR d.current_bid <= %s::real) "
    "AND (%s::text IS NULL OR d.site = %s::text) "
    "AND NOT EXISTS (SELECT 1 FROM platform_alert_sends s WHERE s.alert_id = %s "
    "AND s.source = d.site AND s.lot_key = d.native_id) "
    f"ORDER BY d.first_seen_at DESC LIMIT {MAX_LOTS_PER_ALERT}"
)

SENDS_TODAY_SQL = (
    "SELECT count(*) AS n FROM (SELECT DISTINCT s.alert_id, s.sent_at "
    "FROM platform_alert_sends s JOIN platform_alerts a ON a.id = s.alert_id "
    "WHERE a.user_id = %s AND s.sent_at >= %s::timestamptz) t"
)

INSERT_SEND_SQL = (
    "INSERT INTO platform_alert_sends (alert_id, source, lot_key, email_to, sent_at) "
    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (alert_id, source, lot_key) DO NOTHING"
)

FIRED_SQL = "UPDATE platform_alerts SET last_fired_at = %s WHERE id = %s"


def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _cap() -> int:
    try:
        return int(os.getenv("PLATFORM_ALERTS_DAILY_CAP", "20"))
    except ValueError:
        return 20


_warned_base = False


def _base_url() -> str:
    global _warned_base
    if not os.getenv("PLATFORM_BASE_URL") and not _warned_base:
        _warned_base = True
        logger.warning("platform-alerts: PLATFORM_BASE_URL unset; emails link to localhost")
    return (os.getenv("PLATFORM_BASE_URL") or "http://127.0.0.1:8765").rstrip("/")


def _lot_dict(r: dict) -> dict:
    url = ""
    try:
        from deals import sites
        url = sites.lot_url({"site": r["source"], "asset_id": r.get("asset_id"),
                             "account_id": r.get("account_id"), "native_id": r["lot_key"]})
    except Exception:
        logger.warning("platform-alerts: no url for %s/%s", r.get("source"), r.get("lot_key"))
    return {"title": r.get("title"), "current_bid": r.get("current_bid"),
            "end_utc": r.get("end_utc"), "state": r.get("state"), "url": url}


def _do_alert(a: dict, now: datetime, day_start: datetime, cap: int, rep: dict, users: set) -> None:
    kw = (a["keyword"] or "").strip()
    if not kw:
        return
    st, src, mp = a.get("state_filter"), a.get("source_filter"), a.get("max_price")
    rows = db.fetch_all(MATCH_SQL, (
        a["created_at"], now, now, f"%{escape_like(kw)}%",
        st, st, mp, mp, src, src, a["id"]))
    if not rows:
        return
    rep["matched"] += len(rows)
    sent_today = (db.fetch_one(SENDS_TODAY_SQL, (a["user_id"], day_start)) or {}).get("n", 0)
    if sent_today >= cap:
        rep["skipped_dup"] += len(rows)
        return
    # Built BEFORE any insert: a failure here must not burn the lots.
    unsub = f"{_base_url()}/platform/unsubscribe?token={platform_email.make_unsub_token(str(a['user_id']))}"
    fresh = []
    for r in rows:
        if db.execute(INSERT_SEND_SQL, (a["id"], r["source"], r["lot_key"], a["email"], now)):
            fresh.append(r)
        else:
            rep["skipped_dup"] += 1
    if not fresh:
        return
    db.execute(FIRED_SQL, (now, a["id"]))
    ok = platform_email.send_alert_email(
        a["email"], {"keyword": kw}, [_lot_dict(r) for r in fresh], unsub)
    if ok:
        rep["emailed"] += 1
        users.add(str(a["user_id"]))
    else:
        rep["failed"] += 1


def run_pass(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rep = {"matched": 0, "emailed": 0, "skipped_dup": 0, "users": 0, "failed": 0}
    users: set = set()
    cap = _cap()
    for a in db.fetch_all(ALERTS_SQL):
        try:
            _do_alert(a, now, day_start, cap, rep, users)
        except Exception as e:
            logger.error("platform-alerts: alert %s failed: %r", a.get("id"), e)
    rep["users"] = len(users)
    return rep
