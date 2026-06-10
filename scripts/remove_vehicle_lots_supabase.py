"""One-shot cleanup: LLM-classify vehicle lots in Supabase ``auction_listings``
and delete them.

Why: the GovDeals JSON-API scrape path token-matches "chair" against
"Wheelchair"/"Wheel Chair", so wheelchair-accessible vans and other vehicles
leaked into the table (2026-06-10). The scraper now drops them at the source
(``_is_vehicle_lot``); this script removes the rows that already landed.

Flow: regex-prefilter candidate rows (vehicle nouns / automotive makes in the
title) → ask Gemini to confirm each is a vehicle lot, not seating → delete the
confirmed links. Dry-run by default; pass ``--apply`` to delete.

    .venv/bin/python scripts/remove_vehicle_lots_supabase.py [--apply]

Pass ``--sqlite <path>`` to clean a local ``listings.db`` scrape cache instead
of Supabase (the transfer script mirrors the WHOLE SQLite table, so vehicles
left in a local cache would resurrect in Supabase on the next sync).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import db
from automation.config import GEMINI_API_KEY, GEMINI_MODEL

_VEHICLE_WORDS = (
    "van|vans|truck|trucks|bus|buses|suv|suvs|sedan|sedans|pickup|pickups|"
    "minivan|ambulance|coupe|hatchback|forklift|ford|chevrolet|chevy|dodge|"
    "freightliner|gmc|toyota|honda|nissan|jeep|chrysler|mack|peterbilt|"
    "kenworth|isuzu"
)
CANDIDATE_TITLE_RE = rf"\m({_VEHICLE_WORDS})\M"          # Postgres ~* syntax
CANDIDATE_TITLE_PY = re.compile(rf"\b({_VEHICLE_WORDS})\b", re.IGNORECASE)

PROMPT = """\
You are cleaning a database of government-surplus auction lots scraped for a
CHAIR resale business. Each item below has an index, a title, and the start of
its description.

Classify each item: is the lot primarily a VEHICLE (car, van, truck, bus, SUV,
ambulance, forklift, trailer, etc.) rather than furniture/seating?

Notes:
- "Wheelchair accessible van" lots are VEHICLES.
- Lots of wheelchairs themselves (the mobility equipment) are NOT vehicles.
- Chair/table/furniture lots that merely mention a truck ("must load your own
  truck") are NOT vehicles.

Return ONLY a JSON array, one entry per item, like:
[{"index": 0, "is_vehicle": true}, {"index": 1, "is_vehicle": false}]

Items:
"""


def classify(rows: list[dict]) -> list[bool]:
    import time

    from google import genai
    from google.genai import errors

    client = genai.Client(api_key=GEMINI_API_KEY)
    lines = [
        f'{i}. title: {r["title"]!r} | description: {(r["descr"] or "")[:200]!r}'
        for i, r in enumerate(rows)
    ]
    resp = None
    last_err: Exception | None = None
    # 503s under load are common; walk through retries and a fallback model.
    for model in (GEMINI_MODEL,) * 4 + ("gemini-2.5-flash-lite",):
        try:
            resp = client.models.generate_content(
                model=model, contents=PROMPT + "\n".join(lines)
            )
            break
        except errors.APIError as e:
            print(f"  [llm] {model} unavailable ({e.code}); retrying…")
            last_err = e
            time.sleep(15)
    if resp is None:
        raise last_err
    text = resp.text or ""
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        raise ValueError(f"Gemini returned non-JSON: {text[:300]}")
    verdicts = {int(v["index"]): bool(v["is_vehicle"]) for v in json.loads(m.group(0))}
    missing = [i for i in range(len(rows)) if i not in verdicts]
    if missing:
        raise ValueError(f"Gemini omitted indices {missing}")
    return [verdicts[i] for i in range(len(rows))]


def _fetch_candidates_sqlite(path: str) -> list[dict]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT link, title, description FROM listings").fetchall()
    con.close()
    return [
        {"link": r["link"], "title": r["title"], "descr": (r["description"] or "")[:300]}
        for r in rows
        if CANDIDATE_TITLE_PY.search(r["title"] or "")
    ]


def main() -> int:
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY not set — aborting.")
        return 1
    apply = "--apply" in sys.argv
    sqlite_path = None
    if "--sqlite" in sys.argv:
        sqlite_path = sys.argv[sys.argv.index("--sqlite") + 1]

    if sqlite_path:
        rows = _fetch_candidates_sqlite(sqlite_path)
    else:
        rows = db.fetch_all(
            "SELECT link, title, left(description, 300) AS descr FROM auction_listings "
            "WHERE title ~* %s ORDER BY title",
            (CANDIDATE_TITLE_RE,),
        )
    print(f"{len(rows)} candidate row(s) match the vehicle-keyword prefilter.")
    if not rows:
        return 0

    flags = classify(rows)
    vehicles = [r for r, is_v in zip(rows, flags) if is_v]
    kept = [r for r, is_v in zip(rows, flags) if not is_v]

    print(f"\nLLM verdict: {len(vehicles)} vehicle(s), {len(kept)} kept as non-vehicle.\n")
    for r in vehicles:
        print(f"  DELETE  {r['title']}  ({r['link']})")
    for r in kept:
        print(f"  keep    {r['title']}")

    if not vehicles:
        return 0
    if not apply:
        print("\nDry run — re-run with --apply to delete.")
        return 0

    links = [r["link"] for r in vehicles]
    if sqlite_path:
        con = sqlite3.connect(sqlite_path)
        con.executemany("DELETE FROM listings WHERE link = ?", [(l,) for l in links])
        con.commit()
        n = con.execute("SELECT count(*) FROM listings").fetchone()[0]
        con.close()
        print(f"\nDeleted {len(links)} row(s); {sqlite_path} now holds {n}.")
    else:
        db.execute("DELETE FROM auction_listings WHERE link = ANY(%s)", (links,))
        remaining = db.fetch_one("SELECT count(*) AS n FROM auction_listings")
        print(f"\nDeleted {len(links)} row(s); auction_listings now holds {remaining['n']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
