"""Deposits ledger + settings store — fully offline.

Nothing here opens a connection. `automation.deposits.quote` is pure by
construction; everything else reaches the DB only through `deposits.db.*` /
`site_settings.db.*`, which these tests replace with a tiny in-memory fake. If
a test in this file ever needs BLACKWHOLE_DB_URL, the module under test grew a
connection it shouldn't have.
"""
import pytest

from automation import deposits, site_settings

# The real reader, captured before the autouse stub below replaces it — the
# settings tests need it back.
_REAL_GET_ALL = site_settings.get_all


# ─────────────────────────────── fixtures ───────────────────────────────────

class FakeDB:
    """Stands in for `automation.db`. Holds one deposits row keyed by id."""

    def __init__(self, row: dict | None = None):
        self.row = row
        self.updates: list[tuple[str, tuple]] = []

    def fetch_one(self, sql, params=None):
        self.updates.append((sql, tuple(params or ())))
        if self.row is None:
            return None
        if sql.strip().upper().startswith("UPDATE"):
            # Apply just enough of the statement for the assertions we make.
            status, pi, pm, reason = params[0], params[1], params[2], params[3]
            self.row = dict(self.row)
            self.row["status"] = status
            self.row["stripe_payment_intent"] = pi or self.row.get("stripe_payment_intent")
            self.row["payment_method"] = pm or self.row.get("payment_method")
            self.row["failure_reason"] = reason or self.row.get("failure_reason")
            if status == "paid":
                self.row.setdefault("paid_at", "2026-07-31T00:00:00Z")
            if status == "refunded":
                self.row.setdefault("refunded_at", "2026-07-31T00:00:00Z")
        return self.row

    def fetch_all(self, sql, params=None):
        return [self.row] if self.row else []

    def execute(self, sql, params=None):
        self.updates.append((sql, tuple(params or ())))
        return 1


def make_row(**over) -> dict:
    row = {
        "id": 1,
        "lot_id": "31225",
        "kind": "deposit",
        "quantity": 30,
        "price_per_chair": 100,
        "subtotal_cents": 300000,
        "amount_cents": 45000,
        "status": "pending",
        "stripe_session_id": "cs_test_1",
        "stripe_payment_intent": None,
        "payment_method": None,
        "failure_reason": None,
    }
    row.update(over)
    return row


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDB(make_row())
    monkeypatch.setattr(deposits, "db", fake)
    return fake


@pytest.fixture(autouse=True)
def stub_settings(monkeypatch):
    """Default site settings without touching Postgres."""
    monkeypatch.setattr(
        site_settings, "get_all",
        lambda: {"deposit_pct": 0.15, "deposit_min_usd": 200},
    )


# ─────────────────────────────── quote matrix ───────────────────────────────

def test_quote_headline_case_30_chairs_at_100():
    # 30 × $100 = $3,000 order → 15% = $450.
    q = deposits.quote(quantity=30, price_per_chair=100, pct=0.15, min_cents=20000)
    assert q.subtotal_cents == 300000
    assert q.amount_cents == 45000
    assert q.balance_cents == 255000


def test_quote_applies_the_floor_when_the_percentage_is_too_small():
    # 40 × $20 = $800 → 15% = $120, below the $200 floor.
    q = deposits.quote(quantity=40, price_per_chair=20, pct=0.15, min_cents=20000)
    assert q.amount_cents == 20000


def test_quote_never_asks_for_more_than_the_order_is_worth():
    # $150 order with a $200 floor — the floor must be capped at the subtotal,
    # otherwise the buyer is asked to pre-pay more than the goods cost.
    q = deposits.quote(quantity=10, price_per_chair=15, pct=0.15, min_cents=20000)
    assert q.subtotal_cents == 15000
    assert q.amount_cents == 15000
    assert q.balance_cents == 0


def test_quote_pay_in_full_charges_the_whole_subtotal():
    q = deposits.quote(
        quantity=30, price_per_chair=100, kind="full", pct=0.15, min_cents=20000
    )
    assert q.amount_cents == q.subtotal_cents == 300000
    assert q.balance_cents == 0


def test_quote_rounds_partial_cents_up():
    # 7 × $9.99 = $69.93 → 6993¢; 15% = 1048.95¢ → ceil to 1049¢.
    q = deposits.quote(quantity=7, price_per_chair="9.99", pct=0.15, min_cents=0)
    assert q.subtotal_cents == 6993
    assert q.amount_cents == 1049


def test_quote_uses_decimal_not_float_for_the_subtotal():
    # 3 × 1.10 is 3.3000000000000003 in float; must land on exactly 330¢.
    q = deposits.quote(quantity=3, price_per_chair="1.10", pct=1.0, min_cents=0)
    assert q.subtotal_cents == 330


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quantity": 0, "price_per_chair": 100, "pct": 0.15, "min_cents": 0},
        {"quantity": -1, "price_per_chair": 100, "pct": 0.15, "min_cents": 0},
        {"quantity": 5, "price_per_chair": 0, "pct": 0.15, "min_cents": 0},
        {"quantity": 5, "price_per_chair": 100, "kind": "layaway", "pct": 0.15, "min_cents": 0},
        {"quantity": 5, "price_per_chair": 100, "pct": 1.5, "min_cents": 0},
    ],
)
def test_quote_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        deposits.quote(**kwargs)


# ─────────────────────────────── deposit rules ──────────────────────────────

def test_deposit_rules_default_to_site_settings():
    assert deposits.deposit_rules(None) == (0.15, 20000)
    assert deposits.deposit_rules({"deposit_pct_override": None}) == (0.15, 20000)


def test_deposit_rules_honor_a_per_lot_override_but_keep_the_global_floor():
    pct, min_cents = deposits.deposit_rules({"deposit_pct_override": 0.25})
    assert (pct, min_cents) == (0.25, 20000)


def test_deposit_rules_ignore_an_out_of_range_override():
    pct, _ = deposits.deposit_rules({"deposit_pct_override": 4})
    assert pct == 0.15


def test_quote_for_lot_uses_the_override():
    lot = {"price_per_chair": 100, "deposit_pct_override": 0.25}
    q = deposits.quote_for_lot(lot, quantity=30)
    assert q.amount_cents == 75000  # 25% of $3,000
    assert q.pct == 0.25


# ─────────────────────────── transition legality ────────────────────────────

def test_transition_pending_to_paid_is_legal(fake_db):
    row, changed = deposits.transition(1, "paid", payment_intent="pi_1")
    assert changed is True
    assert row["status"] == "paid"
    assert row["stripe_payment_intent"] == "pi_1"


def test_transition_backwards_from_paid_to_processing_is_a_noop(fake_db):
    fake_db.row = make_row(status="paid")
    row, changed = deposits.transition(1, "processing")
    assert changed is False
    assert row["status"] == "paid"


def test_transition_replaying_the_same_status_does_not_change_anything(fake_db):
    fake_db.row = make_row(status="paid")
    row, changed = deposits.transition(1, "paid")
    assert changed is False


def test_refunded_is_terminal(fake_db):
    fake_db.row = make_row(status="refunded")
    for target in ("paid", "processing", "pending", "canceled", "failed"):
        _, changed = deposits.transition(1, target)
        assert changed is False, target


def test_failed_and_canceled_are_terminal(fake_db):
    for start in ("failed", "canceled"):
        fake_db.row = make_row(status=start)
        _, changed = deposits.transition(1, "paid")
        assert changed is False, start


def test_transition_on_a_missing_row_is_a_noop(fake_db):
    fake_db.row = None
    row, changed = deposits.transition(999, "paid")
    assert (row, changed) == (None, False)


def test_transition_rejects_an_unknown_status(fake_db):
    with pytest.raises(ValueError):
        deposits.transition(1, "chargeback")


# ─────────────────────────── stripe event mapping ───────────────────────────

def _session_event(event_type, **session):
    session.setdefault("id", "cs_test_1")
    return {"type": event_type, "data": {"object": session}}


def test_completed_card_session_goes_straight_to_paid(fake_db):
    row, changed = deposits.apply_stripe_event(_session_event(
        "checkout.session.completed",
        payment_status="paid",
        payment_intent="pi_card",
        payment_method_types=["card", "us_bank_account"],
    ))
    assert changed is True
    assert row["status"] == "paid"
    assert row["stripe_payment_intent"] == "pi_card"
    assert row["payment_method"] == "card"


def test_completed_ach_session_goes_to_processing(fake_db):
    row, changed = deposits.apply_stripe_event(_session_event(
        "checkout.session.completed",
        payment_status="unpaid",
        payment_intent={"id": "pi_ach"},
        payment_method_types=["card", "us_bank_account"],
    ))
    assert changed is True
    assert row["status"] == "processing"
    assert row["stripe_payment_intent"] == "pi_ach"
    assert row["payment_method"] == "us_bank_account"


def test_async_payment_succeeded_settles_a_processing_row(fake_db):
    fake_db.row = make_row(status="processing")
    row, changed = deposits.apply_stripe_event(_session_event(
        "checkout.session.async_payment_succeeded",
        payment_status="paid",
        payment_intent="pi_ach",
        payment_method_types=["us_bank_account"],
    ))
    assert changed is True
    assert row["status"] == "paid"


def test_async_payment_failed_records_the_bank_reason(fake_db):
    fake_db.row = make_row(status="processing")
    row, changed = deposits.apply_stripe_event(_session_event(
        "checkout.session.async_payment_failed",
        payment_intent={"id": "pi_ach",
                        "last_payment_error": {"message": "R01 insufficient funds"}},
    ))
    assert changed is True
    assert row["status"] == "failed"
    assert row["failure_reason"] == "R01 insufficient funds"


def test_expired_session_cancels(fake_db):
    row, changed = deposits.apply_stripe_event(
        _session_event("checkout.session.expired")
    )
    assert changed is True
    assert row["status"] == "canceled"


def test_charge_refunded_is_matched_on_the_payment_intent(fake_db):
    fake_db.row = make_row(status="paid", stripe_payment_intent="pi_card")
    row, changed = deposits.apply_stripe_event({
        "type": "charge.refunded",
        "data": {"object": {"id": "ch_1", "payment_intent": "pi_card"}},
    })
    assert changed is True
    assert row["status"] == "refunded"


def test_unknown_event_type_is_ignored(fake_db):
    assert deposits.apply_stripe_event(
        _session_event("payment_intent.created")
    ) == (None, False)


def test_event_for_an_unknown_session_is_ignored(fake_db):
    fake_db.row = None
    assert deposits.apply_stripe_event(
        _session_event("checkout.session.completed", payment_status="paid")
    ) == (None, False)


def test_replayed_completed_event_does_not_change_twice(fake_db):
    event = _session_event(
        "checkout.session.completed", payment_status="paid",
        payment_intent="pi_card", payment_method_types=["card"],
    )
    _, first = deposits.apply_stripe_event(event)
    _, second = deposits.apply_stripe_event(event)
    assert (first, second) == (True, False)


# ───────────────────────────── site settings ────────────────────────────────

@pytest.fixture
def real_get_all(monkeypatch):
    """Undo the autouse stub — these tests exercise get_all() itself."""
    monkeypatch.setattr(site_settings, "get_all", _REAL_GET_ALL)
    return _REAL_GET_ALL


def test_settings_fall_back_to_defaults_when_the_db_is_down(monkeypatch, real_get_all):
    def boom(*a, **k):
        raise RuntimeError("BLACKWHOLE_DB_URL is not set")

    monkeypatch.setattr(site_settings.db, "fetch_all", boom)
    assert real_get_all() == site_settings.defaults()


def test_settings_read_values_from_the_table(monkeypatch, real_get_all):
    monkeypatch.setattr(site_settings.db, "fetch_all", lambda *a, **k: [
        {"key": "deposit_pct", "value": 0.2},
        {"key": "deposit_min_usd", "value": 300},
    ])
    assert real_get_all() == {"deposit_pct": 0.2, "deposit_min_usd": 300}


def test_settings_ignore_junk_rows_and_unknown_keys(monkeypatch, real_get_all):
    monkeypatch.setattr(site_settings.db, "fetch_all", lambda *a, **k: [
        {"key": "deposit_pct", "value": "not a number"},
        {"key": "something_else", "value": 1},
    ])
    got = real_get_all()
    assert got["deposit_pct"] == site_settings.defaults()["deposit_pct"]
    assert "something_else" not in got


def test_set_many_rejects_an_unknown_key(monkeypatch):
    monkeypatch.setattr(site_settings.db, "execute", lambda *a, **k: pytest.fail(
        "must validate before writing"))
    with pytest.raises(ValueError, match="unknown setting"):
        site_settings.set_many({"deposit_pct": 0.2, "admin_password": "x"})


@pytest.mark.parametrize("values", [
    {"deposit_pct": 1.5},        # over 100%
    {"deposit_pct": 0},          # a 0% deposit is not a deposit
    {"deposit_pct": "abc"},
    {"deposit_min_usd": -1},
    {"deposit_min_usd": 1_000_000},
])
def test_set_many_rejects_out_of_bounds_values(monkeypatch, values):
    monkeypatch.setattr(site_settings.db, "execute", lambda *a, **k: pytest.fail(
        "must validate before writing"))
    with pytest.raises(ValueError):
        site_settings.set_many(values)


def test_set_many_upserts_and_returns_the_new_settings(monkeypatch):
    written = []
    monkeypatch.setattr(site_settings.db, "execute",
                        lambda sql, params=None: written.append(params) or 1)
    monkeypatch.setattr(site_settings, "get_all", lambda: {"deposit_pct": 0.2,
                                                           "deposit_min_usd": 200})
    got = site_settings.set_many({"deposit_pct": "0.2"})
    assert got["deposit_pct"] == 0.2
    assert written and written[0][0] == "deposit_pct"
