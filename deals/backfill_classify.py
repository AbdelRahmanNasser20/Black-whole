"""Re-classify the lots that were never actually classified.

Background: `deals/classify.py` built its prompt with `str.format` over a
template containing a literal JSON example, so every call raised
`KeyError('"label"')` before reaching the network and a bare `except` turned
that into `("other", 0.0)`. The table held 39,758 rows labelled `other` at
confidence exactly 0.0 — min and max confidence both 0.0 across every one of
them, which is the fingerprint of an answer nobody ever gave.

Two operations, deliberately separate:

  `reset_fakes()`  blanks the provable fakes. NULL means "we don't know", which
  is true; `other`/0.0 asserts a fact that was never checked. Safe because those
  values carry zero information — byte-identical across the whole table, and
  produced without a single HTTP request ever leaving the machine.

  `backfill()`  fills pending rows with real answers, rate-limited to the
  provider's published ceiling. Resumable by construction: a filled row is no
  longer pending, so re-running continues where the last run stopped, and once
  the backlog is empty it is a no-op. That is what lets it live as an hourly
  cron instead of a three-day chore someone has to remember.

Only lots whose deterministic category is ambiguous (`general_merchandise` /
`other`) are sent to the LLM — the same gate `run_discovery` applies, so quota
is never spent on a lot the code map already placed confidently.
"""
import sys
import time

from automation import db
from deals.classify import ClassificationUnavailable, breaker_state, classify_category

AMBIGUOUS = ("general_merchandise", "other")

PENDING_SQL = """
    SELECT asset_id, account_id, auction_id, title, description
    FROM deal_lots
    WHERE canonical_category = ANY(%s)
      AND llm_category IS NULL
      AND title IS NOT NULL
    ORDER BY first_seen_at DESC
    LIMIT %s
"""

COUNT_SQL = """
    SELECT count(*) AS n FROM deal_lots
    WHERE canonical_category = ANY(%s) AND llm_category IS NULL AND title IS NOT NULL
"""

UPDATE_SQL = """
    UPDATE deal_lots
    SET llm_category = %s, llm_category_confidence = %s, category_agreement = %s
    WHERE asset_id = %s AND account_id = %s AND auction_id = %s
"""

RESET_SQL = """
    UPDATE deal_lots
    SET llm_category = NULL, llm_category_confidence = NULL, category_agreement = NULL
    WHERE llm_category = 'other' AND llm_category_confidence = 0
"""


def pending_count() -> int:
    return db.fetch_one(COUNT_SQL, (list(AMBIGUOUS),))["n"]


def reset_fakes() -> int:
    audit = db.fetch_one("""
        SELECT count(*) AS n, min(llm_category_confidence) AS lo,
               max(llm_category_confidence) AS hi
        FROM deal_lots WHERE llm_category = 'other' AND llm_category_confidence = 0
    """)
    print(f"{audit['n']} rows labelled other at confidence "
          f"{audit['lo']}..{audit['hi']} — blanking them.")
    n = db.execute(RESET_SQL)
    print(f"reset {n} rows to NULL")
    return n


def backfill(limit: int = 450, rpm: int = 18) -> dict:
    rows = db.fetch_all(PENDING_SQL, (list(AMBIGUOUS), limit))
    if not rows:
        print("nothing pending")
        return {"done": 0, "failed": 0}
    # Stay under the provider's tokens-per-minute ceiling. Groq's free
    # llama-3.1-8b-instant allows 6,000 tok/min against a ~290-token prompt,
    # so ~20/min is the real ceiling and 18 leaves margin. Overrunning earns a
    # 429, and with the breaker armed that would halt the whole run.
    delay = 60.0 / max(rpm, 1)
    done = failed = 0
    for i, r in enumerate(rows, 1):
        try:
            label, conf = classify_category(r["title"], r["description"] or "")
        except ClassificationUnavailable as e:
            failed += 1
            print(f"  stopped after {done}: {e}", file=sys.stderr)
            if breaker_state()["tripped"]:
                break
            continue
        db.execute(UPDATE_SQL, (label, conf, None,
                                r["asset_id"], r["account_id"], r["auction_id"]))
        done += 1
        if i % 50 == 0:
            print(f"  {i}/{len(rows)}  (last: {label} {conf:.2f})")
        time.sleep(delay)
    return {"done": done, "failed": failed}


def run(limit: int = 450, rpm: int = 18, reset: bool = False) -> dict:
    if reset:
        return {"reset": reset_fakes()}
    print(f"{pending_count()} lots pending classification")
    return backfill(limit, rpm)
