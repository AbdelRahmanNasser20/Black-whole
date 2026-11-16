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
from datetime import datetime, timedelta, timezone

from automation import db, platform_email
from automation.web import public_deals

logger = logging.getLogger(__name__)

WINDOW_DAYS = 2
MAX_LOTS_PER_ALERT = 200

# a.* on purpose: migration 037 adds the saved-search filters (category, min_price,
# max_bids, ending_within, name). Whole rows read the same before and after it.
ALERTS_SQL = (
    "SELECT a.*, u.email FROM platform_alerts a "
    "JOIN platform_users u ON u.user_id = a.user_id "
    "WHERE a.active AND u.onboarded_at IS NOT NULL AND u.email IS NOT NULL ORDER BY a.id"
)

# first_seen_at window first (indexable / selective), then the cheap filters, then the
# public exclusion policy: an alert email is a public surface, so it never carries a
# seating lot or a lot the operator tracks, starred or listed (same rule as /deals).
# The table stays unaliased because exclusion_where() names `deal_lots.` itself.
MATCH_SQL = (
    "SELECT deal_lots.site AS source, deal_lots.native_id AS lot_key, deal_lots.title, "
    "deal_lots.current_bid, deal_lots.end_utc, deal_lots.state, deal_lots.asset_id, "
    "deal_lots.account_id FROM deal_lots "
    "WHERE deal_lots.first_seen_at > greatest(%s::timestamptz, %s::timestamptz - interval '2 days') "
    "AND deal_lots.end_utc > %s::timestamptz AND deal_lots.outcome_complete IS NOT TRUE "
    "AND deal_lots.title ILIKE %s ESCAPE '\\' "
    "AND (%s::text IS NULL OR deal_lots.state = %s::text) "
    "AND (%s::real IS NULL OR deal_lots.current_bid <= %s::real) "
    "AND (%s::text IS NULL OR deal_lots.site = %s::text) "
    "AND NOT EXISTS (SELECT 1 FROM platform_alert_sends s WHERE s.alert_id = %s "
    "AND s.source = deal_lots.site AND s.lot_key = deal_lots.native_id) "
    f"AND {public_deals.exclusion_where()[0]} "
    f"ORDER BY deal_lots.first_seen_at DESC LIMIT {MAX_LOTS_PER_ALERT}"
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

# What an alert already sent. MATCH_SQL does this inside the query for deal_lots, the
# rows read from the snapshot memo or the public feed are checked against this list.
SENT_SQL = "SELECT source, lot_key FROM platform_alert_sends WHERE alert_id = %s"

EXTRA_FILTERS = ("category", "min_price", "max_bids", "ending_within")   # migration 037
FEED_ROWS = 100

# The feed path's deal_lots read: the keyword path's columns (site + native_id = the
# dedupe key, as in MATCH_SQL) plus first_seen_at for the window. The WHERE comes from
# public_deals.build_public_where, so the public exclusion policy is always part of it.
FEED_SELECT = (
    "SELECT deal_lots.site AS source, deal_lots.native_id AS lot_key, deal_lots.title, "
    "deal_lots.current_bid, deal_lots.end_utc, deal_lots.state, deal_lots.asset_id, "
    "deal_lots.account_id, deal_lots.first_seen_at FROM deal_lots WHERE "
)


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
    url = r.get("url") or r.get("govdeals_url") or ""
    if url:   # a feed or snapshot row carries the auction site's own link
        return {"title": r.get("title"), "current_bid": r.get("current_bid"),
                "end_utc": r.get("end_utc"), "state": r.get("state"), "url": url}
    try:
        from deals import sites
        url = sites.lot_url({"site": r["source"], "asset_id": r.get("asset_id"),
                             "account_id": r.get("account_id"), "native_id": r["lot_key"]})
    except Exception:
        logger.warning("platform-alerts: no url for %s/%s", r.get("source"), r.get("lot_key"))
    return {"title": r.get("title"), "current_bid": r.get("current_bid"),
            "end_utc": r.get("end_utc"), "state": r.get("state"), "url": url}


def _aware(dt):
    return dt if (dt is None or dt.tzinfo) else dt.replace(tzinfo=timezone.utc)


def _fresh(rows: list[dict], a: dict, now: datetime) -> list[dict]:
    """Same window as MATCH_SQL: first seen after the alert was made and inside
    WINDOW_DAYS, still open, and not already sent for this alert."""
    floor = max(_aware(a["created_at"]), now - timedelta(days=WINDOW_DAYS))
    live = [r for r in rows if r.get("first_seen_at") and _aware(r["first_seen_at"]) > floor
            and r.get("end_utc") and _aware(r["end_utc"]) > now]
    if not live:
        return []
    sent = {(x["source"], x["lot_key"]) for x in db.fetch_all(SENT_SQL, (a["id"],))}
    return [r for r in live if (r["source"], r["lot_key"]) not in sent]


def _snapshot_matches(a: dict, kw: str, now: datetime) -> list[dict]:
    """The sources that are not in deal_lots (GSA, Municibid, ...), from the public
    snapshot memo. It already carries the exclusion policy. A failed read costs this
    half only, never the deal_lots half."""
    try:
        from automation.web import deals_sources
        items = deals_sources.apply_filters(
            deals_sources.lots("active", now=now), q=kw, state=a.get("state_filter"),
            max_price=a.get("max_price"), site=a.get("source_filter"), now=now)
    except Exception as e:
        logger.warning("platform-alerts: snapshot half skipped for alert %s: %r", a.get("id"), e)
        return []
    return _fresh([{**i, "lot_key": i["source_lot_id"]} for i in items], a, now)


def _feed_matches(a: dict, kw: str, now: datetime) -> list[dict]:
    """A saved search with the 037 filters (or no keyword). Same filters and same public
    policy as the Finds page (`public_deals.build_public_where` + the snapshot memo), but
    its own read: no count(*) (the matcher never uses a total), and the deal_lots rows keep
    their REAL site and native id. `public_deals.fetch_page` rows are GovDeals-shaped
    (source and link overwritten), which would email a TXAuction lot with a GovDeals link
    and store a key the keyword path does not use. A failed snapshot read costs that half only."""
    f = dict(q=kw or None, category=a.get("category"), state=a.get("state_filter"),
             max_bids=a.get("max_bids"), ending_within=a.get("ending_within"),
             min_price=a.get("min_price"), max_price=a.get("max_price"))
    site = a.get("source_filter")
    rows: list[dict] = []
    if public_deals.govdeals_half(site):
        where, args = public_deals.build_public_where(**f, status="active")
        rows = [dict(r) for r in db.fetch_all(
            f"{FEED_SELECT}{where} ORDER BY deal_lots.first_seen_at DESC NULLS LAST LIMIT %s",
            (*args, FEED_ROWS))]
    if site != public_deals.GOVDEALS:
        try:
            from automation.web import deals_sources
            items = deals_sources.apply_filters(
                deals_sources.lots("active", now=now), **f, site=site, now=now)
            rows += [{**i, "lot_key": i["source_lot_id"]} for i in items]
        except Exception as e:
            logger.warning("platform-alerts: snapshot half skipped for alert %s: %r", a.get("id"), e)
    return _fresh(rows, a, now)[:MAX_LOTS_PER_ALERT]


def alert_is_narrow(a: dict, kw: str) -> bool:
    """An alert must name what it watches: a keyword (2+ letters), a category, a state or
    a site. A price or a bid ceiling alone would match almost every new lot. The API
    refuses such an alert (platform_finds.alert_allowed); this guards a row that exists."""
    return len(kw) >= 2 or bool(a.get("category") or a.get("state_filter") or a.get("source_filter"))


def _do_alert(a: dict, now: datetime, day_start: datetime, cap: int, rep: dict, users: set) -> None:
    kw = (a.get("keyword") or "").strip()
    st, src, mp = a.get("state_filter"), a.get("source_filter"), a.get("max_price")
    extra = any(a.get(k) is not None for k in EXTRA_FILTERS)
    if not alert_is_narrow(a, kw):
        return   # no keyword, category, state or site: it would match almost every lot
    if kw and not extra:
        rows = db.fetch_all(MATCH_SQL, (
            a["created_at"], now, now, f"%{escape_like(kw)}%",
            st, st, mp, mp, src, src, a["id"], *public_deals.exclusion_where()[1]))
        rows = (list(rows) + _snapshot_matches(a, kw, now))[:MAX_LOTS_PER_ALERT]
    else:
        rows = _feed_matches(a, kw, now)
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
        a["email"], {"keyword": kw or a.get("name") or "your saved search"},
        [_lot_dict(r) for r in fresh], unsub)
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
    if not os.getenv("RESEND_API_KEY", "").strip():
        # Send rows go in before the email. With no key nothing can go out, so a pass
        # would mark every match as sent and those lots would never be emailed.
        logger.warning("platform-alerts: pass skipped (RESEND_API_KEY unset)")
        return rep
    for a in db.fetch_all(ALERTS_SQL):
        try:
            _do_alert(a, now, day_start, cap, rep, users)
        except Exception as e:
            logger.error("platform-alerts: alert %s failed: %r", a.get("id"), e)
    rep["users"] = len(users)
    return rep
