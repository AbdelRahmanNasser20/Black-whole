"""A re-scrape whose LLM failed (quantity=None) must keep the stored quantity.
Uses a temp SQLite file. Run:
  .venv/bin/python -m pytest tests/test_listings_db_quantity_preserve.py -v
"""
from auction_extractors import listings_db as ldb


def _base_listing(**over):
    row = {
        "link": "https://www.publicsurplus.com/sms/auction/view?auc=999123",
        "title": "200 Chairs", "description": "", "quantity": 200,
        "quantity_source": "llm", "quantity_confidence": "high",
        "price": "$1", "location": "Tulsa, OK", "lot_number": "1",
        "end_date": "", "time_left": "", "image_url": "",
        "pickup_zip": "", "contact_email": "", "contact_phone": "",
    }
    row.update(over)
    return row


def test_null_quantity_does_not_clobber_stored_count(tmp_path):
    conn = ldb.connect(tmp_path / "listings.db")
    assert ldb.upsert_listing(conn, _base_listing()) == "insert"          # store 200
    ldb.upsert_listing(conn, _base_listing(quantity=None,                  # failed re-scrape
                                           quantity_source="llm_failed",
                                           quantity_confidence=None))
    row = ldb.get_cached(conn, ldb.extract_asset_id(_base_listing()["link"]))
    assert row["quantity"] == 200                    # preserved, not None
    assert row["quantity_source"] == "llm"           # source preserved too


def test_real_new_quantity_still_overwrites(tmp_path):
    conn = ldb.connect(tmp_path / "listings.db")
    ldb.upsert_listing(conn, _base_listing())                              # 200
    ldb.upsert_listing(conn, _base_listing(quantity=250))                 # verified update
    row = ldb.get_cached(conn, ldb.extract_asset_id(_base_listing()["link"]))
    assert row["quantity"] == 250
