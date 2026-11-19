"""Backtest of deals/auction_comps.py against real closing prices. Read-only.

Scores each lot at a bid snapshot 6-48 h before its close, with comps closed
before that week only, then compares with the real final price. Run it before
any threshold change:

    .venv/bin/python scripts/flip_backtest.py [NAME=value ...]   # e.g. MIN_SIM=0.5
"""
import datetime as dt
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import automation.config  # noqa: F401  (loads .env)
from automation import db
import deals.auction_comps as ac

overrides = dict(a.split("=") for a in sys.argv[1:])
for k, v in overrides.items():
    setattr(ac, k, type(getattr(ac, k))(v))

with db.connect() as conn:
    conn.execute("SET statement_timeout = '180s'")
    sold = conn.execute(
        "SELECT asset_id, account_id, auction_id, title, canonical_category AS cat, final_bid, closed_at "
        "FROM deal_lots WHERE final_bid > 0 AND outcome IN ('sold', 'low_bid') AND title IS NOT NULL "
        "AND closed_at > now() - interval '300 days'").fetchall()
    snaps = conn.execute(
        "SELECT DISTINCT ON (asset_id, account_id, auction_id) asset_id, account_id, auction_id, "
        "observed_at, bid_count, current_bid, end_utc FROM deal_snapshots "
        "WHERE end_utc - observed_at BETWEEN interval '6 hours' AND interval '48 hours' "
        "AND current_bid > 0 AND bid_count > 0 "
        "ORDER BY asset_id, account_id, auction_id, observed_at DESC").fetchall()
by_key = {(r["asset_id"], r["account_id"], r["auction_id"]): r for r in sold}
snaps = [s for s in snaps if (s["asset_id"], s["account_id"], s["auction_id"]) in by_key]
# test set: snapshots in the last 5 weeks of snapshot data
cut = max(s["observed_at"] for s in snaps)
start = cut - dt.timedelta(days=35)
test = [s for s in snaps if s["observed_at"] >= start]
# one matcher per week bucket so comps are strictly before the snapshot week
buckets = defaultdict(list)
for s in test:
    buckets[(s["observed_at"] - start).days // 7].append(s)
rows = []
t0 = time.time()
for b, items in sorted(buckets.items()):
    asof = start + dt.timedelta(days=7 * b)
    m = ac.Matcher()
    for s in items:
        k = (s["asset_id"], s["account_id"], s["auction_id"])
        r = by_key[k]
        m.add_lot(k, title=r["title"], category=r["cat"], bid=s["current_bid"], premium_pct=0.125)
    for r in sold:
        age = (asof - r["closed_at"]).total_seconds() / 86400
        if age <= 0:
            continue
        m.add_sale(title=r["title"], category=r["cat"], price=r["final_bid"], age_days=age)
    res = m.results()
    for s in items:
        k = (s["asset_id"], s["account_id"], s["auction_id"])
        if k in res:
            r = by_key[k]
            hrs = (s["end_utc"] - s["observed_at"]).total_seconds() / 3600
            rows.append((res[k], s, r, hrs))
print(f"test lots {len(test)}  scored {len(rows)} ({100*len(rows)/len(test):.0f}%)  {time.time()-t0:.0f}s  overrides={overrides}")

def med(x): return statistics.median(x) if x else float('nan')
ratios = [r["final_bid"] / c.comp_value for c, s, r, h in rows]
err = [abs(math.log(x)) for x in ratios]
print(f"comp accuracy: median final/comp = {med(ratios):.2f}; median |log err| = {med(err):.2f} (x{math.exp(med(err)):.2f}); within 2x = {100*sum(e<math.log(2) for e in err)/len(err):.0f}%")
print("band      n   median final/comp   median final cost/comp   share that closed under comp   rise bid->final")
for lo, hi in ((80, 101), (65, 80), (50, 65), (35, 50), (0, 35)):
    g = [(c, s, r) for c, s, r, h in rows if lo <= c.score < hi]
    if not g: continue
    fr = [r["final_bid"] / c.comp_value for c, s, r in g]
    fc = [r["final_bid"] * 1.125 / c.comp_value for c, s, r in g]
    rise = [r["final_bid"] / s["current_bid"] for c, s, r in g]
    print(f"{lo:>3}-{hi-1:<3} {len(g):>6}   {med(fr):>8.2f}   {med(fc):>12.2f}   {100*sum(x<1 for x in fc)/len(fc):>14.0f}%   {med(rise):>10.2f}x")
# top-deal simulation: per day pick top 10 by deal_rank among strong, undamaged, value>=100, >=2 bids
days = defaultdict(list)
for c, s, r, h in rows:
    if c.comp_count >= ac.STRONG_COMPS and not c.damaged and c.comp_value >= 150 and s["bid_count"] >= 2 and c.score >= 65:
        days[s["observed_at"].date()].append((ac.deal_rank(c.cost, c.comp_value, h, s["bid_count"]), c, s, r))
picked = []
for day, items in days.items():
    items.sort(key=lambda x: -x[0])
    picked += items[:10]
if picked:
    fc = [r["final_bid"] * 1.125 / c.comp_value for _, c, s, r in picked]
    print(f"top-10/day sim: {len(picked)} picks over {len(days)} days; median final cost/comp = {med(fc):.2f}; closed under comp = {100*sum(x<1 for x in fc)/len(fc):.0f}%; closed under 70% of comp = {100*sum(x<0.7 for x in fc)/len(fc):.0f}%")
    for _, c, s, r in sorted(picked, key=lambda x: -x[0])[:12]:
        print(f"   score {c.score} comps {c.comp_count} value ${c.comp_value:,.0f} bid ${s['current_bid']:,.0f} -> final ${r['final_bid']:,.0f} | {r['title'][:60]}")
