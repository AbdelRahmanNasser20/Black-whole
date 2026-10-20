#!/usr/bin/env python
"""Quote any lane with real carriers — the operator's terminal lane tool.

    .venv/bin/python scripts/lane_quote.py --origin 04270 --dest 28206 --qty 100
    .venv/bin/python scripts/lane_quote.py --origin 04270 --dest 28206 --qty 100 \\
        --lb 11 --per-pallet 40 --height 80
    .venv/bin/python scripts/lane_quote.py --lot bs-10121-53 --dest 28206      # origin + chair data from the lot
    .venv/bin/python scripts/lane_quote.py ... --no-liftgate    # driver does not unload, buyer hand-unloads
    .venv/bin/python scripts/lane_quote.py ... --dock           # forklift/dock: no residential, no liftgate
    .venv/bin/python scripts/lane_quote.py ... --json

Prints every carrier Warp returns for the lane, cheapest first, UNPADDED, with
the in-house estimate beside it and what the site would show a buyer (cheapest
carrier + `site_settings.freight_padding_pct`). Same feed as the lot page and
the admin Sales › Lane quote view (`automation/lane_quote.py`); never
`/ltl/quote`. Calls Warp directly — the hourly budget lives in the web process.
`--lot` reads the lot's ZIP and chair data from Supabase (needs `.env`); without
it nothing touches the database. Exit 0 = carriers listed, 1 = no carrier
answered (the estimate still prints), 2 = bad input.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config  # noqa: E402,F401  (loads .env)
from automation import lane_quote, site_settings, warp_rates  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--origin", help="origin ZIP (5 digits); default = the --lot's ZIP")
    ap.add_argument("--dest", required=True, help="destination ZIP (5 digits)")
    ap.add_argument("--qty", type=int, help="chairs; default = the --lot's quantity remaining")
    ap.add_argument("--lot", metavar="LOT_ID",
                    help="inventory lot: origin ZIP, quantity and chair data come from it (needs .env)")
    ap.add_argument("--lb", type=float, metavar="LBS",
                    help="pounds per chair (default: the lot's, else the standard 13)")
    ap.add_argument("--per-pallet", type=float, metavar="N",
                    help="chairs per LTL pallet (default: the lot's, else 35)")
    ap.add_argument("--height", type=int, metavar="IN",
                    help="pallet height in inches (default: the lot's, else 80; LTL max 85)")
    ap.add_argument("--no-liftgate", action="store_true",
                    help="residential drop without a liftgate — the driver does not unload")
    ap.add_argument("--dock", action="store_true",
                    help="dock/forklift delivery — no residential fee, no liftgate")
    ap.add_argument("--padding", type=float, metavar="PCT",
                    help="padding %% for the 'site would show' line (default: the live setting)")
    ap.add_argument("--timeout", type=float, default=warp_rates.TIMEOUT_SEC,
                    help=f"seconds to wait on Warp (default {warp_rates.TIMEOUT_SEC})")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    return ap


def _lot_row(lot_id: str) -> dict:
    from automation import inventory  # noqa: WPS433 — DB only when asked for
    row = inventory.get(lot_id)
    if not row:
        raise lane_quote.LaneInput(f"lot {lot_id!r} not found")
    return row


def _money(n) -> str:
    return "—" if n is None else f"${float(n):,.2f}"


def render(out: dict) -> str:
    ship = out["shipment"]
    drop = ("dock" if not out["residential"] and not out["liftgate"]
            else "residential + liftgate" if out["residential"] and out["liftgate"]
            else "residential, no liftgate (buyer hand-unloads)" if out["residential"]
            else "liftgate, limited access")
    weight_note = " (standard chair — unmeasured)" if ship["lbs_per_chair_estimated"] else ""
    lines = [
        f"LANE {out['origin_zip']} → {out['dest_zip']} · {out['quantity']:,} chairs · "
        f"{ship['pallets']} pallet{'s' if ship['pallets'] != 1 else ''} × "
        f"{ship['weight_lbs_per_pallet']} lb · {ship['height_in']} in · {drop}",
        f"      {ship['lbs_per_chair']:g} lb/chair{weight_note} · {ship['chairs_per_pallet']:g} per pallet",
    ]
    carriers = out["carriers"]
    if carriers:
        lines.append(f"CARRIERS ({out['carrier_count']}), cheapest first, unpadded:")
        for o in carriers:
            days = f"{o['transit_days']} d" if o.get("transit_days") is not None else ""
            expires = (o.get("expires_at") or "")[:16].replace("T", " ")
            book = "BOOKABLE" if o.get("bookable") else "quote only"
            lines.append(f"  {o['carrier'][:28]:<28} {_money(o['price_usd']):>11}   {days:<5} "
                         f"expires {expires or '—':<16} {book}")
        cheap = out["cheapest"]
        lines.append(
            f"CHEAPEST  {_money(cheap['price_usd'])} {cheap['carrier']} · "
            f"SITE WOULD SHOW ${out['padded_price']:,} (+{out['padding_pct']:g}%)"
        )
    else:
        status = out.get("carrier_status")
        why = out.get("error") or {
            "none": "no carrier answered for this lane",
            "too_big": "too big for LTL",
        }.get(status, status or "not asked")
        lines.append(f"CARRIERS  none — {why}")
    est = out.get("estimator")
    if est:
        mode = est.get("recommended_mode") or est.get("mode") or "ltl"
        low, high = est.get(f"{mode}_low"), est.get(f"{mode}_high")
        rng = f"${low:,.0f}–${high:,.0f} ({mode})" if low is not None else "—"
        lines.append(f"ESTIMATOR {rng} · ~{est.get('miles'):,} mi · ~{est.get('transit_days')} days")
    else:
        lines.append("ESTIMATOR —  (lane refused: offshore or unresolvable)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.dock and args.no_liftgate:
        ap.error("--dock already means no liftgate; pass one of them")
    residential = not args.dock
    liftgate = not (args.dock or args.no_liftgate)

    try:
        lot = _lot_row(args.lot) if args.lot else None
        origin_in = args.origin or (lot or {}).get("zip_code")
        origin = lane_quote.clean_zip(origin_in, "origin")
        dest = lane_quote.clean_zip(args.dest, "dest")
        qty_in = args.qty
        if qty_in is None and lot is not None:
            qty_in = lot.get("quantity_remaining") or lot.get("quantity_original")
        quantity = lane_quote.clean_quantity(qty_in)
        cal = lane_quote.calibration(
            lot, lbs_per_chair=args.lb, chairs_per_pallet=args.per_pallet,
            pallet_height_in=args.height,
        )
    except lane_quote.LaneInput as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    padding = args.padding
    if padding is None:
        padding = float(site_settings.get_all().get(
            "freight_padding_pct", config.FREIGHT_PADDING_PCT_DEFAULT))

    summary, error = None, None
    try:
        options = lane_quote.fetch_options(
            origin, dest, quantity, cal,
            residential=residential, liftgate=liftgate, timeout=args.timeout,
        )
        summary = warp_rates.summarize(options, limit=None)
    except lane_quote.LaneInput as e:          # too big for LTL
        error = str(e)
    except warp_rates.WarpUnavailable as e:
        error = str(e)

    out = lane_quote.result(
        origin, dest, quantity, cal, residential=residential, liftgate=liftgate,
        summary=summary, padding_pct=padding, error=error,
    )
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(render(out))
    return 0 if out["carriers"] else 1


if __name__ == "__main__":
    sys.exit(main())
