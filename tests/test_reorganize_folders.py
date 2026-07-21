"""plan_moves buckets folders by status and flags unmatched ones. Pure. Run:
  .venv/bin/python -m pytest tests/test_reorganize_folders.py -v
"""
from scripts.reorganize_listing_folders import plan_moves, bucket_for_status


def test_bucket_mapping():
    assert bucket_for_status("listed") == "_active"
    assert bucket_for_status("draft") == "_active"
    assert bucket_for_status("sold_out") == "_sold"
    assert bucket_for_status("lost_sold_out") == "_lost"
    assert bucket_for_status("lost") == "_lost"
    assert bucket_for_status("hidden") == "_archive"
    assert bucket_for_status(None) == "_archive"


def test_plan_moves_buckets_and_flags_unmatched():
    rows = [
        {"folder_name": "Fresno_700", "status": "lost_sold_out"},
        {"folder_name": "FortSill_250", "status": "listed"},
        {"folder_name": None, "status": "listed"},          # no folder → skipped
    ]
    on_disk = ["Fresno_700", "FortSill_250", "General listing"]
    moves, unmatched = plan_moves(rows, on_disk)
    assert ("Fresno_700", "_lost") in moves
    assert ("FortSill_250", "_active") in moves
    assert "General listing" in unmatched      # on disk, no ledger row


def test_plan_moves_ignores_ledger_rows_absent_from_disk():
    rows = [{"folder_name": "GhostLot", "status": "listed"}]
    moves, unmatched = plan_moves(rows, on_disk=[])
    assert moves == []
    assert unmatched == []
