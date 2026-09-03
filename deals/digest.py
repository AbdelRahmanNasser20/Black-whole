from deals import sites
from deals.fees import FeeModel, landed_cost
from automation import db
from automation.telegram_alerts import send_message_sync

VIEW_SQL = """
CREATE OR REPLACE VIEW deal_candidates AS
SELECT asset_id, account_id, auction_id, title, current_bid, bid_count,
       city, state, end_utc, canonical_category
FROM deal_lots
WHERE outcome_complete IS NOT TRUE AND bid_count = 0 AND is_free = false
  AND currency_code = 'USD' AND end_utc <= now() + interval '24 hours'
ORDER BY end_utc ASC;
"""

# Same predicate as the deal_candidates view, minus its ORDER BY, so a
# research-profile fragment can be spliced in (the view has no description
# column to filter on).
_CANDIDATE_SQL = """SELECT asset_id, account_id, auction_id, title, current_bid, bid_count,
       city, state, end_utc, canonical_category
FROM deal_lots
WHERE outcome_complete IS NOT TRUE AND bid_count = 0 AND is_free = false
  AND currency_code = 'USD' AND end_utc <= now() + interval '24 hours'"""

def candidate_rows(profile_where: tuple[str, list] | None = None) -> list[dict]:
    """0-bid lots closing <24h; `profile_where` (deals/profiles.deal_lots_where)
    narrows to one research profile. None = the deal_candidates view as-is."""
    if profile_where is None or profile_where[0] == "TRUE":
        return db.fetch_all("SELECT * FROM deal_candidates")
    return db.fetch_all(f"{_CANDIDATE_SQL} AND ({profile_where[0]}) ORDER BY end_utc ASC",
                        tuple(profile_where[1]))

def format_digest(rows: list[dict], fees: FeeModel, label: str = "") -> str:
    tag = f" [{label}]" if label else ""
    if not rows:
        return f"🪑 No 0-bid lots closing in the next 24h{tag}."
    lines = [f"🪑 {len(rows)} lots closing <24h with 0 bids{tag}:\n"]
    for r in rows[:40]:
        lc = landed_cost(float(r["current_bid"] or 0), qty=1, fees=fees)
        url = sites.lot_url(r)
        lines.append(f"• {r['title'][:50]} — ${r['current_bid']:.0f} ({r['bid_count']} bids), "
                     f"landed ~${lc.total:.0f}, {r['city']}, {r['state']} — {url}")
    return "\n".join(lines)

def send_daily_digest(fees: FeeModel, profile=None) -> tuple[bool, str | None]:
    """`profile` is a deals.profiles.Profile; None = today's all-candidates digest."""
    from deals import profiles
    pw = profiles.deal_lots_where(profile) if profile else None
    rows = candidate_rows(pw)
    return send_message_sync(format_digest(rows, fees, label=profile.name if profile else ""),
                             topic="deals")
