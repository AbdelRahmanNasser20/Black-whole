"""Offline tests for the public freight-estimate endpoints (A2).

Same shape as ``tests/web/test_auth.py`` and ``test_reserve.py``: a
``TestClient`` built WITHOUT a ``with`` block so the lifespan (and its
DB-touching alert scheduler) never runs, every DB call site monkeypatched, and
no network anywhere — including Telegram, which is stubbed by an autouse
fixture so a dev machine with a real bot token can't page the operator from a
test run.

The estimator itself is NOT stubbed. It's pure arithmetic over a committed
lookup table, and the properties worth defending here are exactly the ones a
mock would erase:

  1. **The buyer never sees `raw`.** The calibration internals (weight, NMFC
     class, our per-cwt table, a carrier's own response) are the audit trail
     for a quote, not a spec sheet. `test_response_never_leaks_raw` walks the
     whole JSON tree, not just the top level.
  2. **The origin is ours.** A ZIP in the payload must not move the lane; the
     server reads it off the inventory row or the state's capital.
  3. **An unquotable lane is a 200, not a 500 and never a guess.** Canada,
     Hawaii and Alaska all come back `ok: false` with the hand-quote copy.
  4. **Every request is a lead on file.** Email and phone are required, the row
     is written for quoted AND unquotable lanes, and a price is only returned
     once the row exists. The alert carries the contact details, so even a
     failed write leaves a record.
"""
import asyncio
import logging
import threading
import sys

import pytest
from fastapi.testclient import TestClient

from automation import freight_estimate, freight_log, inventory, telegram_alerts, warp_rates
from automation.web import auth as auth_svc
from automation.web import rate_limit
from automation.web.app import app

# `automation.web.__init__` re-exports the FastAPI instance as `automation.web.app`,
# which shadows the submodule of the same name — reach for the module itself
# through sys.modules rather than an import statement.
app_module = sys.modules["automation.web.app"]

# The pinned lane, same one tests/test_freight_estimate.py holds the estimator
# to: Boise ID 83702 → Worcester MA 01608, 150 chairs.
DEST_WORCESTER = "01608"
EMAIL = "buyer@example.com"
PHONE = "(404) 555-0100"
PHONE_DIGITS = "4045550100"
# What a buyer's browser posts. Tests that build their own body start from this.
BODY = {"lot_id": "31225", "dest_zip": DEST_WORCESTER, "email": EMAIL, "phone": PHONE}

# The real alert coroutine, captured before the autouse spy replaces it.
REAL_NOTIFY = app_module._notify_freight_estimate
REAL_CARRIER_TASK = app_module._carrier_check_task


# ─────────────────────────────── fixtures ───────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Fresh rate-limit counters, no carrier keys, no Telegram, no auth."""
    rate_limit.reset()
    app_module._carrier_reset()
    monkeypatch.delenv("WARP_API_KEY", raising=False)
    monkeypatch.delenv("WARP_ENV", raising=False)
    for var in ("ADMIN_PASSWORD", "SESSION_SECRET", "TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)
    auth_svc.reset_caches()
    yield
    rate_limit.reset()
    auth_svc.reset_caches()


def make_lot(**over) -> dict:
    lot = {
        "lot_id": "31225",
        "title": "Mity-Lite folding chairs",
        "status": "listed",
        "price_per_chair": 12,
        "quantity_remaining": 400,
        "quantity_original": 945,
        "city": "Boise",
        "state": "ID",
        "zip_code": "83702",
    }
    lot.update(over)
    return lot


@pytest.fixture
def lot(monkeypatch):
    """`inventory.get` returns one in-memory lot; mutate it per test."""
    row = make_lot()
    monkeypatch.setattr(
        inventory, "get",
        lambda lot_id: dict(row) if lot_id == row["lot_id"] else None,
    )
    return row


@pytest.fixture
def logged(monkeypatch):
    """Capture what would have been written to `freight_quotes`."""
    calls = {"inserts": [], "emails": []}

    def fake_insert(**kw):
        calls["inserts"].append(kw)
        return 4242

    def fake_set_email(quote_id, email):
        calls["emails"].append((quote_id, email))
        return True

    monkeypatch.setattr(freight_log, "insert_storefront_quote", fake_insert)
    monkeypatch.setattr(freight_log, "set_quote_email", fake_set_email)
    return calls


@pytest.fixture(autouse=True)
def notified(monkeypatch):
    """Count Telegram pings. The spy records SYNCHRONOUSLY (at create_task
    time) so the assertion doesn't depend on the task getting scheduled."""
    seen = {"estimates": [], "emails": []}

    async def _noop():
        return None

    def spy_estimate(row, quote, **kw):
        seen["estimates"].append({"row": row, "quote": quote, **kw})
        return _noop()

    def spy_email(quote_id, email):
        seen["emails"].append((quote_id, email))
        return _noop()

    monkeypatch.setattr(app_module, "_notify_freight_estimate", spy_estimate)
    monkeypatch.setattr(app_module, "_notify_freight_email", spy_email)
    return seen


@pytest.fixture(autouse=True)
def carrier(monkeypatch):
    """The background carrier check never runs for real in this file — record
    which quote ids it was started for instead (no thread, no Warp call)."""
    started = []

    async def _noop():
        return None

    def spy(quote_id):
        started.append(quote_id)
        return _noop()

    monkeypatch.setattr(app_module, "_carrier_check_task", spy)
    return started


def _client():
    # https so any Secure cookie round-trips; no `with` so lifespan never fires.
    return TestClient(app, base_url="https://testserver")


def _estimate(client, **body):
    payload = dict(BODY, quantity=150)
    payload.update(body)
    return client.post("/freight-estimate", json=payload)


def _walk(obj):
    """Every key in a nested JSON structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


# ──────────────────────────────── happy path ────────────────────────────────

def test_happy_path_response_shape(lot, logged):
    r = _estimate(_client())
    assert r.status_code == 200
    body = r.json()

    assert body["ok"] is True
    assert body["quote_id"] == 4242
    assert set(body) == {"ok", "quote_id", "estimate", "framing", "available"}
    assert body["available"] is None
    assert set(body["estimate"]) == {
        "mode", "recommended_mode", "ltl", "partial", "miles", "transit_days",
        "valid_until",
    }
    assert body["framing"] == {
        "estimate_only": True,
        "residential_liftgate_included": True,
        "chair_price_separate": True,
        "pickup_free": True,
    }


def test_pinned_lane_numbers(lot, logged):
    """Boise → Worcester × 150 must land where the estimator's own test pins it.

    This is the end-to-end version of tests/test_freight_estimate.py's pinned
    lane: if the route ever quietly stops using the row's real origin (or the
    calibration drifts), the range moves and this fails.
    """
    est = _estimate(_client()).json()["estimate"]
    assert est["mode"] == "ltl"
    assert est["recommended_mode"] == "ltl"
    assert est["partial"] is None
    assert 1000 <= est["ltl"]["low"] <= 1300
    assert 1450 <= est["ltl"]["high"] <= 1800
    assert 2500 <= est["miles"] <= 2800
    assert est["transit_days"] == 7
    assert est["valid_until"]


def test_response_never_leaks_raw(lot, logged):
    """`raw` is logged server-side and stripped at the boundary — both halves."""
    body = _estimate(_client()).json()
    assert "raw" not in set(_walk(body))
    # …but the row we'd write keeps it: that's the audit trail.
    assert "raw" in logged["inserts"][0]["quote"]
    assert logged["inserts"][0]["quote"]["raw"]["nmfc_class"] == 175


def test_logged_row_records_the_lane_and_the_caller(lot, logged):
    _client().post(
        "/freight-estimate",
        json=dict(BODY, quantity=150),
        headers={"cf-connecting-ip": "203.0.113.7"},
    )
    (call,) = logged["inserts"]
    assert call["lot_id"] == "31225"
    assert call["origin_zip"] == "83702"
    assert call["dest_zip"] == DEST_WORCESTER
    assert call["quantity"] == 150
    assert call["client_ip"] == "203.0.113.7"
    assert call["buyer_email"] == EMAIL
    assert call["buyer_phone"] == PHONE_DIGITS       # normalised, not as typed
    assert call["unquotable_reason"] is None
    assert call["lot_quantity_remaining"] == 400


def test_a_failed_save_shows_no_price_and_still_alerts(monkeypatch, lot, notified, carrier):
    """The row is the product. No row ⇒ no number on screen — but the operator
    is still paged with the contact details, so the lead is not lost."""
    monkeypatch.setattr(
        freight_log, "insert_storefront_quote", lambda **kw: None
    )
    r = _estimate(_client())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "not_saved"
    assert "estimate" not in body
    (ping,) = notified["estimates"]
    assert ping["quote_id"] is None
    assert ping["email"] == EMAIL and ping["phone"] == PHONE_DIGITS
    assert ping["quote"]["ltl_low"] > 0          # the operator still sees the range
    assert carrier == []                          # nothing to attach prices to


def test_telegram_pings_exactly_once_per_estimate(lot, logged, notified):
    client = _client()
    _estimate(client)
    assert len(notified["estimates"]) == 1
    _estimate(client)
    assert len(notified["estimates"]) == 2
    ping = notified["estimates"][0]
    assert (ping["dest_zip"], ping["quantity"], ping["quote_id"]) == (DEST_WORCESTER, 150, 4242)
    assert (ping["email"], ping["phone"]) == (EMAIL, PHONE_DIGITS)
    assert ping["reason"] is None and ping["available"] is None


# ──────────────────────────────── the origin ────────────────────────────────

def test_origin_falls_back_to_the_state_capital(lot, logged):
    """No ZIP on the row → the state's center, not a refusal."""
    lot["zip_code"] = None
    body = _estimate(_client()).json()
    assert body["ok"] is True
    # ID's center IS Boise 83702, so the lane (and the range) is unchanged.
    assert logged["inserts"][0]["origin_zip"] == "83702"


def test_origin_is_never_client_supplied(lot, logged):
    """An `origin_zip` in the payload is noise — the lane comes off the row."""
    lot["zip_code"] = "30303"          # Atlanta
    _estimate(_client(), origin_zip="99501", origin="99501")
    assert logged["inserts"][0]["origin_zip"] == "30303"


def test_unlocatable_origin_is_a_hand_quote(lot, logged):
    """No ZIP and no known state — we will not measure a lane from nowhere."""
    lot["zip_code"] = None
    lot["state"] = ""
    r = _estimate(_client())
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "unquotable"
    # …but the request is kept: no price, no origin, a reason, and the contact.
    (call,) = logged["inserts"]
    assert call["quote"] is None and call["origin_zip"] is None
    assert call["unquotable_reason"]
    assert call["buyer_email"] == EMAIL


# ────────────────────────────── unquotable lanes ────────────────────────────

@pytest.mark.parametrize(
    "dest",
    [
        "K1A 0B1",   # Ottawa — international
        "96801",     # Honolulu — offshore
        "99501",     # Anchorage — barge/air, ground math is wrong
        "not-a-zip",
        "",
    ],
)
def test_unquotable_destination_is_a_200_not_a_number(lot, logged, notified, carrier, dest):
    r = _estimate(_client(), dest_zip=dest)
    assert r.status_code == 200
    assert r.json() == {
        "ok": False,
        "reason": "unquotable",
        "saved": True,
        "message": (
            "We'll quote this lane by hand and come back to you with a real "
            "number at the email and phone you gave us."
        ),
    }
    # No price anywhere — but the request is stored and the operator is paged:
    # somebody asked, and left a way to answer them.
    (call,) = logged["inserts"]
    assert call["quote"] is None
    assert call["unquotable_reason"]
    assert call["dest_zip"] == dest
    assert (call["buyer_email"], call["buyer_phone"]) == (EMAIL, PHONE_DIGITS)
    (ping,) = notified["estimates"]
    assert ping["quote"] is None and ping["reason"]
    assert carrier == []                 # no lane to price ⇒ no carrier check


def test_unquotable_lane_that_could_not_be_stored_sends_the_buyer_to_the_form(monkeypatch, lot, notified):
    """Before migration 021 an unquotable row cannot be written (insert → None),
    and a Telegram ping is best-effort — it is not a record. So the buyer must
    NOT read "got it": they are handed to the contact form, which writes
    `inquiries`. The operator is still paged with the contact details."""
    monkeypatch.setattr(freight_log, "insert_storefront_quote", lambda **kw: None)
    body = _estimate(_client(), dest_zip="96801").json()
    assert body["ok"] is False and body["reason"] == "unquotable"
    assert body["saved"] is False
    assert "form below" in body["message"] and "gave us" not in body["message"]
    (ping,) = notified["estimates"]
    assert ping["quote_id"] is None and ping["email"] == EMAIL


@pytest.mark.parametrize("typo", ["3003", "303", "0160", "1608", "303033", "1", "30 033"])
def test_a_short_or_long_zip_is_never_padded_into_another_zip(lot, logged, notified, carrier, typo):
    """`_resolve_zip` zero-pads (right for an origin stored as a number). For a
    buyer's destination that turned "3003" — a typo for Atlanta's 30033 — into
    03003, New Hampshire, and returned a confident price for a 1,125-mile lane.
    Not exactly five digits ⇒ no price, and the request is kept."""
    body = _estimate(_client(), dest_zip=typo).json()
    assert body["ok"] is False and body["reason"] == "unquotable"
    assert "estimate" not in body
    (call,) = logged["inserts"]
    assert call["quote"] is None
    assert "not a 5-digit US ZIP" in call["unquotable_reason"]
    assert carrier == []


def test_five_digit_zips_with_a_leading_zero_still_quote(lot, logged):
    assert _estimate(_client(), dest_zip="01608").json()["ok"] is True


# ─────────────────────────────── lot resolution ─────────────────────────────

@pytest.mark.parametrize(
    "patch",
    [
        {"status": "hidden"},
        {"status": "sold_out"},
        {"status": "lost_sold_out"},
    ],
)
def test_hidden_and_sold_lots_are_404(lot, logged, patch):
    lot.update(patch)
    assert _estimate(_client()).status_code == 404


@pytest.mark.parametrize("lot_id", ["nope", "", None])
def test_unknown_lot_is_404(lot, logged, lot_id):
    assert _estimate(_client(), lot_id=lot_id).status_code == 404


# ──────────────────────────────── quantity ──────────────────────────────────

@pytest.mark.parametrize("quantity", ["many", "12.5.3", {"n": 1}, [5]])
def test_garbage_quantity_is_a_400(lot, logged, quantity):
    r = _estimate(_client(), quantity=quantity)
    assert r.status_code == 400
    assert logged["inserts"] == []


def test_quantity_defaults_to_the_lot(lot, logged):
    """No quantity → quantity_remaining; none of that → quantity_original."""
    _estimate(_client(), quantity=None)
    assert logged["inserts"][-1]["quantity"] == 400

    lot["quantity_remaining"] = 0
    _estimate(_client(), quantity=None)
    assert logged["inserts"][-1]["quantity"] == 945

    lot["quantity_original"] = None
    _estimate(_client(), quantity=None)
    assert logged["inserts"][-1]["quantity"] == 1


@pytest.mark.parametrize(
    "sent,expected", [(0, 1), (-5, 1), (999_999, 10_000), (1, 1)]
)
def test_quantity_is_clamped_not_rejected(lot, logged, sent, expected):
    """A fat-fingered number is a typo, not an attack — clamp and quote."""
    r = _estimate(_client(), quantity=sent)
    assert r.status_code == 200
    assert logged["inserts"][-1]["quantity"] == expected


# ───────────────────────────── database outage ──────────────────────────────

def test_a_database_outage_is_a_paged_lead_not_a_500(monkeypatch, logged, notified, carrier):
    """`inventory.get` is the first DB touch. When it raises, the buyer's
    details exist only in the request — page the operator with them."""
    def down(lot_id):
        raise RuntimeError("pooler timeout")

    monkeypatch.setattr(inventory, "get", down)
    r = _estimate(_client(), quantity=160)
    assert r.status_code == 200
    assert r.json()["reason"] == "not_saved" and "estimate" not in r.json()
    (ping,) = notified["estimates"]
    assert ping["quote"] is None and ping["quote_id"] is None
    assert (ping["email"], ping["phone"]) == (EMAIL, PHONE_DIGITS)
    assert ping["reason"] == "database unavailable"
    assert ping["row"] == {"lot_id": "31225"} and ping["quantity"] == 160
    assert logged["inserts"] == [] and carrier == []


def test_contact_is_checked_before_the_database_is_touched(monkeypatch, notified):
    touched = []
    monkeypatch.setattr(inventory, "get", lambda lot_id: touched.append(lot_id))
    assert _estimate(_client(), email="nope").status_code == 400
    assert touched == [] and notified["estimates"] == []


# ─────────────────────────────── contact details ────────────────────────────

@pytest.mark.parametrize(
    "patch, detail",
    [
        ({"email": None}, "valid email required"),
        ({"email": ""}, "valid email required"),
        ({"email": "nope"}, "valid email required"),
        ({"email": "two words@example.com"}, "valid email required"),
        ({"email": "a@b.co?bcc=x@evil.io"}, "valid email required"),   # mailto: parameter injection
        ({"email": "a@b.co#x"}, "valid email required"),
        ({"email": 'a"b@example.com'}, "valid email required"),
        ({"phone": None}, "valid phone required"),
        ({"phone": ""}, "valid phone required"),
        ({"phone": "555-0100"}, "valid phone required"),          # 7 digits
        ({"phone": "404 555 010"}, "valid phone required"),        # 9 digits
        ({"phone": "(404) 555-0100 x12"}, "valid phone required"),
        ({"phone": "call me"}, "valid phone required"),
    ],
)
def test_contact_required(lot, logged, notified, patch, detail):
    """No way to answer ⇒ not a request. This is also what a lot page cached
    from before the change gets (it posts no email/phone): a 400, never a price."""
    r = _estimate(_client(), **patch)
    assert r.status_code == 400
    assert r.json() == {"detail": detail}
    assert logged["inserts"] == []
    assert notified["estimates"] == []


@pytest.mark.parametrize(
    "typed",
    ["(404) 555-0100", "404-555-0100", "404.555.0100", "4045550100",
     "+1 404 555 0100", "1-404-555-0100", " 404 555 0100 "],
)
def test_phone_normalisation(typed):
    assert app_module._clean_phone(typed) == PHONE_DIGITS


@pytest.mark.parametrize(
    "typed",
    ["", None, "404555010", "40455501001", "2-404-555-0100", "044 555 0100",
     "404 155 0100", "++", "phone"],
)
def test_phone_rejects_what_is_not_a_us_number(typed):
    with pytest.raises(ValueError):
        app_module._clean_phone(typed)


def test_an_apostrophe_in_an_email_is_fine(lot, logged):
    assert _estimate(_client(), email="o'brien@example.com").json()["ok"] is True
    assert logged["inserts"][-1]["buyer_email"] == "o'brien@example.com"


def test_contact_details_never_come_back_in_the_response(lot, logged):
    body = _estimate(_client()).json()
    flat = repr(body)
    assert EMAIL not in flat and PHONE_DIGITS not in flat


@pytest.mark.parametrize("typed", ["01608-1234", "01608 1234", "016081234", " 01608 "])
def test_zip_plus_four_is_trimmed(lot, logged, typed):
    assert _estimate(_client(), dest_zip=typed).json()["ok"] is True
    assert logged["inserts"][-1]["dest_zip"] == DEST_WORCESTER


# ──────────────────────────────── stock + chair data ────────────────────────

def test_request_over_stock_is_quoted_and_flagged(lot, logged, notified):
    """160 chairs asked on a lot with 100 left (the 2026-09 Atlanta request):
    quote it, but say so to the buyer, the row and the operator."""
    lot["quantity_remaining"] = 100
    body = _estimate(_client(), quantity=160).json()
    assert body["ok"] is True and body["available"] == 100
    assert logged["inserts"][-1]["lot_quantity_remaining"] == 100
    assert notified["estimates"][-1]["available"] == 100


def test_request_within_stock_is_not_flagged(lot, logged, notified):
    body = _estimate(_client(), quantity=400).json()
    assert body["available"] is None
    assert notified["estimates"][-1]["available"] is None


def test_lot_chair_weight_reaches_the_estimator(lot, logged):
    base = _estimate(_client()).json()["estimate"]["ltl"]["low"]
    lot["chair_weight_lb"] = 20
    heavy = _estimate(_client()).json()["estimate"]["ltl"]["low"]
    assert heavy > base
    assert logged["inserts"][-1]["quote"]["raw"]["lbs_per_chair"] == 20


# ─────────────────────────────── carrier check ──────────────────────────────

def test_carrier_check_starts_for_a_saved_priced_quote(lot, logged, carrier):
    _estimate(_client())
    assert carrier == [4242]


def _stored(**over):
    row = {"id": 9, "lot_id": "31225", "origin_zip": "30318", "dest_zip": "30033",
           "quantity": 160, "unquotable_reason": None}
    row.update(over)
    return row


@pytest.fixture
def check(monkeypatch, lot):
    """Wire `_run_carrier_check` to in-memory doubles; returns what happened."""
    seen = {"quote": _stored(), "calls": [], "saved": [], "options": [
        {"carrier_name": "Averitt Express", "price_usd": 589.51, "transit_days": 1},
        {"carrier_name": "Saia LTL Freight", "price_usd": 625.51, "transit_days": 1},
    ], "raise": None}

    def fake_market(origin, dest, **kw):
        seen["calls"].append((origin, dest, kw))
        if seen["raise"]:
            raise seen["raise"]
        return seen["options"]

    seen["ready"] = True
    monkeypatch.setattr(freight_log, "schema_ready", lambda: seen["ready"])
    monkeypatch.setattr(freight_log, "get_quote", lambda qid: seen["quote"])
    monkeypatch.setattr(freight_log, "set_carrier_result",
                        lambda qid, summary: seen["saved"].append((qid, summary)) or True)
    monkeypatch.setattr(warp_rates, "market_options", fake_market)
    return seen


def test_carrier_check_sizes_the_shipment_from_the_standard_chair(check):
    summary = app_module._run_carrier_check(9)
    ((origin, dest, kw),) = check["calls"]
    assert (origin, dest) == ("30318", "30033")
    # 160 chairs at 35 per pallet = 5 pallets; 160 × 13 lb / 5 + 40 lb pallet.
    assert kw == {"pallets": 5, "weight_lbs_per_pallet": 456, "height_in": 80}
    assert summary["carrier_status"] == "ok"
    assert summary["carrier_low"] == 589.51 and summary["carrier_name"] == "Averitt Express"
    assert check["saved"] == [(9, summary)]


def test_carrier_check_uses_the_lots_own_chair_data(check, lot):
    lot.update(chair_weight_lb=17, chairs_per_pallet=40, pallet_height_in=72)
    app_module._run_carrier_check(9)
    kw = check["calls"][0][2]
    assert kw == {"pallets": 4, "weight_lbs_per_pallet": 720, "height_in": 72}


def test_carrier_check_skips_the_network_for_a_load_too_big_for_ltl(check):
    check["quote"] = _stored(quantity=500)        # 15 pallets
    summary = app_module._run_carrier_check(9)
    assert check["calls"] == []
    assert summary["carrier_status"] == "too_big" and summary["carrier_low"] is None
    assert check["saved"][0][1]["carrier_status"] == "too_big"


def test_carrier_check_records_an_error_and_no_price(check):
    check["raise"] = warp_rates.WarpUnavailable("warp HTTP 503")
    summary = app_module._run_carrier_check(9)
    assert summary["carrier_status"] == "error" and summary["carrier_low"] is None
    assert check["saved"][0][1]["carrier_status"] == "error"


def test_carrier_check_with_no_carriers_is_status_none(check):
    check["options"] = []
    assert app_module._run_carrier_check(9)["carrier_status"] == "none"


@pytest.mark.parametrize("quote", [
    None,
    _stored(unquotable_reason="international"),
    _stored(origin_zip=None),
    _stored(quantity=0),
])
def test_carrier_check_has_nothing_to_do_without_a_priced_lane(check, quote):
    check["quote"] = quote
    assert app_module._run_carrier_check(9) is None
    assert check["calls"] == [] and check["saved"] == []


def test_carrier_check_makes_no_call_before_the_migration(check):
    """Pre-021 there is nowhere to store the answer: no 20-45 s Warp call, and
    none of the hour's budget, for a result that would be dropped."""
    check["ready"] = False
    assert app_module._run_carrier_check(9) is None
    assert check["calls"] == [] and check["saved"] == []


def test_carrier_check_skips_a_row_whose_destination_is_not_a_zip(check):
    check["quote"] = _stored(dest_zip="3003")       # an old row from before the ZIP rule
    assert app_module._run_carrier_check(9) is None
    assert check["calls"] == []


def test_the_same_lane_is_asked_once(check):
    """Sixty quotes for one lane must not be sixty Warp calls."""
    first = app_module._run_carrier_check(9)
    second = app_module._run_carrier_check(10)
    assert len(check["calls"]) == 1
    assert second == first
    assert [qid for qid, _ in check["saved"]] == [9, 10]      # both rows still get the prices


def test_a_failed_lane_is_not_memoised(check):
    check["raise"] = warp_rates.WarpUnavailable("warp HTTP 503")
    app_module._run_carrier_check(9)
    check["raise"] = None
    assert app_module._run_carrier_check(9)["carrier_status"] == "ok"
    assert len(check["calls"]) == 2


def test_hourly_budget_stops_outbound_calls(check, monkeypatch):
    monkeypatch.setattr(app_module, "CARRIER_CALLS_PER_HOUR", 1)
    app_module._run_carrier_check(9)
    check["quote"] = _stored(dest_zip="30030")                 # a different lane
    with pytest.raises(app_module.CarrierBudgetExceeded):
        app_module._run_carrier_check(9)
    assert len(check["calls"]) == 1


def test_carrier_task_swallows_an_exhausted_budget(monkeypatch):
    def spent(qid):
        raise app_module.CarrierBudgetExceeded("40 carrier checks used this hour")

    monkeypatch.setattr(app_module, "_run_carrier_check", spent)
    asyncio.run(REAL_CARRIER_TASK(7))                          # logged, not raised


def test_carrier_task_runs_on_its_own_threads_not_the_default_pool(monkeypatch):
    """A Warp call blocks ~20-45 s. On the default executor it would starve
    every `asyncio.to_thread` DB hop in the app."""
    names = []
    monkeypatch.setattr(app_module, "_run_carrier_check",
                        lambda qid: names.append(threading.current_thread().name))
    asyncio.run(REAL_CARRIER_TASK(7))
    assert names and names[0].startswith("carrier-check")


def test_carrier_task_gives_up_when_the_queue_is_full(monkeypatch):
    ran = []
    monkeypatch.setattr(app_module, "_run_carrier_check", lambda qid: ran.append(qid))
    monkeypatch.setattr(app_module, "CARRIER_QUEUE_MAX", 0)
    asyncio.run(REAL_CARRIER_TASK(7))
    assert ran == []
    monkeypatch.setattr(app_module, "CARRIER_QUEUE_MAX", 8)
    asyncio.run(REAL_CARRIER_TASK(7))
    assert ran == [7] and app_module._carrier_pending == 0     # the slot is released


def test_carrier_check_task_respects_the_kill_switch(monkeypatch):
    """`_carrier_check_task` (the real one — the autouse spy is bypassed here)."""
    ran = []
    monkeypatch.setattr(app_module, "_run_carrier_check", lambda qid: ran.append(qid))
    monkeypatch.setenv("WARP_RATES_ENABLED", "0")
    asyncio.run(REAL_CARRIER_TASK(7))
    assert ran == []
    monkeypatch.delenv("WARP_RATES_ENABLED")
    asyncio.run(REAL_CARRIER_TASK(7))
    assert ran == [7]


def test_carrier_check_task_never_raises(monkeypatch):
    def boom(qid):
        raise RuntimeError("db down")

    monkeypatch.setattr(app_module, "_run_carrier_check", boom)
    asyncio.run(REAL_CARRIER_TASK(7))     # logged, not raised


# ──────────────────────────────────── alert ─────────────────────────────────

@pytest.fixture
def telegram(monkeypatch):
    sent = {"texts": [], "result": (True, None)}

    async def fake_send(text, *, topic=None):
        sent["texts"].append((text, topic))
        return sent["result"]

    monkeypatch.setattr(telegram_alerts, "send_message", fake_send)
    return sent


QUOTE_FOR_ALERT = {
    "recommended_mode": "ltl", "mode": "ltl", "ltl_low": 570, "ltl_high": 810,
    "miles": 15, "transit_days": 2, "provider": "estimator",
}


def test_alert_carries_everything_needed_to_answer(telegram):
    asyncio.run(REAL_NOTIFY(
        make_lot(), QUOTE_FOR_ALERT, dest_zip="30033", quantity=160, quote_id=9,
        email=EMAIL, phone=PHONE_DIGITS, available=100,
    ))
    ((text, topic),) = telegram["texts"]
    assert topic == "leads"
    assert "#9" in text and "160 chairs → 30033" in text and "$570–$810" in text
    assert EMAIL in text and "(404) 555-0100" in text
    assert "asked for 160, lot has 100" in text
    assert "NOT SAVED" not in text
    assert "/admin?tab=quotes" in text


def test_alert_for_an_unsaved_unquotable_request_says_so(telegram):
    asyncio.run(REAL_NOTIFY(
        make_lot(), None, dest_zip="K1A 0B1", quantity=50, quote_id=None,
        email=EMAIL, phone=PHONE_DIGITS, reason="international/offshore destination",
    ))
    text = telegram["texts"][0][0]
    assert "NO PRICE — quote by hand" in text
    assert "international/offshore destination" in text
    assert "NOT SAVED to the database" in text
    assert EMAIL in text


def test_an_unsaved_lead_whose_alert_also_failed_is_written_to_the_log(telegram, caplog):
    """No row and no ping: the log line is the only place the lead exists, so
    it carries the contact details."""
    telegram["result"] = (False, "http_429: Too Many Requests")
    with caplog.at_level(logging.WARNING):
        asyncio.run(REAL_NOTIFY(
            make_lot(), None, dest_zip="K1A 0B1", quantity=50, quote_id=None,
            email=EMAIL, phone=PHONE_DIGITS, reason="international",
        ))
    (rec,) = [r for r in caplog.records if "NOT SAVED AND NOT ALERTED" in r.getMessage()]
    assert rec.levelno == logging.ERROR
    assert EMAIL in rec.getMessage() and PHONE_DIGITS in rec.getMessage()


def test_a_saved_lead_keeps_its_contact_out_of_the_log(telegram, caplog):
    telegram["result"] = (False, "http_429")
    with caplog.at_level(logging.WARNING):
        asyncio.run(REAL_NOTIFY(
            make_lot(), QUOTE_FOR_ALERT, dest_zip="30033", quantity=160, quote_id=9,
            email=EMAIL, phone=PHONE_DIGITS,
        ))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "alert not sent" in text
    assert EMAIL not in text and PHONE_DIGITS not in text


def test_a_failed_alert_is_logged_not_swallowed(telegram, caplog):
    telegram["result"] = (False, "http_429: Too Many Requests")
    with caplog.at_level(logging.WARNING):
        asyncio.run(REAL_NOTIFY(
            make_lot(), QUOTE_FOR_ALERT, dest_zip="30033", quantity=160, quote_id=9,
            email=EMAIL, phone=PHONE_DIGITS,
        ))
    assert any("alert not sent" in r.getMessage() and "http_429" in r.getMessage()
               for r in caplog.records)


# ─────────────────────────────── rate limiting ──────────────────────────────

def test_per_ip_limit_returns_429(monkeypatch, lot, logged):
    monkeypatch.setattr(rate_limit, "FREIGHT_PER_IP_LIMIT", 3)
    monkeypatch.setattr(rate_limit, "FREIGHT_GLOBAL_LIMIT", 1000)
    client = _client()
    headers = {"cf-connecting-ip": "198.51.100.9"}
    for _ in range(3):
        assert client.post(
            "/freight-estimate",
            json=BODY,
            headers=headers,
        ).status_code == 200
    r = client.post(
        "/freight-estimate",
        json=BODY,
        headers=headers,
    )
    assert r.status_code == 429
    assert r.json() == {"detail": "rate_limited"}

    # A different IP is unaffected — the bucket is per-caller.
    assert client.post(
        "/freight-estimate",
        json=BODY,
        headers={"cf-connecting-ip": "198.51.100.10"},
    ).status_code == 200


def test_global_limit_catches_a_rotating_caller(monkeypatch, lot, logged):
    """Per-IP caps do nothing against a proxy pool; the global one does."""
    monkeypatch.setattr(rate_limit, "FREIGHT_PER_IP_LIMIT", 1000)
    monkeypatch.setattr(rate_limit, "FREIGHT_GLOBAL_LIMIT", 2)
    client = _client()

    def hit(ip):
        return client.post(
            "/freight-estimate",
            json=BODY,
            headers={"cf-connecting-ip": ip},
        ).status_code

    assert hit("203.0.113.1") == 200
    assert hit("203.0.113.2") == 200
    assert hit("203.0.113.3") == 429


def test_client_ip_prefers_cloudflare_then_forwarded_for():
    class Req:
        def __init__(self, headers, host="10.0.0.1"):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    assert rate_limit.client_ip(
        Req({"cf-connecting-ip": "1.1.1.1", "x-forwarded-for": "2.2.2.2, 3.3.3.3"})
    ) == "1.1.1.1"
    # XFF is a chain "client, proxy1, proxy2" — the client is the first entry.
    assert rate_limit.client_ip(
        Req({"x-forwarded-for": "2.2.2.2, 3.3.3.3"})
    ) == "2.2.2.2"
    assert rate_limit.client_ip(Req({})) == "10.0.0.1"


def test_rate_limit_window_rolls_over(monkeypatch):
    """A limited caller isn't limited forever — the next window is a clean slate."""
    now = [1_000_000.0]
    monkeypatch.setattr(rate_limit.time, "time", lambda: now[0])
    assert rate_limit.allow("k", limit=1, window_s=60) is True
    assert rate_limit.allow("k", limit=1, window_s=60) is False
    now[0] += 61
    assert rate_limit.allow("k", limit=1, window_s=60) is True


# ─────────────────────────────── email capture ──────────────────────────────

def test_email_attaches_to_a_quote(lot, logged, notified):
    r = _client().post(
        "/freight-estimate/email",
        json={"quote_id": 4242, "email": "buyer@example.com"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert logged["emails"] == [(4242, "buyer@example.com")]
    assert notified["emails"] == [(4242, "buyer@example.com")]


def test_legacy_email_step_admits_when_nothing_was_saved(monkeypatch, lot, notified):
    """It used to answer `ok` whatever happened to the UPDATE."""
    monkeypatch.setattr(freight_log, "set_quote_email", lambda quote_id, email: False)
    r = _client().post(
        "/freight-estimate/email", json={"quote_id": 4242, "email": EMAIL}
    )
    assert r.status_code == 503
    assert notified["emails"] == [(4242, EMAIL)]      # the ping still carries it


@pytest.mark.parametrize(
    "body",
    [
        {"quote_id": 4242, "email": "nope"},
        {"quote_id": 4242, "email": "no@domain"},
        {"quote_id": 4242, "email": "two words@example.com"},
        {"quote_id": 4242, "email": "@example.com"},
        {"quote_id": 4242, "email": ""},
        {"quote_id": 4242},
        {"email": "buyer@example.com"},          # no quote_id
        {"quote_id": "abc", "email": "buyer@example.com"},
    ],
)
def test_junk_email_or_missing_quote_id_is_a_400(logged, notified, body):
    assert _client().post("/freight-estimate/email", json=body).status_code == 400
    assert logged["emails"] == []
    assert notified["emails"] == []


def test_email_endpoint_shares_the_freight_rate_bucket(monkeypatch, lot, logged):
    monkeypatch.setattr(rate_limit, "FREIGHT_PER_IP_LIMIT", 1)
    client = _client()
    assert _estimate(client).status_code == 200
    r = client.post(
        "/freight-estimate/email",
        json={"quote_id": 4242, "email": "buyer@example.com"},
    )
    assert r.status_code == 429


# ──────────────────────────── public-path proof ─────────────────────────────

def test_endpoints_stay_public_when_admin_auth_is_on(monkeypatch, lot, logged):
    """Auth guards `/admin`, `/api/`, `/screenshot/` — a buyer must still quote.

    Mirrors tests/web/test_auth.py: turn ADMIN_PASSWORD on, prove the admin
    surface locks and these two paths don't.
    """
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    client = _client()
    assert client.get("/api/site-config").status_code == 401

    assert not auth_svc.path_requires_auth("/freight-estimate")
    assert not auth_svc.path_requires_auth("/freight-estimate/email")
    assert _estimate(client).status_code == 200
    assert client.post(
        "/freight-estimate/email",
        json={"quote_id": 4242, "email": "buyer@example.com"},
    ).status_code == 200


# ───────────────────────── lot page template context ────────────────────────

def test_listing_detail_context_gates_the_widget():
    """Wave 4's widget renders off `freight.enabled` / `freight.default_qty`."""
    row = make_lot()
    assert app_module._freight_origin_zip(row) == "83702"
    assert app_module._freight_default_qty(row) == 400

    no_zip = make_lot(zip_code=None, state="GA")
    assert app_module._freight_origin_zip(no_zip) == \
        freight_estimate.STATE_CENTER_ZIP["GA"]

    nowhere = make_lot(zip_code=None, state=None)
    assert app_module._freight_origin_zip(nowhere) is None

    assert app_module._freight_default_qty(
        make_lot(quantity_remaining=0, quantity_original=945)
    ) == 945
    assert app_module._freight_default_qty(
        make_lot(quantity_remaining=None, quantity_original=None)
    ) == 1
