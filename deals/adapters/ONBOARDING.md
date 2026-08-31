# Onboard <SITE> (<URL>) into the deal tracker

You are onboarding one auction site. Work only inside listing_automation/. Do not touch prod
credentials for bidding accounts. Budget: one session.

1. PROBE: .venv/bin/python scripts/onboard_probe.py <URL> --site <key>
   Read PROBE-REPORT.md. STOP and report back (do not build) if: legal != ok and the
   crawl-delay is not honorable, robots blocks the listing paths, access: blocked with a
   CAPTCHA, or the site requires login to see listings.
2. FIXTURES: capture 2 real pages (a search/category page + one lot page) into
   tests/deals/fixtures/<key>/. Trim to <500 KB each. Record the fetch URL in a comment.
3. ADAPTER: create deals/adapters/<key>.py with class <Key>Adapter: site = "<key>";
   discover(**kw) -> Iterator[Lot] (use models.synth_ids(site, native_id, ordinal=<N>) for the
   id trio; set native_id to the site's own stable lot id; price missing/garbled → raise
   ValueError, never 0); refetch(keys) -> dict[str, Snapshot] (re-fetch the lot pages; OK to
   be O(n) requests at Crawl-delay pace). fetch_detail optional.
4. TEST: tests/deals/test_<key>_adapter.py parses ONLY the saved fixtures (no network) and
   calls adapter_contract.check_lots(lots, site="<key>"). Add one test for the price-fails-loud
   path with a mutilated fixture record.
5. REGISTER: add SiteSpec to deals/sites.py with the next ordinal, a lot_url lambda built from
   native_id, enabled=False.
6. VERIFY: .venv/bin/python -m pytest tests/deals/ -q (all green), then a live dry-run:
   .venv/bin/python -m deals.cli discover --site <key> --limit 20 --dry-run
   (dry-run prints Lots, writes nothing).
7. REPORT: paste PROBE-REPORT verdict, lot count seen, 3 sample titles+prices, and any
   category/quantity parsing gaps. The operator flips enabled=True after reading.

Rules that override everything: logged-out only; honor Crawl-delay (min 2 s); stop on 403 or
challenge; no accounts; geo note — record state per lot so ingestion can filter GA/LA/IL/AZ+300mi.
