# deals/analyze.py
"""The analyze pass: cheap-close funnel -> identity -> comps -> judge ->
valuation -> verdict row -> alert. Per-lot error isolation throughout."""
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from deals.comps import CompsUnavailable, comps_provider_from_env
from deals.fees import fee_model_from_env
from deals.geo import distance_from_home
from deals.llm_steps import LlmStepError, extract_identity, judge_comps
from deals.mapping import asset_to_lot
from deals.models import Lot
from deals.saved_search_alerts import run_saved_search_alerts
from deals.valuation import value_from_comps, value_from_estimate
from deals.verdict_store import insert_verdict, lots_for_analysis, mark_alerted
from automation.telegram_alerts import send_message_sync

@dataclass
class AnalyzeReport:
    considered: int = 0; analyzed: int = 0; comp_grounded: int = 0
    degraded: int = 0; alerted: int = 0; errors: int = 0

def analyze_lot(lot: Lot, comps_provider, fees, env: dict) -> dict:
    identity = extract_identity(lot)          # raises LlmStepError -> caller counts error
    kept, all_comps, val = [], [], None
    if comps_provider is not None:
        for q in identity.queries:
            try:
                result = comps_provider.fetch(q)
            except CompsUnavailable as e:
                print(f"[analyze] comps unavailable for {q!r}: {e}", file=sys.stderr)
                continue
            all_comps = result.items
            kept = judge_comps(identity, result.items)
            val = value_from_comps(kept, identity.quantity, lot.current_bid, fees, env)
            if val is not None:
                break
    if val is None:
        est_pu = identity.est_resale_per_unit or 0.0
        val = value_from_estimate(est_pu * identity.quantity, identity.quantity,
                                  lot.current_bid, fees)
    return {
        "asset_id": lot.asset_id, "account_id": lot.account_id,
        "auction_id": lot.auction_id, "analyzed_at": datetime.now().astimezone(),
        "identity": {"brand": identity.brand, "model": identity.model,
                     "item_type": identity.item_type, "quantity": identity.quantity,
                     "condition": identity.condition},
        "queries": identity.queries, "method": val.method,
        "comps": [{"listing_id": c.listing_id, "title": c.title, "price": c.price,
                   "url": c.url} for c in kept],
        "comp_count": len(kept), "per_unit": val.per_unit,
        "recovery_tier": val.recovery_tier, "est_resale": val.est_resale,
        "piece_out_ceiling": val.piece_out_ceiling, "landed_cost": val.landed_cost,
        "margin": val.margin, "margin_pct": val.margin_pct,
        "confidence": val.confidence,
        "reasoning": f"{len(kept)}/{len(all_comps)} comps kept for "
                     f"'{identity.queries[0] if identity.queries else ''}'",
        "rank_score": None, "rank_notes": None, "alerted_at": None,
    }

def should_alert(verdict: dict, env: dict) -> bool:
    min_pct = float(env.get("DEALS_ALERT_MIN_MARGIN_PCT", "100"))
    return (verdict["method"] == "comps"
            and verdict["confidence"] in ("medium", "high")
            and verdict["margin_pct"] >= min_pct)

def format_verdict_alert(lot: Lot, v: dict, distance: float | None) -> str:
    url = f"https://www.govdeals.com/en/asset/{lot.asset_id}/{lot.account_id}"
    dist = f" · {distance:.0f} mi away" if distance is not None else ""
    comp_urls = " ".join(c["url"] for c in v.get("comps", [])[:3] if c.get("url"))
    return (f"💰 {lot.title[:70]}\n"
            f"bid ${lot.current_bid:.0f} ({lot.bid_count} bids) → "
            f"est. resale ${v['est_resale']:.0f} "
            f"(margin {v['margin_pct']:.0f}%, {v['confidence']}, "
            f"{v['comp_count']} comps)\n"
            f"landed ~${v['landed_cost']:.0f} · {lot.city}, {lot.state}{dist}\n"
            f"{url}\ncomps: {comp_urls}")

def run_analysis(now: datetime | None = None, env: dict | None = None) -> AnalyzeReport:
    now = now or datetime.now().astimezone()
    env = env if env is not None else dict(os.environ)
    rep = AnalyzeReport()
    fees = fee_model_from_env()
    provider = comps_provider_from_env(env)
    rows = lots_for_analysis(now,
        max_bid=float(env.get("DEALS_ANALYZE_MAX_BID", "25")),
        window_h=int(env.get("DEALS_ANALYZE_WINDOW_H", "24")),
        limit=int(env.get("DEALS_ANALYZE_LIMIT", "50")))
    for row in rows:
        rep.considered += 1
        try:
            lot = asset_to_lot(row["raw"])
            verdict = analyze_lot(lot, provider, fees, env)
            insert_verdict(verdict)
            rep.analyzed += 1
            if verdict["method"] == "comps":
                rep.comp_grounded += 1
            else:
                rep.degraded += 1
            if should_alert(verdict, env):
                dist = distance_from_home(lot.lat, lot.lng, env)
                ok, _ = send_message_sync(format_verdict_alert(lot, verdict, dist))
                if ok:
                    mark_alerted((lot.asset_id, lot.account_id, lot.auction_id),
                                 verdict["analyzed_at"])
                    rep.alerted += 1
        except (LlmStepError, ValueError, KeyError) as e:
            rep.errors += 1
            print(f"[analyze] error on {row.get('asset_id')}: {e}", file=sys.stderr)
    try:
        run_saved_search_alerts(now)
    except Exception as e:
        print(f"[analyze] saved-search pass failed: {e}", file=sys.stderr)
    return rep
