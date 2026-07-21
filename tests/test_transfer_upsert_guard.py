"""Guard: a failed-LLM NULL quantity must never erase a verified count, and a
stale re-transfer must never push last_seen_at backward. Pure string checks on
the module-level UPSERT — no DB. Run:
  .venv/bin/python -m pytest tests/test_transfer_upsert_guard.py -v
"""
from scripts.transfer_listings_to_supabase import UPSERT


def test_quantity_upsert_coalesces_incoming_null():
    # incoming NULL quantity must fall back to the stored value
    assert "COALESCE(EXCLUDED.quantity, auction_listings.quantity)" in UPSERT
    # and must NOT be a bare overwrite
    assert "quantity = EXCLUDED.quantity" not in UPSERT


def test_upsert_guards_against_stale_last_seen():
    assert "WHERE EXCLUDED.last_seen_at > auction_listings.last_seen_at" in UPSERT
