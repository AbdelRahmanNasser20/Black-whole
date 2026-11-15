"""All-in cost block: a premium only for a source on file, never a tax number."""
from automation.web import lot_costs as lc


def test_known_source_shows_an_estimate():
    c = lc.estimate("govdeals", 200.0)
    assert c["known"] and c["pct"] == 12.5 and c["premium"] == 25.0 and c["total"] == 225.0
    assert lc.estimate("public_surplus", 100.0)["total"] == 110.0


def test_unknown_source_never_gets_a_number():
    for source in ("gsa", "purple_wave", "municibid", "mibid", "txauction", None, "ebay"):
        c = lc.estimate(source, 500.0)
        assert c == {"known": False, "pct": None, "bid": 500.0, "premium": None, "total": None,
                     "tax_note": lc.TAX_NOTE}


def test_no_bid_shows_the_rate_but_no_total():
    c = lc.estimate("allsurplus", None)
    assert c["known"] and c["pct"] == 12.5 and c["premium"] is None and c["total"] is None


def test_rates_come_from_the_fee_table_and_tax_is_words_only():
    from deals.fees import SITE_BUYER_PREMIUM
    assert lc.SOURCE_PREMIUM == {"govdeals": SITE_BUYER_PREMIUM["govdeals"],
                                 "allsurplus": SITE_BUYER_PREMIUM["allsurplus"],
                                 "public_surplus": SITE_BUYER_PREMIUM["publicsurplus"]}
    assert "not included" in lc.TAX_NOTE and not any(ch.isdigit() for ch in lc.TAX_NOTE)
