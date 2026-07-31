"""Offline tests for the admin Deposits surface (B4): /api/deposits, /api/settings,
and the per-lot `deposit_pct_override` on /api/inventory.

Same shape as ``tests/web/test_auth.py`` and ``test_reserve.py``: a ``TestClient``
built WITHOUT a ``with`` block so the lifespan (and its DB-touching alert
scheduler) never runs, and every DB call site monkeypatched — nothing here opens
a connection.

The properties worth defending:

  1. **The money ledger is behind the door.** These routes list buyer emails,
     phone numbers and payment-intent ids. They live under ``/api/`` precisely
     so the session middleware gates them; the test asserts that rather than
     trusting the prefix by eye.
  2. **Junk status never reaches SQL.** Both the list filter and the PATCH
     validate against ``DEPOSIT_STATUSES`` and answer 400, so a typo can't
     become an empty result set that reads like "no deposits".
  3. **A percent typed as a percent is a 400, not a silent no-op.** Entering
     ``15`` instead of ``0.15`` for a per-lot override would otherwise fall
     outside ``deposit_rules()``'s accepted range and be quietly ignored —
     the operator would think they'd set a 15% rule.
"""
import pytest
from fastapi.testclient import TestClient

from automation import deposits, inventory, site_settings
from automation.web import auth as auth_svc
from automation.web.app import app

PASSWORD = "hunter2-but-much-longer"
_AUTH_ENV = ("ADMIN_PASSWORD", "SESSION_SECRET", "TOTP_SECRET")


# ─────────────────────────────── fixtures ───────────────────────────────────

@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    """Auth off by default with a fresh signing-key cache; tests opt in."""
    for var in _AUTH_ENV:
        monkeypatch.delenv(var, raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _enable_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()


def _client():
    # https so Secure cookies round-trip; no `with` so lifespan never fires.
    return TestClient(app, base_url="https://testserver")


def _login(client):
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


def make_row(**over) -> dict:
    row = {
        "id": 7,
        "lot_id": "31225",
        "kind": "deposit",
        "quantity": 30,
        "price_per_chair": 100,
        "subtotal_cents": 300000,
        "amount_cents": 45000,
        "currency": "usd",
        "status": "pending",
        "stripe_session_id": "cs_test_1",
        "stripe_payment_intent": None,
        "payment_method": None,
        "failure_reason": None,
        "admin_note": None,
        "buyer_name": "Dana Buyer",
        "buyer_email": "dana@example.com",
        "buyer_phone": None,
        "created_at": "2026-07-30T12:00:00+00:00",
        "paid_at": None,
        "refunded_at": None,
    }
    row.update(over)
    return row


@pytest.fixture
def store(monkeypatch):
    """In-memory deposits store. `calls` records what the routes asked for."""
    calls = {"list": [], "admin": [], "deleted": []}

    def fake_list(status=None):
        calls["list"].append(status)
        return [make_row(status=status or "pending")]

    def fake_admin(deposit_id, *, status=None, admin_note=None):
        calls["admin"].append((deposit_id, status, admin_note))
        if deposit_id != 7:
            return None
        return make_row(status=status or "pending", admin_note=admin_note)

    def fake_delete(deposit_id):
        calls["deleted"].append(deposit_id)
        return deposit_id == 7

    monkeypatch.setattr(deposits, "list_deposits", fake_list)
    monkeypatch.setattr(deposits, "set_admin_fields", fake_admin)
    monkeypatch.setattr(deposits, "delete_deposit", fake_delete)
    return calls


@pytest.fixture
def settings(monkeypatch):
    """`site_settings` backed by a dict, with the real validation semantics."""
    state = {"deposit_pct": 0.15, "deposit_min_usd": 200}

    def fake_get_all():
        return dict(state)

    def fake_set_many(values):
        for key, value in (values or {}).items():
            if key not in state:
                raise ValueError(f"unknown setting: {key}")
            if key == "deposit_pct" and not 0.01 <= float(value) <= 1.0:
                raise ValueError("deposit_pct must be between 0.01 and 1.0")
            state[key] = value
        return dict(state)

    monkeypatch.setattr(site_settings, "get_all", fake_get_all)
    monkeypatch.setattr(site_settings, "set_many", fake_set_many)
    return state


# ─────────────────────────────── auth gate ──────────────────────────────────

def test_deposit_routes_require_auth(monkeypatch, store, settings):
    """ADMIN_PASSWORD set → the middleware rejects before any handler runs."""
    _enable_auth(monkeypatch)
    client = _client()
    for method, path in (
        ("get", "/api/deposits"),
        ("patch", "/api/deposits/7"),
        ("delete", "/api/deposits/7"),
        ("get", "/api/settings"),
        ("patch", "/api/settings"),
    ):
        r = getattr(client, method)(path, **({"json": {}} if method == "patch" else {}))
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"
        assert r.json() == {"detail": "not_authenticated"}
    # No handler ran, so nothing touched the store.
    assert store == {"list": [], "admin": [], "deleted": []}


def test_deposits_readable_once_logged_in(monkeypatch, store, settings):
    _enable_auth(monkeypatch)
    client = _login(_client())
    r = client.get("/api/deposits")
    assert r.status_code == 200
    assert r.json()["items"][0]["id"] == 7


# ────────────────────────────── GET /api/deposits ───────────────────────────

def test_list_returns_items_when_auth_disabled(store):
    r = _client().get("/api/deposits")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["buyer_email"] == "dana@example.com"
    assert items[0]["amount_cents"] == 45000
    # Timestamps come back as JSON strings the admin JS can slice.
    assert items[0]["created_at"].startswith("2026-07-30")
    assert store["list"] == [None]


@pytest.mark.parametrize("status", deposits.DEPOSIT_STATUSES)
def test_list_status_filter_passes_through(store, status):
    r = _client().get(f"/api/deposits?status={status}")
    assert r.status_code == 200
    assert store["list"] == [status]


def test_list_rejects_unknown_status(store):
    r = _client().get("/api/deposits?status=refuned")
    assert r.status_code == 400
    assert "refuned" in r.json()["detail"]
    assert store["list"] == []  # never reached the store


def test_list_empty_status_means_all(store):
    """`?status=` (the "all" segment) must not filter on the empty string."""
    assert _client().get("/api/deposits?status=").status_code == 200
    assert store["list"] == [None]


# ───────────────────────────── PATCH /api/deposits ──────────────────────────

def test_patch_status_and_note(store):
    r = _client().patch(
        "/api/deposits/7", json={"status": "canceled", "admin_note": "buyer ghosted"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "canceled"
    assert body["admin_note"] == "buyer ghosted"
    assert store["admin"] == [(7, "canceled", "buyer ghosted")]


def test_patch_note_only_leaves_status_alone(store):
    r = _client().patch("/api/deposits/7", json={"admin_note": "called him"})
    assert r.status_code == 200
    assert store["admin"] == [(7, None, "called him")]


def test_patch_rejects_unknown_status(store):
    r = _client().patch("/api/deposits/7", json={"status": "voided"})
    assert r.status_code == 400
    assert "voided" in r.json()["detail"]
    assert store["admin"] == []


def test_patch_missing_row_is_404(store):
    r = _client().patch("/api/deposits/999", json={"status": "paid"})
    assert r.status_code == 404


def test_patch_is_not_state_machine_gated(store):
    """The admin tab is the manual override for when Stripe and reality
    disagree — a move the webhook machine would refuse must still land."""
    r = _client().patch("/api/deposits/7", json={"status": "paid"})
    assert r.status_code == 200
    assert r.json()["status"] == "paid"


# ──────────────────────────── DELETE /api/deposits ──────────────────────────

def test_delete_ok(store):
    r = _client().delete("/api/deposits/7")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert store["deleted"] == [7]


def test_delete_missing_is_404(store):
    assert _client().delete("/api/deposits/999").status_code == 404


# ───────────────────────────────  /api/settings ─────────────────────────────

def test_get_settings(settings):
    r = _client().get("/api/settings")
    assert r.status_code == 200
    assert r.json() == {"deposit_pct": 0.15, "deposit_min_usd": 200}


def test_patch_settings_writes_and_returns_full_dict(settings):
    r = _client().patch(
        "/api/settings", json={"deposit_pct": 0.2, "deposit_min_usd": 300}
    )
    assert r.status_code == 200
    assert r.json() == {"deposit_pct": 0.2, "deposit_min_usd": 300}
    assert settings["deposit_pct"] == 0.2


def test_patch_settings_validation_error_is_400(settings):
    r = _client().patch("/api/settings", json={"deposit_pct": 5})
    assert r.status_code == 400
    assert "deposit_pct" in r.json()["detail"]
    assert settings["deposit_pct"] == 0.15  # unchanged


def test_patch_settings_unknown_key_is_400(settings):
    assert _client().patch("/api/settings", json={"free_beer": True}).status_code == 400


def test_settings_endpoint_is_not_site_config(settings):
    """/api/site-config is load-bearing for the auth tests — it must keep
    reporting deployment config, not the deposit rule."""
    body = _client().get("/api/site-config").json()
    assert "deposit_pct" not in body


# ───────────────── per-lot override on PATCH /api/inventory ─────────────────

@pytest.fixture
def lot_writes(monkeypatch):
    """Capture the UPDATE `inventory.set_fields` issues, without a DB."""
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
    monkeypatch.setattr(
        inventory, "get",
        lambda lot_id: {
            "lot_id": lot_id, "title": "Mity-Lite folding chairs",
            "status": "listed", "quantity_remaining": 100,
            "price_per_chair": 100, "deposit_pct_override": 0.2,
            "locations": None,
        },
    )
    return statements


def _override_params(statements) -> list:
    sql, params = statements[-1]
    assert "deposit_pct_override = %s" in sql, sql
    return params


def test_inventory_patch_accepts_deposit_pct_override(lot_writes):
    r = _client().patch("/api/inventory/31225", json={"deposit_pct_override": 0.2})
    assert r.status_code == 200
    assert r.json()["deposit_pct_override"] == 0.2
    assert _override_params(lot_writes)[0] == 0.2


def test_inventory_patch_clears_override_with_blank(lot_writes):
    for blank in (None, ""):
        lot_writes.clear()
        r = _client().patch(
            "/api/inventory/31225", json={"deposit_pct_override": blank}
        )
        assert r.status_code == 200
        assert _override_params(lot_writes)[0] is None


def test_inventory_patch_rejects_percent_shaped_override(lot_writes):
    """15 means 1500%, and the DB CHECK would reject it — say so as a 400."""
    r = _client().patch("/api/inventory/31225", json={"deposit_pct_override": 15})
    assert r.status_code == 400
    assert "0.15" in r.json()["detail"]
    assert lot_writes == []


def test_inventory_patch_rejects_junk_override(lot_writes):
    r = _client().patch("/api/inventory/31225", json={"deposit_pct_override": "soon"})
    assert r.status_code == 400
    assert lot_writes == []
