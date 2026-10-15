"""eBay API check + Promoted Listings ad-rate sync (CLI over automation/ebay_ads.py).

    .venv/bin/python scripts/ebay_ads.py check            # read-only: keys, token, campaigns, listings, eligibility
    .venv/bin/python scripts/ebay_ads.py plan             # read-only: suggested rate per listing, capped
    .venv/bin/python scripts/ebay_ads.py apply --yes      # writes the planned rates (costs money on sale)
    add --sandbox to any command to hit the sandbox keyset instead.

Env (.env, gitignored): EBAY_APP_ID, EBAY_CERT_ID, EBAY_REFRESH_TOKEN (seller user token, OAuth),
EBAY_AD_RATE_CAP (default 15). Sandbox: EBAY_SANDBOX_APP_ID / _CERT_ID / _REFRESH_TOKEN.
Rate rule: eBay's suggested rate for the listing, never above the cap. Default is dry — nothing
is written without `apply --yes`, and never while eBay says the account is not eligible (409).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from automation import config  # noqa: E402,F401 — loads .env
from automation import ebay_ads  # noqa: E402


def _print_plan(p: dict) -> None:
    c = p["campaign"]
    print(f"campaign: {c and c['id']} ({c and c['status']}) cap={p['cap']}%")
    print(f"state: {p['state']}" + (f" — {p['reason']}" if p.get("reason") else ""))
    for r in p["rows"]:
        print(f"  {r['id']}  now={r['now']}  suggested={r['suggested']}  → {r['want']}%  "
              f"{(r['title'] or '')[:55]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "plan", "apply"])
    ap.add_argument("--sandbox", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    try:
        eb = ebay_ads.EbayClient(sandbox=a.sandbox)
        if a.cmd == "check":
            eb.app_token()
            print(f"✓ app keys OK ({eb.host})")
            eb.token()
            print("✓ seller user token OK")
            ls = eb.active_listings()
            print(f"✓ {len(ls)} active listings")
            for c in eb.campaigns():
                s = ebay_ads._campaign_summary(c)
                print(f"  campaign {s['id']} {s['name']!r} status={s['status']} model={s['model']} rate={s['rate']}")
            p = ebay_ads.plan(eb, ebay_ads.default_cap())
            mark = "✓" if p["state"] in ebay_ads.WRITABLE_STATES else "✗"
            print(f"{mark} promoted listings: {p['state']}" + (f" — {p['reason']}" if p.get("reason") else ""))
            return

        p = ebay_ads.plan(eb, ebay_ads.default_cap())
        _print_plan(p)
        if a.cmd == "plan":
            return
        if p["state"] not in ebay_ads.WRITABLE_STATES:
            sys.exit(f"not writing: {p['state']} — {p.get('reason')}")
        if not a.yes:
            sys.exit("dry run — re-run with `apply --yes` to write these rates")
        out = ebay_ads.apply(eb, p)
        print(f"✓ updated {out['updated']}, added {out['added']} listings to campaign {out['campaign_id']}")
    except (ebay_ads.EbayNotConfigured, ebay_ads.EbayError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
