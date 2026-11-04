-- 035: every chair inquiry carries a ZIP code.
-- A sell inquiry = where the chairs sit; a buy inquiry = where they would go.
-- Without it the lead is un-routable (no freight lane, no nearest lot).
-- Until applied, inventory.create_inquiry folds the ZIP into `message` instead.
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS zip_code VARCHAR(10);
