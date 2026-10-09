-- 025_deal_verdicts_flip.sql — GovAuctions-style flip analysis on deal_verdicts.
--
-- Status: PENDING — NOT applied to prod. Numbered 025 (024 is informally
-- reserved for the distress package).
-- Apply with: .venv/bin/python scripts/apply_sql.py scripts/sql/025_deal_verdicts_flip.sql
--
-- `flip` is the whole deals/flip.py card as JSONB (comp spread p25/p50/p75,
-- demand, projected close, max bid per 25/50/100% target margin); `flip_score`
-- is duplicated out as a REAL so it can be indexed/sorted like margin_pct.
-- Until this is applied the code degrades gracefully: deals/verdict_store.py
-- checks information_schema once per process (`_has_flip_columns`, positive
-- answers cached) and inserts verdicts without the two columns, printing a
-- one-line VERDICT NOTE. Additive + idempotent.

ALTER TABLE deal_verdicts
  ADD COLUMN IF NOT EXISTS flip JSONB,
  ADD COLUMN IF NOT EXISTS flip_score REAL;
