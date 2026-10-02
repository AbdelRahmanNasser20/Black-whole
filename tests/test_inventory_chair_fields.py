"""Chair data per lot (`inventory.set_fields` → migration 021 columns).

Operator-entered weight / frame / chairs-per-pallet / pallet height. Blank
clears a field back to the standard chair; a slipped decimal is a ValueError
(the API turns it into a 400) rather than a freight quote for a 130 lb chair.
"""
import inspect
from pathlib import Path

import pytest

from automation import inventory


@pytest.fixture
def writes(monkeypatch):
    statements: list[tuple[str, list]] = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            statements.append((sql, list(params or [])))
            return self

        def commit(self):
            pass

    monkeypatch.setattr(inventory, "connect", lambda: FakeConn())
    monkeypatch.setattr(inventory, "get", lambda lot_id: {"lot_id": lot_id, "status": "listed"})
    return statements


def _set(writes, **fields):
    inventory.set_fields("31225", **fields)
    sql, params = writes[-1]
    cols = [c.split(" = ")[0].strip() for c in sql.split("SET", 1)[1].split("WHERE")[0].split(",")]
    return dict(zip(cols, params))


def test_chair_fields_are_written(writes):
    row = _set(writes, chair_weight_lb="13.456", chair_frame="  steel, powder-coat ",
               chairs_per_pallet="35", pallet_height_in=79.6)
    assert row["chair_weight_lb"] == 13.46
    assert row["chair_frame"] == "steel, powder-coat"
    assert row["chairs_per_pallet"] == 35 and isinstance(row["chairs_per_pallet"], int)
    assert row["pallet_height_in"] == 80


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_clears_back_to_the_standard_chair(writes, blank):
    row = _set(writes, chair_weight_lb=blank, chair_frame=blank,
               chairs_per_pallet=blank, pallet_height_in=blank)
    assert row["chair_weight_lb"] is None and row["chair_frame"] is None
    assert row["chairs_per_pallet"] is None and row["pallet_height_in"] is None


@pytest.mark.parametrize("field, value", [
    ("chair_weight_lb", 130),      # 13.0 with a slipped decimal
    ("chair_weight_lb", 0),
    ("chair_weight_lb", -4),
    ("chair_weight_lb", "heavy"),
    ("chair_weight_lb", True),     # JSON `true` must not become a 1 lb chair
    ("chairs_per_pallet", 3500),
    ("chairs_per_pallet", 0),
    ("pallet_height_in", 6),
    ("pallet_height_in", 400),
])
def test_out_of_range_chair_data_is_refused(writes, field, value):
    with pytest.raises(ValueError) as err:
        inventory.set_fields("31225", **{field: value})
    assert field in str(err.value)
    assert writes == []


def test_frame_is_capped(writes):
    assert len(_set(writes, chair_frame="x" * 500)["chair_frame"]) == inventory.CHAIR_FRAME_MAX_LEN


def test_a_pipeline_rerun_never_touches_chair_data():
    """HARD RULE: `upsert_from_run` preserves operator edits. It names every
    column it writes — the chair columns must stay off that list."""
    src = inspect.getsource(inventory.upsert_from_run)
    for col in ("chair_weight_lb", "chair_frame", "chairs_per_pallet", "pallet_height_in"):
        assert col not in src, col


def test_inventory_tab_offers_the_four_fields():
    js = Path("automation/web/static/admin/inventory.js").read_text()
    for field in ("chair_weight_lb", "chair_frame", "chairs_per_pallet", "pallet_height_in"):
        assert f'data-field="{field}"' in js, field
    # the numeric ones are sent as numbers, blank as null
    for field in ("chair_weight_lb", "chairs_per_pallet", "pallet_height_in"):
        assert f"'{field}'" in js.split("const NUMERIC_FIELDS")[1].split(";")[0], field
    # a number box holding junk reads back as '' — it must not be sent as "clear this"
    assert "validity.badInput" in js
