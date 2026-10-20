"""`freight_log.insert_storefront_quote` with a carrier-priced quote (2026-10-20):
the carrier summary lands in the `carrier_*` columns of the SAME INSERT — one
request, one row — and an estimator quote leaves them NULL. Offline."""
from datetime import datetime

import pytest

from automation import db, freight_log

CARRIER = {
    "carrier_status": "ok", "carrier_low": 589.51, "carrier_name": "Averitt Express",
    "carrier_count": 7, "expires_at": "2026-10-23T18:00:00Z", "bookable": False,
    "carrier_options": [
        {"carrier": f"C{i}", "price_usd": 589.51 + i, "transit_days": 3,
         "expires_at": "2026-10-23T18:00:00Z", "bookable": False, "service_level": "STND"}
        for i in range(7)
    ],
}
WARP_QUOTE = {
    "mode": "ltl", "recommended_mode": "ltl", "ltl_low": 678, "ltl_high": 678,
    "partial_low": None, "partial_high": None, "miles": 912, "transit_days": 3,
    "provider": "warp", "valid_until": "2026-10-23",
    "accessorials": {"residential": True, "liftgate": True},
    "carrier": CARRIER, "without_liftgate": None,
    "raw": {"provider": "warp", "padding_pct": 15, "carrier_low": 589.51},
}


@pytest.fixture
def calls(monkeypatch):
    state = {"ready": True, "sql": []}

    def fetch_one(sql, params=None):
        if "information_schema.columns" in sql:
            return {"?column?": 1} if state["ready"] else None
        state["sql"].append((sql, params))
        return {"id": 78}

    freight_log.reset_schema_cache()
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    yield state
    freight_log.reset_schema_cache()


def _insert(quote):
    return freight_log.insert_storefront_quote(
        lot_id="bs-10121-53", origin_zip="04270", dest_zip="28206", quantity=100,
        quote=quote, buyer_email="buyer@example.com", buyer_phone="7045550100",
        client_ip="203.0.113.7", lot_quantity_remaining=360,
    )


def test_carrier_quote_writes_the_carrier_columns_in_the_same_insert(calls):
    assert _insert(WARP_QUOTE) == 78
    (sql, params) = calls["sql"][-1]
    assert sql.count("INSERT INTO freight_quotes") == 1
    for col in ("carrier_status", "carrier_low", "carrier_name", "carrier_count",
                "carrier_options", "carrier_checked_at"):
        assert col in sql.split("VALUES")[0], col
    assert "warp" in params and 678 in params
    assert "ok" in params and 589.51 in params and "Averitt Express" in params and 7 in params
    options_json = next(p for p in params if isinstance(p, str) and '"carrier"' in p)
    assert options_json.count('"carrier"') == freight_log.CARRIER_OPTIONS_MAX   # capped like set_carrier_result
    assert '"expires_at": "2026-10-23T18:00:00Z"' in options_json and '"bookable": false' in options_json
    assert any(isinstance(p, datetime) for p in params)                          # carrier_checked_at = now
    assert "2026-10-23" in params                                                # valid_until = the quote's expiry day


def test_estimator_quote_leaves_the_carrier_columns_null(calls):
    est = dict(WARP_QUOTE, provider="estimator", ltl_low=1080, ltl_high=1520, carrier=None,
               raw={"provider": "estimator", "carrier_fallback": "timeout"})
    assert _insert(est) == 78
    (_sql, params) = calls["sql"][-1]
    assert "estimator" in params and "ok" not in params and "Averitt Express" not in params
    assert not any(isinstance(p, datetime) for p in params)


def test_legacy_schema_insert_ignores_the_carrier_summary(calls):
    calls["ready"] = False
    assert _insert(WARP_QUOTE) == 78
    (sql, params) = calls["sql"][-1]
    assert "carrier_status" not in sql
    assert "warp" in params and 678 in params
