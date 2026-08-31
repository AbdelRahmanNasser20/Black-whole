import pytest


def test_registry_has_govdeals_enabled():
    from deals.sites import SITES, enabled_sites
    assert SITES["govdeals"].ordinal == 1 and "govdeals" in enabled_sites()


def test_lot_url_dispatches_by_site(make_lot):
    from deals.sites import lot_url
    assert lot_url(make_lot(asset_id=5, account_id=9, auction_id=2)) == \
        "https://www.govdeals.com/en/asset/5/9"   # asset first, account second — repo memory: swapped = HTTP 204


def test_lot_url_accepts_db_row_dicts():
    # digest/relist/saved-search alerts build URLs from fetch_all dict rows
    from deals.sites import lot_url
    assert lot_url({"asset_id": 5, "account_id": 9, "auction_id": 2}) == \
        "https://www.govdeals.com/en/asset/5/9"


def test_unknown_site_fails_loud(make_lot):
    from deals.sites import lot_url
    with pytest.raises(KeyError):
        lot_url(make_lot(site="nope"))
