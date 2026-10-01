-- 013 — tracked_lots landed cost (qty, premium, tax, GovDeals' all-in total).
-- Status: APPLIED to prod 2026-09-28 (19 closed lots backfilled). Re-run with
--   .venv/bin/python scripts/apply_sql.py scripts/sql/013_tracked_lots_costs.sql
-- Additive + nullable. The code runs before it is applied (cost columns just
-- stay empty and the Tracking tab shows estimates).
--
-- Every column mirrors the bidbox (GET /bids/bidbox/GD/{asset}/{account}/{auction}):
--   premium_pct  <- premiumPercent      (12.5 on most sellers, 10 on some)
--   admin_fee    <- adminFeeAmount
--   tax_total    <- totalTaxAmount      (tax on bid + tax on premium, 0 until sold)
--   grand_total  <- grandTotalAmount    (bid + premium + fees + tax, 0 until sold)
--   lot_state    <- state               (which seller's tax the estimate borrows)
-- quantity is the operator's override. NULL = parsed from the title at read time.

ALTER TABLE tracked_lots
  ADD COLUMN IF NOT EXISTS quantity    INT,
  ADD COLUMN IF NOT EXISTS premium_pct NUMERIC(6,3),
  ADD COLUMN IF NOT EXISTS admin_fee   NUMERIC(12,2),
  ADD COLUMN IF NOT EXISTS tax_total   NUMERIC(12,2),
  ADD COLUMN IF NOT EXISTS grand_total NUMERIC(12,2),
  ADD COLUMN IF NOT EXISTS lot_state   TEXT;
