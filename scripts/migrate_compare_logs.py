"""One-shot importer for the A/B compare feature.

Reads:
  ~/.listing_automation/logs/llm_compare_*.json   — per-run dual-extractor logs
  ~/.listing_automation/compare_ratings.json       — operator's match/wrong marks

Writes to Supabase `llm_compare_logs`. Idempotent (ON CONFLICT DO UPDATE).

Run once after applying the `add_llm_compare_logs` migration:

    .venv/bin/python -m scripts.migrate_compare_logs

After verification, the JSON files can be deleted; new runs will write
straight to Supabase via automation/llm/__init__.py.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the repo root is importable when run as `python scripts/...`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config  # noqa: F401  — loads .env
from automation import db
from automation.config import LOG_DIR, STATE_ROOT

RATINGS_FILE = STATE_ROOT / "compare_ratings.json"


def _load_ratings() -> dict[str, str]:
    if not RATINGS_FILE.exists():
        return {}
    try:
        return json.loads(RATINGS_FILE.read_text())
    except Exception:
        return {}


def main() -> None:
    ratings = _load_ratings()
    logs = sorted(LOG_DIR.glob("llm_compare_*.json"))
    if not logs:
        print("no llm_compare_*.json files found; nothing to import")
        return

    rows = []
    skipped = 0
    for log in logs:
        m = re.search(r"llm_compare_(\d+)\.json", log.name)
        if not m:
            skipped += 1
            continue
        log_id = int(m.group(1))
        try:
            data = json.loads(log.read_text())
        except Exception as e:
            print(f"  skip {log.name}: {e}")
            skipped += 1
            continue

        ts_float = data.get("timestamp") or log_id
        ts = datetime.fromtimestamp(float(ts_float), tz=timezone.utc)
        rating = ratings.get(str(log_id))
        rated_at = ts if rating else None

        rows.append((
            log_id,
            ts,
            json.dumps(data.get("dom_hint")) if data.get("dom_hint") is not None else None,
            json.dumps(data.get("primary")) if data.get("primary") is not None else None,
            json.dumps(data.get("secondary")) if data.get("secondary") is not None else None,
            rating,
            rated_at,
        ))

    db.executemany(
        """
        INSERT INTO llm_compare_logs
            (id, ts, dom_hint, primary_extraction, secondary_extraction, rating, rated_at)
        VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            ts = EXCLUDED.ts,
            dom_hint = EXCLUDED.dom_hint,
            primary_extraction = EXCLUDED.primary_extraction,
            secondary_extraction = EXCLUDED.secondary_extraction,
            rating = EXCLUDED.rating,
            rated_at = EXCLUDED.rated_at
        """,
        rows,
    )
    total = db.fetch_one("SELECT count(*) AS n FROM llm_compare_logs")["n"]
    print(f"imported {len(rows)} logs (skipped {skipped}); total in Supabase: {total}")
    print(f"ratings carried over: {sum(1 for r in rows if r[5])}")


if __name__ == "__main__":
    main()
