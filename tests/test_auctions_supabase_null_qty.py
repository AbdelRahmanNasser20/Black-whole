"""NULL-quantity rows must surface as degraded 'qty unknown' cards, not vanish.
db.fetch_all is monkeypatched — no DB, no LLM. Run:
  .venv/bin/python -m pytest tests/test_auctions_supabase_null_qty.py -v
"""
from automation import auctions_supabase as A


def _fake(rows):
    return lambda *a, **k: [dict(r) for r in rows]


def test_null_quantity_row_is_surfaced_as_unknown(monkeypatch):
    fake_rows = [
        {"asset_id": "ps:1", "link": "https://publicsurplus.com/a", "title": "Stack of banquet chairs",
         "description": "", "quantity": None, "quantity_source": "llm_failed",
         "quantity_confidence": None, "price": "$5", "location": "Tulsa, OK",
         "pickup_zip": "", "contact_email": "", "contact_phone": "",
         "end_date": "", "time_left": "", "image_url": "", "last_seen_at": None},
    ]
    monkeypatch.setattr(A.db, "fetch_all", _fake(fake_rows))
    out = A.get_top_chairs(source="ps", include_condition=False, active_only=False)
    assert len(out) == 1
    assert out[0]["quantity_unknown"] is True
    assert out[0]["quantity"] == 0


def test_known_quantity_row_is_not_unknown(monkeypatch):
    fake_rows = [
        {"asset_id": "gd:1", "link": "https://govdeals.com/a", "title": "300 banquet chairs",
         "description": "", "quantity": 300, "quantity_source": "llm",
         "quantity_confidence": "high", "price": "$5", "location": "Boise, ID",
         "pickup_zip": "", "contact_email": "", "contact_phone": "",
         "end_date": "", "time_left": "", "image_url": "", "last_seen_at": None},
    ]
    monkeypatch.setattr(A.db, "fetch_all", _fake(fake_rows))
    out = A.get_top_chairs(source="gd", include_condition=False, active_only=False)
    assert len(out) == 1
    assert out[0]["quantity_unknown"] is False
    assert out[0]["quantity"] == 300
