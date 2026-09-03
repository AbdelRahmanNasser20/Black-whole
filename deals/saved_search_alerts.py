# deals/saved_search_alerts.py
"""Saved searches with alert=true fire a Telegram message when NEW lots
(first_seen_at after the search's last_run_at) match their stored params."""
import sys
from datetime import datetime
from deals import sites
from automation import db
from automation.telegram_alerts import send_message_sync
from automation.web.deals_query import build_where

def format_search_alert(name: str, rows: list[dict]) -> str:
    lines = [f"🔎 saved search “{name}”: {len(rows)} new match(es)"]
    for r in rows[:10]:
        url = sites.lot_url(r)
        lines.append(f"• {r['title'][:55]} — ${float(r['current_bid'] or 0):.0f} "
                     f"({r['bid_count']} bids), {r['city']}, {r['state']} — {url}")
    if len(rows) > 10:
        lines.append(f"…+{len(rows) - 10} more")
    return "\n".join(lines)

_ALLOWED = {"q", "category", "native", "state", "max_bids", "ending_within",
            "status", "min_margin", "list_id", "tag", "min_price", "max_price",
            "bbox", "profile"}

def _sanitize_params(params: dict | None) -> dict:
    """Whitelist + coerce saved-search params for build_where(). Defensive:
    a malformed value drops its key rather than crash the alert sweep.
    - bbox: "s,w,n,e" string (what the UI saves) or a 4-item JSONB array ->
      4-float tuple (south, west, north, east); anything else is dropped.
    - min_price/max_price: coerced to float, dropped if not numeric."""
    out = {k: v for k, v in (params or {}).items() if k in _ALLOWED}
    if "bbox" in out:
        raw = out.pop("bbox")
        if isinstance(raw, str):
            raw = raw.split(",")
        try:
            if isinstance(raw, (list, tuple)) and len(raw) == 4:
                out["bbox"] = tuple(float(x) for x in raw)
        except (TypeError, ValueError):
            pass
    for key in ("min_price", "max_price"):
        if key in out:
            try:
                out[key] = float(out[key])
            except (TypeError, ValueError):
                out.pop(key)
    return out

def run_saved_search_alerts(now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    sent = 0
    for s in db.fetch_all("SELECT * FROM saved_searches WHERE alert = true"):
        try:
            params = _sanitize_params(s["params"])
            slug = params.pop("profile", None)
            if slug:
                from deals import profiles
                params["profile_where"] = profiles.deal_lots_where(profiles.resolve(slug))
            where, args = build_where(**params)
            if s["last_run_at"]:
                where += " AND deal_lots.first_seen_at > %s"
                args = list(args) + [s["last_run_at"]]
            rows = db.fetch_all(f"""SELECT deal_lots.* FROM deal_lots
                LEFT JOIN LATERAL (SELECT margin_pct FROM deal_verdicts v0
                    WHERE v0.asset_id=deal_lots.asset_id AND v0.account_id=deal_lots.account_id
                      AND v0.auction_id=deal_lots.auction_id
                    ORDER BY v0.analyzed_at DESC LIMIT 1) v ON TRUE
                WHERE {where} ORDER BY end_utc ASC LIMIT 100""", args)
            if rows:
                ok, _ = send_message_sync(format_search_alert(s["name"], rows), topic="deals")
                sent += 1 if ok else 0
            db.execute("UPDATE saved_searches SET last_run_at=%s WHERE id=%s", (now, s["id"]))
        except Exception as e:
            print(f"[saved-search] {s.get('name')}: {e}", file=sys.stderr)
    return sent
