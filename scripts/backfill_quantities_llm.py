"""Backfill quantities on auction_listings rows that were parsed by regex.

One-off corrective pass for BLACKWHOLE-4: the regex quantity path misreads
comma-formatted counts ("2,100" -> 2) and lot/asset numbers ("UAB ... (1)" ->
105). Re-runs the Groq LLM quantity pass over every row whose quantity_source
is regex_* and updates the row when the LLM returns a confident value.

Safe to re-run (idempotent-ish: only flips regex_* rows to llm). Reads creds
from auction_extractors/.env. Run:  python scripts/backfill_quantities_llm.py
"""
import os
import sys

import automation.config  # loads repo .env (BLACKWHOLE_DB_URL)
from automation import db

sys.path.insert(0, "auction_extractors")
from dotenv import load_dotenv

load_dotenv("auction_extractors/.env")
from quantity_llm import refine_quantities_with_llm  # noqa: E402

BATCH = 12
SOURCES = ("regex_fulltext", "regex_title", "llm_failed", "llm_missing")
PROVIDER = os.getenv("LLM_PROVIDER_BACKFILL", "gemini")


def main() -> None:
    rows = db.fetch_all(
        "SELECT asset_id, title, description, quantity AS old_q, quantity_source "
        "FROM auction_listings WHERE quantity_source = ANY(%s) ORDER BY asset_id",
        (list(SOURCES),),
    )
    print(f"{len(rows)} rows to re-parse (sources: {SOURCES})", flush=True)
    updated = changed = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        try:
            out = refine_quantities_with_llm(
                [{"title": r["title"], "description": r.get("description") or ""} for r in chunk],
                provider=PROVIDER,
                ollama_base_url="",
                ollama_model="",
                ollama_timeout=300,
                groq_api_key=os.getenv("GROQ_API_KEY"),
                openai_api_key=os.getenv("OPENAI_API_KEY"),
                gemini_api_key=os.getenv("GEMINI_API_KEY"),
            )
        except Exception as e:  # noqa: BLE001
            print(f"  batch {i // BATCH} failed: {e!r}", flush=True)
            continue
        for r, o in zip(chunk, out):
            q = o.get("quantity")
            if not q:
                continue
            db.execute(
                "UPDATE auction_listings SET quantity=%s, quantity_source='llm', "
                "quantity_confidence=%s WHERE asset_id=%s",
                (q, o.get("quantity_confidence", "medium"), r["asset_id"]),
            )
            updated += 1
            if q != r["old_q"]:
                changed += 1
        if (i // BATCH) % 10 == 0:
            print(f"  ...{i + len(chunk)}/{len(rows)} done, {changed} changed", flush=True)
    print(f"DONE: {updated} rows set to llm-source, {changed} quantities changed", flush=True)


if __name__ == "__main__":
    main()
