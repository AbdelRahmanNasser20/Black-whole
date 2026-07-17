# eBay bulk-upload CSV runbook (BLACKWHOLE-9)

Deterministic exporter that turns the inventory ledger into an eBay Seller Hub /
File Exchange bulk CSV to **create** (`Action=Add`) or **edit**
(`Action=Revise` + `ItemID`) fixed-price banquet-chair listings. No LLM, no
browser, read-only DB.

- Serializer: `automation/listing_csv.py` (pure, unit-tested in
  `tests/test_listing_csv.py`).
- CLI: `scripts/build_ebay_csv.py` → writes `catalog/ebay_bulk_upload.csv`.

## Build the CSV

```bash
python scripts/build_ebay_csv.py                 # sellable lots (feed set)
python scripts/build_ebay_csv.py --all           # every inventory row
python scripts/build_ebay_csv.py --quantity 50   # override listing quantity
```

Each sellable lot becomes one row. Lots that already have a stored `ebay_url`
become `Action=Revise` with the parsed `ItemID`; the rest become `Action=Add`.
Rows with no positive price or no durable (Supabase) photo URL are dropped.

## ⚠️ Operator input required before the CSV imports cleanly

The exact column contract is confirmed against the downloaded Seller Hub
template (the ticket's blocked item). Until then, set these and the exporter
fills them in:

| What | How | Why |
|------|-----|-----|
| **Category id** | `EBAY_BANQUET_CATEGORY_ID=<numeric leaf id>` in `.env` | eBay rejects `Add` rows with a blank `Category`. Left blank until set. |
| **Business Policies** | `EBAY_SHIPPING_PROFILE`, `EBAY_RETURN_PROFILE`, `EBAY_PAYMENT_PROFILE` | Appended as `ShippingProfileName` / `ReturnProfileName` / `PaymentProfileName` columns only when set. Freight/LTL shipping usually lives in a shipping policy. |
| **Required item specifics** | Confirm the exact required labels for the Banquet Chairs category against the template; adjust `_SPECIFIC_LABELS` in `listing_csv.py` if eBay's names differ. | We currently emit `Brand, Type, Color, Frame Material, Seat Material, Frame Color, Features` as `C:<Name>` columns. |

## Upload

Seller Hub → Listings → **Upload** (bulk). Review the report; fix any
category/item-specific validation errors, then re-run.
