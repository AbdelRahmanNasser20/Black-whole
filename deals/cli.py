# deals/cli.py
import argparse
from datetime import datetime
from deals import sites
from deals.discover import run_discovery
from deals.watch import poll_once
from deals.digest import send_daily_digest
from deals.fees import fee_model_from_env
from deals.store import init_schema

FURNITURE = ["372","47B","47C","47A","46","47D","28E","266"]
AV_EQUIPMENT = ["22"]              # projectors, screens, sound gear
# resale-vetted verticals (2026-07-03) — see deals/categories.py for the canonical mapping
TOOLS      = ["90","249","375","28I","153","159"]   # tools, power tools, generators, compressors
KITCHEN    = ["287","21","632","631","630","25U"]   # commercial food service + kitchen
COMPUTERS  = ["219","217","218","29","291","220"]   # laptops, desktops, tablets, parts, monitors
RADIOS     = ["28","28S"]                           # two-way radios / comms
LAB        = ["57","57M"]                           # laboratory / test equipment
MEDICAL    = ["67","301"]                           # Class I medical + hospital
FITNESS    = ["147","208"]                          # exercise + fitness/rec
MUSIC      = ["70"]                                 # school-band instruments
LAWN       = ["71","373"]                           # mowing + parks/grounds
DEFAULT_CATEGORIES = (FURNITURE + AV_EQUIPMENT + TOOLS + KITCHEN + COMPUTERS
                      + RADIOS + LAB + MEDICAL + FITNESS + MUSIC + LAWN)

def sweep_categories(arg: str | None, env: dict) -> list[str]:
    """Resolve which categories to sweep. 'all' (arg or env) = whole site,
    which the maestro API expresses as an empty categoryIds string."""
    raw = arg if arg is not None else env.get("DEALS_SWEEP_CATEGORIES", "")
    if raw.strip().lower() == "all":
        return [""]
    if raw.strip():
        return [c.strip() for c in raw.split(",") if c.strip()]
    return DEFAULT_CATEGORIES

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--categories", default=None)
    d.add_argument("--max-pages", type=int, default=60)
    d.add_argument("--site", default="govdeals", choices=list(sites.SITES) + ["all"])
    ar = sub.add_parser("archive-active",
                        help="backfill image archives for active lots before their listings expire")
    ar.add_argument("--limit", type=int, default=100)
    ar.add_argument("--max-mb", type=float, default=200.0)
    ar.add_argument("--zero-bid-only", action="store_true")
    tb = sub.add_parser("track-bidders",
                        help="sample who is leading the lots we care about (GovDeals bidbox)")
    tb.add_argument("--favorites", action="store_true",
                    help="only the lots starred on the Auctions tab")
    tb.add_argument("--category", default="seating_furniture",
                    help="canonical category to sample; 'all' for no filter")
    tb.add_argument("--title-like", default=None, help="extra title filter, e.g. 'chair'")
    tb.add_argument("--ending-within", type=int, default=None,
                    help="only lots closing within N hours (where lead changes are unrecoverable)")
    tb.add_argument("--min-bids", type=int, default=0,
                    help="only lots with at least N bids (an un-bid lot has no bidder to name)")
    tb.add_argument("--limit", type=int, default=100)
    bc = sub.add_parser("backfill-classify",
                        help="fill in lots the str.format bug left unclassified")
    bc.add_argument("--limit", type=int, default=450)
    bc.add_argument("--rpm", type=int, default=18)
    bc.add_argument("--reset-fakes", action="store_true",
                    help="blank the provably-fake other/0.0 rows, then exit")
    rawa = sub.add_parser("archive-raw",
                    help="cold-store closed lots' raw blob to R2, then null it in Postgres")
    # 6000 x ~2.7 KB ~= 16 MB per object: one HTTP PUT, and small enough that a
    # failed run wastes little. Pending is state-defined, so this is cron-safe
    # forever -- a drained backlog prints "nothing pending" and exits 0.
    rawa.add_argument("--limit", type=int, default=6000)
    rawa.add_argument("--lag-hours", type=int, default=48,
                    help="skip lots touched recently; absorbs anti-snipe re-sweeps")
    rawa.add_argument("--no-null", action="store_true",
                    help="export and verify only; leave raw in place")
    sub.add_parser("watch-once")
    sub.add_parser("backfill-outcomes")
    sub.add_parser("analyze")
    sub.add_parser("digest")
    sub.add_parser("rank")
    sub.add_parser("saved-search-alerts",
                   help="run the saved-search alert sweep once (manual test path)")
    sub.add_parser("init-schema")
    a = ap.parse_args()
    # non-discover commands are GovDeals-only for now; discover picks per --site
    adapter = sites.get_adapter(getattr(a, "site", None) or "govdeals") \
        if getattr(a, "site", "govdeals") != "all" else None
    if a.cmd == "init-schema":
        init_schema(); print("schema ready")
    elif a.cmd == "discover":
        import os
        cats = sweep_categories(a.categories, os.environ)
        for key in (sites.enabled_sites() if a.site == "all" else [a.site]):
            rep = run_discovery(sites.get_adapter(key), categories=cats, max_pages=a.max_pages)
            print(f"[{key}] {rep}" if a.site == "all" else rep)
    elif a.cmd == "archive-active":
        from deals.archive import archive_active
        print(archive_active(adapter, limit=a.limit, max_mb=a.max_mb,
                             zero_bid_only=a.zero_bid_only))
    elif a.cmd == "track-bidders":
        from deals import bidders, store
        if a.favorites:
            keys = bidders.favorite_targets(adapter)
        else:
            keys = store.bidder_targets(
                limit=a.limit,
                category=(None if a.category == "all" else a.category),
                title_like=a.title_like, ending_within_hours=a.ending_within,
                min_bids=a.min_bids)
        print(f"sampling {len(keys)} lots")
        print(bidders.track_bidders(adapter, keys))
    elif a.cmd == "backfill-classify":
        from deals.backfill_classify import run as run_backfill_classify
        print(run_backfill_classify(limit=a.limit, rpm=a.rpm, reset=a.reset_fakes))
    elif a.cmd == "archive-raw":
        from deals.raw_archive import run_archive_raw
        print(run_archive_raw(limit=a.limit, lag_hours=a.lag_hours,
                              null_after=not a.no_null))
    elif a.cmd == "watch-once":
        print(poll_once(adapter, datetime.now().astimezone()))
    elif a.cmd == "backfill-outcomes":
        from deals.backfill import run_backfill
        print(f"closed {run_backfill()} lots")
    elif a.cmd == "analyze":
        from deals.analyze import run_analysis
        print(run_analysis())
    elif a.cmd == "digest":
        ok, err = send_daily_digest(fee_model_from_env())
        print("digest sent" if ok else f"digest failed: {err}")
    elif a.cmd == "rank":
        from deals.rank import run_rank
        print(f"ranked {run_rank()} verdicts")
    elif a.cmd == "saved-search-alerts":
        from deals.saved_search_alerts import run_saved_search_alerts
        print(f"sent {run_saved_search_alerts()} alert(s)")

if __name__ == "__main__":
    main()
