# deals/cli.py
import argparse
from datetime import datetime
from deals.adapters.govdeals import GovDealsAdapter
from deals.discover import run_discovery
from deals.watch import poll_once
from deals.digest import send_daily_digest
from deals.fees import fee_model_from_env
from deals.store import init_schema

FURNITURE = ["372","47B","47C","47A","46","47D","28E","266"]
AV_EQUIPMENT = ["22"]              # projectors, screens, sound gear
DEFAULT_CATEGORIES = FURNITURE + AV_EQUIPMENT

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover"); d.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES))
    sub.add_parser("watch-once")
    sub.add_parser("digest")
    sub.add_parser("init-schema")
    a = ap.parse_args()
    adapter = GovDealsAdapter()
    if a.cmd == "init-schema":
        init_schema(); print("schema ready")
    elif a.cmd == "discover":
        rep = run_discovery(adapter, categories=a.categories.split(","))
        print(rep)
    elif a.cmd == "watch-once":
        print(poll_once(adapter, datetime.now().astimezone()))
    elif a.cmd == "digest":
        ok, err = send_daily_digest(fee_model_from_env())
        print("digest sent" if ok else f"digest failed: {err}")

if __name__ == "__main__":
    main()
