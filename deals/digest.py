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

def format_digest(rows: list[dict], fees: FeeModel) -> str:
    if not rows:
        return "🪑 No 0-bid lots closing in the next 24h."
    lines = [f"🪑 {len(rows)} lots closing <24h with 0 bids:\n"]
    for r in rows[:40]:
        lc = landed_cost(float(r["current_bid"] or 0), qty=1, fees=fees)
        url = sites.lot_url(r)
        lines.append(f"• {r['title'][:50]} — ${r['current_bid']:.0f} ({r['bid_count']} bids), "
                     f"landed ~${lc.total:.0f}, {r['city']}, {r['state']} — {url}")
    return "\n".join(lines)

def send_daily_digest(fees: FeeModel) -> tuple[bool, str | None]:
    rows = db.fetch_all("SELECT * FROM deal_candidates")
    return send_message_sync(format_digest(rows, fees), topic="deals")
