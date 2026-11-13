-- Affirm financing links — a third `deposits.kind`.
--
-- A 'financed' row is a Checkout session the OPERATOR creates from the admin
-- Deposits view for a negotiated amount (e.g. $4,500 of a $9,000 order, the
-- rest paid in cash at pickup). The session offers Affirm only; Affirm pays us
-- the full amount up front and the buyer repays Affirm.
--
-- Shape reuse: `subtotal_cents` = the whole order, `amount_cents` = the part
-- financed now, `deposit_pct`/`deposit_min_cents` stay NULL (no rule applies).
-- `payment_method` will read 'affirm'. No new columns, so no new archival path.
--
-- The public /reserve checkout still only accepts 'deposit' | 'full' — the
-- route validates against `deposits.DEPOSIT_KINDS`, which does not include
-- 'financed'. Only the auth-walled /api/deposits/financing-link writes it.
--
-- RLS: `deposits` already has RLS + `service_role_all` (029). Constraint-only
-- change, nothing to add.
--
-- Apply: .venv/bin/python scripts/apply_sql.py scripts/sql/034_deposits_financed.sql
-- Applied to Supabase project nihgzltpjriekyqqucbd on: PENDING

ALTER TABLE deposits DROP CONSTRAINT IF EXISTS deposits_kind_check;
ALTER TABLE deposits ADD CONSTRAINT deposits_kind_check
    CHECK (kind IN ('deposit', 'full', 'financed'));
