"""Pure-logic tests for multi-location lots + the sold showcase (BLACKWHOLE-29).

DB-free: every function under test is a plain transform over a row dict.
"""
import pytest

from automation import inventory


# ───────────────────────────── parse_locations ─────────────────────────────

def test_parse_locations_accepts_a_list_of_dicts():
    got = inventory.parse_locations([
        {"city": "Baltimore", "state": "MD", "quantity": 1200},
        {"city": "Atlanta", "state": "GA"},
    ])
    assert got == [
        {"city": "Baltimore", "state": "MD", "quantity": 1200},
        {"city": "Atlanta", "state": "GA"},
    ]


def test_parse_locations_accepts_the_admin_text_form():
    # What an operator types into one cell on the Inventory tab.
    got = inventory.parse_locations(
        "Baltimore, MD x1200; Atlanta, GA x399; Orlando, FL"
    )
    assert got == [
        {"city": "Baltimore", "state": "MD", "quantity": 1200},
        {"city": "Atlanta", "state": "GA", "quantity": 399},
        {"city": "Orlando", "state": "FL"},
    ]


def test_parse_locations_tolerates_a_json_string():
    got = inventory.parse_locations('[{"city": "Orlando", "state": "FL"}]')
    assert got == [{"city": "Orlando", "state": "FL"}]


@pytest.mark.parametrize("value", [None, "", "   ", [], "  ;  ; "])
def test_parse_locations_treats_empty_as_cleared(value):
    assert inventory.parse_locations(value) is None


def test_parse_locations_drops_entries_with_no_city():
    assert inventory.parse_locations([{"state": "MD"}, {"city": "Tulsa"}]) == [
        {"city": "Tulsa"}
    ]


def test_parse_locations_rejects_a_non_numeric_quantity():
    with pytest.raises(ValueError):
        inventory.parse_locations("Baltimore, MD xlots")


def test_parse_locations_does_not_mistake_a_city_x_for_a_quantity():
    # "Lexington" must not parse as city "Le" + quantity "ington".
    assert inventory.parse_locations("Lexington, KY; Xenia, OH") == [
        {"city": "Lexington", "state": "KY"},
        {"city": "Xenia", "state": "OH"},
    ]


# ───────────────────────────── location_labels ─────────────────────────────

def test_location_labels_prefers_the_locations_column():
    row = {
        "city": "Baltimore", "state": "MD",
        "locations": [{"city": "Baltimore", "state": "MD"},
                      {"city": "Atlanta", "state": "GA"},
                      {"city": "Orlando", "state": "FL"}],
    }
    assert inventory.location_labels(row) == [
        "Baltimore, MD", "Atlanta, GA", "Orlando, FL",
    ]


def test_location_labels_falls_back_to_the_primary_city():
    assert inventory.location_labels(
        {"city": "Phoenix", "state": "AZ", "locations": None}
    ) == ["Phoenix, AZ"]


def test_location_labels_is_empty_when_the_lot_has_no_place_at_all():
    assert inventory.location_labels({"city": None, "state": None}) == []


# ───────────────────────────────── is_sold ─────────────────────────────────

@pytest.mark.parametrize("status", ["sold_out", "lost_sold_out"])
def test_is_sold_covers_both_sold_statuses(status):
    assert inventory.is_sold({"status": status}) is True


@pytest.mark.parametrize("status", ["listed", "owned", "draft", "active_bid"])
def test_is_sold_is_false_for_live_stock(status):
    assert inventory.is_sold({"status": status}) is False


# ─────────────────────────── lost_sold_out is valid ────────────────────────

def test_lost_sold_out_is_an_accepted_status():
    # The DB CHECK constraint already allows it (lots we never owned, shown as
    # sold). set_fields used to reject it, so the admin couldn't set it.
    assert "lost_sold_out" in inventory.ALL_STATUSES
