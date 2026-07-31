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
"""
import sys

import pytest
from fastapi.testclient import TestClient

from automation import freight_estimate, freight_log, inventory
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


# ─────────────────────────────── fixtures ───────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Fresh rate-limit counters, no carrier keys, no Telegram, no auth."""
    rate_limit.reset()
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

    def spy_estimate(row, quote, *, dest_zip, quantity, quote_id):
        seen["estimates"].append((row, quote, dest_zip, quantity, quote_id))
        return _noop()

    def spy_email(quote_id, email):
        seen["emails"].append((quote_id, email))
        return _noop()

    monkeypatch.setattr(app_module, "_notify_freight_estimate", spy_estimate)
    monkeypatch.setattr(app_module, "_notify_freight_email", spy_email)
    return seen


def _client():
    # https so any Secure cookie round-trips; no `with` so lifespan never fires.
    return TestClient(app, base_url="https://testserver")


def _estimate(client, **body):
    payload = {"lot_id": "31225", "dest_zip": DEST_WORCESTER, "quantity": 150}
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
    assert set(body) == {"ok", "quote_id", "estimate", "framing"}
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
        json={"lot_id": "31225", "dest_zip": DEST_WORCESTER, "quantity": 150},
        headers={"cf-connecting-ip": "203.0.113.7"},
    )
    (call,) = logged["inserts"]
    assert call["lot_id"] == "31225"
    assert call["origin_zip"] == "83702"
    assert call["dest_zip"] == DEST_WORCESTER
    assert call["quantity"] == 150
    assert call["client_ip"] == "203.0.113.7"


def test_a_logging_outage_still_returns_an_estimate(monkeypatch, lot):
    """`freight_log` swallows DB errors and returns None; the quote survives."""
    monkeypatch.setattr(
        freight_log, "insert_storefront_quote", lambda **kw: None
    )
    body = _estimate(_client()).json()
    assert body["ok"] is True
    assert body["quote_id"] is None
    assert body["estimate"]["ltl"]["low"] > 0


def test_telegram_pings_exactly_once_per_estimate(lot, logged, notified):
    client = _client()
    _estimate(client)
    assert len(notified["estimates"]) == 1
    _estimate(client)
    assert len(notified["estimates"]) == 2
    (_row, _quote, dest, qty, quote_id) = notified["estimates"][0]
    assert (dest, qty, quote_id) == (DEST_WORCESTER, 150, 4242)


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
    assert logged["inserts"] == []


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
def test_unquotable_destination_is_a_200_not_a_number(lot, logged, notified, dest):
    r = _estimate(_client(), dest_zip=dest)
    assert r.status_code == 200
    assert r.json() == {
        "ok": False,
        "reason": "unquotable",
        "message": (
            "We'll quote this lane by hand — send the request below and we'll "
            "come back with a real number."
        ),
    }
    # Nothing logged, nobody paged: there is no quote.
    assert logged["inserts"] == []
    assert notified["estimates"] == []


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


# ─────────────────────────────── rate limiting ──────────────────────────────

def test_per_ip_limit_returns_429(monkeypatch, lot, logged):
    monkeypatch.setattr(rate_limit, "FREIGHT_PER_IP_LIMIT", 3)
    monkeypatch.setattr(rate_limit, "FREIGHT_GLOBAL_LIMIT", 1000)
    client = _client()
    headers = {"cf-connecting-ip": "198.51.100.9"}
    for _ in range(3):
        assert client.post(
            "/freight-estimate",
            json={"lot_id": "31225", "dest_zip": DEST_WORCESTER},
            headers=headers,
        ).status_code == 200
    r = client.post(
        "/freight-estimate",
        json={"lot_id": "31225", "dest_zip": DEST_WORCESTER},
        headers=headers,
    )
    assert r.status_code == 429
    assert r.json() == {"detail": "rate_limited"}

    # A different IP is unaffected — the bucket is per-caller.
    assert client.post(
        "/freight-estimate",
        json={"lot_id": "31225", "dest_zip": DEST_WORCESTER},
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
            json={"lot_id": "31225", "dest_zip": DEST_WORCESTER},
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
