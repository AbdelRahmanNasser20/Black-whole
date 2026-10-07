#!/usr/bin/env python3
"""Weekly lead funnel, per channel: visits → lot views → leads → deposits paid.

    .venv/bin/python scripts/lead_funnel_report.py --days 7
    .venv/bin/python scripts/lead_funnel_report.py --days 30 --json

READ-ONLY. Every query is a SELECT over the shared Supabase DB through
`automation.db`. Nothing here writes.

How the numbers are made (plan: docs/analytics_leads_plan.md):
- visits / visitor_days / lot_views: `site_visits` rows (page views the app
  logged; path NOT LIKE '/_event/%'). `visitor_days` counts a (person, day)
  once. These are NOT bot-filtered beyond a user-agent regex — use Cloudflare
  Web Analytics for the headline visit number; use this report for LEADS.
- leads: contact = `inquiries`, subscribe = `subscribers`,
  freight = `freight_quotes` (source='storefront'), clicks = `site_visits`
  rows at '/_event/<kind>' (a tel:/mailto: click beaconed by site.js),
  checkout = `deposits` rows created.
- paid: `deposits` with `paid_at` in the window and status paid/refunded;
  `paid_usd` is the sum of what was actually charged.
- channel: `automation.attribution.channel(source, medium, referrer)` over the
  raw first-touch columns. Before migration 022 the lead tables have no
  attr_* columns, so every lead lands in `unattributed`.
- conversion % = leads / visitor_days (people, not page views).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation import config  # noqa: E402,F401  (loads .env)
from automation import attribution, db  # noqa: E402

LEAD_KINDS = ("contact", "subscribe", "freight", "clicks", "checkout")
COLUMNS = ("visits", "visitor_days", "lot_views", *LEAD_KINDS, "leads", "conv_pct", "paid", "paid_usd")
UNATTRIBUTED = "unattributed"


# ───────────────────────────── pure aggregation ──────────────────────────────

def empty_row() -> dict[str, Any]:
    row: dict[str, Any] = {c: 0 for c in COLUMNS}
    row["paid_usd"] = 0.0
    row["conv_pct"] = 0.0
    return row


def bucketize(rows: Iterable[dict], *, source="source", medium="medium",
              referrer="referrer") -> dict[str, list[dict]]:
    """Group raw (source, medium, referrer, ...) rows under their channel label."""
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[attribution.channel(r.get(source), r.get(medium), r.get(referrer))].append(r)
    return out


def merge(visits: Iterable[dict], leads: dict[str, Iterable[dict]],
          paid: Iterable[dict]) -> dict[str, dict]:
    """Fold grouped SQL rows into one row per channel.

    `visits` rows: {source, medium, referrer, views, visitor_days, lot_views}.
    `leads[kind]` rows: {source, medium, referrer, n} (or {channel: ..., n}
    when the kind is pre-bucketed — e.g. an unattributed table).
    `paid` rows: {source, medium, referrer, n, usd}.
    """
    table: dict[str, dict] = defaultdict(empty_row)
    for ch, group in bucketize(visits).items():
        row = table[ch]
        for r in group:
            row["visits"] += int(r.get("views") or 0)
            row["visitor_days"] += int(r.get("visitor_days") or 0)
            row["lot_views"] += int(r.get("lot_views") or 0)
    for kind, rows in leads.items():
        for r in rows:
            ch = r.get("channel") or attribution.channel(r.get("source"), r.get("medium"), r.get("referrer"))
            table[ch][kind] += int(r.get("n") or 0)
    for r in paid:
        ch = r.get("channel") or attribution.channel(r.get("source"), r.get("medium"), r.get("referrer"))
        table[ch]["paid"] += int(r.get("n") or 0)
        table[ch]["paid_usd"] += float(r.get("usd") or 0)
    for row in table.values():
        row["leads"] = sum(row[k] for k in LEAD_KINDS)
        row["conv_pct"] = round(100.0 * row["leads"] / row["visitor_days"], 2) if row["visitor_days"] else 0.0
    return dict(table)


def totals(table: dict[str, dict]) -> dict:
    tot = empty_row()
    for row in table.values():
        for c in COLUMNS:
            if c != "conv_pct":
                tot[c] += row[c]
    tot["conv_pct"] = round(100.0 * tot["leads"] / tot["visitor_days"], 2) if tot["visitor_days"] else 0.0
    return tot


def ordered(table: dict[str, dict]) -> list[tuple[str, dict]]:
    return sorted(table.items(), key=lambda kv: (-kv[1]["leads"], -kv[1]["visits"], kv[0]))


# ─────────────────────────────────── SQL ─────────────────────────────────────

_WINDOW = "%s >= now() - (%s || ' days')::interval"


def _window(col: str, days: int) -> tuple[str, tuple]:
    return _WINDOW % (col, "%s"), (str(days),)


def q_visits(days: int) -> list[dict]:
    w, p = _window("ts", days)
    return db.fetch_all(
        f"SELECT utm_source AS source, utm_medium AS medium, referer_host AS referrer,"
        f" count(*) AS views, count(DISTINCT visitor) AS visitor_days,"
        f" count(*) FILTER (WHERE lot_id IS NOT NULL) AS lot_views"
        f" FROM site_visits WHERE {w} AND path NOT LIKE '/_event/%%' GROUP BY 1,2,3", p)


def q_clicks(days: int) -> list[dict]:
    w, p = _window("ts", days)
    return db.fetch_all(
        f"SELECT utm_source AS source, utm_medium AS medium, referer_host AS referrer, count(*) AS n"
        f" FROM site_visits WHERE {w} AND path LIKE '/_event/%%' GROUP BY 1,2,3", p)


def q_lead_table(table: str, ts_col: str, days: int, extra_where: str = "") -> list[dict]:
    """Per-channel counts for one lead table, or one `unattributed` row pre-022."""
    w, p = _window(ts_col, days)
    if not attribution.columns_ready(table):
        row = db.fetch_one(f"SELECT count(*) AS n FROM {table} WHERE {w}{extra_where}", p) or {}
        return [{"channel": UNATTRIBUTED, "n": int(row.get("n") or 0)}]
    return db.fetch_all(
        f"SELECT attr_source AS source, attr_medium AS medium, attr_referrer AS referrer, count(*) AS n"
        f" FROM {table} WHERE {w}{extra_where} GROUP BY 1,2,3", p)


def q_paid(days: int) -> list[dict]:
    w, p = _window("paid_at", days)
    where = f"{w} AND status IN ('paid','refunded')"
    if not attribution.columns_ready("deposits"):
        row = db.fetch_one(
            f"SELECT count(*) AS n, coalesce(sum(amount_cents),0)/100.0 AS usd FROM deposits WHERE {where}", p) or {}
        return [{"channel": UNATTRIBUTED, "n": int(row.get("n") or 0), "usd": float(row.get("usd") or 0)}]
    return db.fetch_all(
        f"SELECT attr_source AS source, attr_medium AS medium, attr_referrer AS referrer,"
        f" count(*) AS n, coalesce(sum(amount_cents),0)/100.0 AS usd FROM deposits WHERE {where} GROUP BY 1,2,3", p)


def q_top_lots(days: int, limit: int = 10) -> list[dict]:
    """Lots with the most lead events (inquiries + storefront quotes + deposits + clicks)."""
    p = (str(days),) * 4
    return db.fetch_all(
        "SELECT lot_id, count(*) AS leads FROM ("
        "  SELECT lot_id FROM inquiries WHERE lot_id IS NOT NULL AND created_at >= now() - (%s || ' days')::interval"
        "  UNION ALL SELECT lot_id FROM freight_quotes WHERE source='storefront' AND lot_id IS NOT NULL"
        "    AND quoted_at >= now() - (%s || ' days')::interval"
        "  UNION ALL SELECT lot_id FROM deposits WHERE created_at >= now() - (%s || ' days')::interval"
        "  UNION ALL SELECT lot_id FROM site_visits WHERE path LIKE '/_event/%%' AND lot_id IS NOT NULL"
        "    AND ts >= now() - (%s || ' days')::interval"
        ") x GROUP BY lot_id ORDER BY leads DESC, lot_id LIMIT " + str(int(limit)), p)


def collect(days: int) -> dict:
    leads = {
        "contact": q_lead_table("inquiries", "created_at", days),
        "subscribe": q_lead_table("subscribers", "created_at", days),
        "freight": q_lead_table("freight_quotes", "quoted_at", days, " AND source = 'storefront'"),
        "clicks": q_clicks(days),
        "checkout": q_lead_table("deposits", "created_at", days),
    }
    table = merge(q_visits(days), leads, q_paid(days))
    missing = [t for t in attribution.TABLES if not attribution.columns_ready(t)]
    return {"days": days, "channels": table, "total": totals(table),
            "top_lots": q_top_lots(days), "migration_missing": missing}


# ─────────────────────────────────── print ───────────────────────────────────

def render(data: dict) -> str:
    days = data["days"]
    hdr = ("channel", "visits", "people", "lot_views", "contact", "subscr", "freight",
           "clicks", "checkout", "leads", "conv%", "paid", "paid_usd")
    keys = ("visits", "visitor_days", "lot_views", "contact", "subscribe", "freight",
            "clicks", "checkout", "leads", "conv_pct", "paid", "paid_usd")
    lines = [f"LEAD FUNNEL — last {days} days (per first-touch channel)",
             "people = visitor-days (one person per day, per channel — a person who arrives two ways counts twice);"
             " conv% = leads / people.",
             "site_visits is NOT bot-filtered beyond a UA regex: use Cloudflare Web Analytics"
             " for the headline visit number, this report for leads.", ""]
    rows = ordered(data["channels"]) + [("TOTAL", data["total"])]
    widths = [max(len(hdr[0]), *(len(k) for k, _ in rows))] + [max(len(h), 9) for h in hdr[1:]]
    fmt = "  ".join("{:<%d}" % widths[0] if i == 0 else "{:>%d}" % w for i, w in enumerate(widths))
    lines.append(fmt.format(*hdr))
    lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for name, row in rows:
        vals = [row[k] for k in keys]
        vals[9] = f"{vals[9]:.2f}"
        vals[11] = f"{vals[11]:,.0f}"
        lines.append(fmt.format(name, *vals))
    if data["migration_missing"]:
        lines += ["", f"NOTE: leads are '{UNATTRIBUTED}' for {', '.join(data['migration_missing'])} — "
                  + attribution.MIGRATION_HINT]
    lines += ["", f"LOT PAGES WITH MOST LEAD EVENTS — last {days} days"]
    if data["top_lots"]:
        for r in data["top_lots"]:
            lines.append(f"  {str(r['lot_id']):<14} {int(r['leads']):>5}")
    else:
        lines.append("  (none)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    days = max(1, min(args.days, 365))
    data = collect(days)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
