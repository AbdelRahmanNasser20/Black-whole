"""Detect no-bid lots reappearing under a new auction (same seller account).
A relist is the second chance to buy at opening price — alert immediately."""
import json
import re
import sys
from datetime import datetime, timedelta
from automation import db
from automation.telegram_alerts import send_message_sync

SIM_THRESHOLD = 0.6

def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 1}

def title_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def find_relist(lot_row: dict, closed_rows: list[dict]) -> dict | None:
    best, best_sim = None, 0.0
    for c in closed_rows:
        if c["account_id"] != lot_row["account_id"]:
            continue
        if c["auction_id"] == lot_row["auction_id"]:
            continue
        sim = title_similarity(lot_row["title"], c["title"])
        if sim >= SIM_THRESHOLD and sim > best_sim:
            best, best_sim = c, sim
    return best

def scan_for_relists(now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    fresh = db.fetch_all("""SELECT asset_id, account_id, auction_id, title, current_bid
        FROM deal_lots WHERE relist_of IS NULL AND outcome IS NULL
          AND first_seen_at > %s""", (now - timedelta(days=2),))
    if not fresh:
        return 0
    accounts = tuple({r["account_id"] for r in fresh})
    closed = db.fetch_all("""SELECT asset_id, account_id, auction_id, title,
               final_bid, closed_at
        FROM deal_lots WHERE outcome = 'no_bid' AND account_id = ANY(%s)""",
        (list(accounts),))
    hits = 0
    for lot in fresh:
        try:
            m = find_relist(lot, closed)
            if not m:
                continue
            db.execute("""UPDATE deal_lots SET relist_of=%s, updated_at=now()
                WHERE asset_id=%s AND account_id=%s AND auction_id=%s""",
                (json.dumps({"asset_id": m["asset_id"], "account_id": m["account_id"],
                             "auction_id": m["auction_id"],
                             "final_bid": m["final_bid"],
                             "closed_at": str(m["closed_at"])}, default=str),
                 lot["asset_id"], lot["account_id"], lot["auction_id"]))
            url = f"https://www.govdeals.com/en/asset/{lot['asset_id']}/{lot['account_id']}"
            send_message_sync(f"♻️ RELIST: {lot['title'][:60]}\n"
                              f"previously closed no-bid — now ${float(lot['current_bid'] or 0):.0f}\n{url}")
            hits += 1
        except Exception as e:
            print(f"[relist] error on {lot['asset_id']}: {e}", file=sys.stderr)
    return hits
