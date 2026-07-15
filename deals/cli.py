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
