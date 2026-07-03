CREATE OR REPLACE VIEW deal_candidates AS
SELECT asset_id, account_id, auction_id, title, current_bid, bid_count,
       city, state, end_utc, canonical_category
FROM deal_lots
WHERE outcome_complete IS NOT TRUE AND bid_count = 0 AND is_free = false
  AND currency_code = 'USD' AND end_utc <= now() + interval '24 hours'
ORDER BY end_utc ASC;
