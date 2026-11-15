"""Public lot URL codec: /deals/lot/{title-slug}-{st}-{source}-{key}.

Only source + key resolve a lot; the text part is decoration (a wrong one 301s).
"""
import pytest

from automation.web import deal_lot_urls as u


@pytest.mark.parametrize("source,key", [
    ("govdeals", "335/11741/60"),
    ("allsurplus", "9001/20948"),
    ("txauction", "4121/88213"),
    ("gsa", "31QSCI26499005"),
    ("purple_wave", "260804/EW0441"),
    ("public_surplus", "4054005"),
    ("municibid", "86233903"),
    ("mibid", "0f8fad5b-d9cb-469f-a165-70867728950e"),
])
def test_round_trip_for_every_source(source, key):
    path = u.build(source, key, title="335 Banquet Chairs - New!", state="NJ")
    assert path.startswith("/deals/lot/335-banquet-chairs-new-nj-")
    assert u.parse(path.rsplit("/", 1)[1]) == (source, key)


def test_slug_is_stable_ascii_and_bounded():
    path = u.build("govdeals", "1/2/3", title="  Café  tables & (40) CHAIRS!! " + "x" * 200, state=None)
    tail = path.rsplit("/", 1)[1]
    assert tail.startswith("cafe-tables-40-chairs-")
    assert "-us-govdeals-1.2.3" in tail and len(tail) < 110
    assert u.build("govdeals", "1/2/3", title=None, state="az") == "/deals/lot/lot-az-govdeals-1.2.3"


def test_text_part_never_decides_the_lot():
    assert u.parse("anything-at-all-zz-govdeals-1.2.3") == ("govdeals", "1/2/3")
    assert u.parse("govdeals-1.2.3") == ("govdeals", "1/2/3")          # no text part at all


@pytest.mark.parametrize("tail", ["", "nope", "a-b-ebay-123", "a-nj-govdeals-", "a-nj-govdeals-1/2", "a-nj-govdeals-..%2f"])
def test_bad_tails_do_not_parse(tail):
    assert u.parse(tail) is None


def test_unknown_source_or_unsafe_key_has_no_url():
    assert u.build("ebay", "1", title="x", state="NJ") is None
    assert u.build("gsa", "a b", title="x", state="NJ") is None
    assert u.build("gsa", "has.dot", title="x", state="NJ") is None    # '.' is the escape for '/'
    assert u.build("gsa", "", title="x", state="NJ") is None


def test_for_row_reads_both_row_shapes():
    gd = {"source": "govdeals", "asset_id": 5, "account_id": 6, "auction_id": 7, "title": "Desk", "state": "AZ"}
    assert u.for_row(gd) == "/deals/lot/desk-az-govdeals-5.6.7"
    tx = {"source": "txauction", "native_id": "41/99", "asset_id": 123, "account_id": -10, "auction_id": 0,
          "title": "Truck", "state": "TX"}
    assert u.for_row(tx) == "/deals/lot/truck-tx-txauction-41.99"
    snap = {"source": "municibid", "source_lot_id": "86233903", "title": "Mower", "state": "PA"}
    assert u.for_row(snap) == "/deals/lot/mower-pa-municibid-86233903"
    assert u.for_row({"source": "hibid", "source_lot_id": "1", "title": "x"}) is None
