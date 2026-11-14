"""A buy inquiry puts the buyer on the alert list (operator rule, 2026-11-14)."""
import contextlib

import pytest

from automation import inventory

REAL = inventory.subscribe_from_inquiry          # conftest stubs the attribute per test


class _Conn:
    def __init__(self, dup): self.dup, self.sql, self.params = dup, None, None
    def execute(self, sql, params=()):
        self.sql, self.params = sql, params
        return self
    def fetchone(self): return {"id": 9} if self.dup else None


@pytest.fixture
def db(monkeypatch):
    state = {"dup": False, "created": [], "conn": None}

    @contextlib.contextmanager
    def connect():
        state["conn"] = _Conn(state["dup"])
        yield state["conn"]
    monkeypatch.setattr(inventory, "connect", connect)
    monkeypatch.setattr(inventory, "create_subscriber",
                        lambda **kw: state["created"].append(kw) or {"id": 1, **kw})
    return state


BUY = {"id": 42, "kind": "buy", "name": "Pat", "email": "Pat@Example.com",
       "phone": "(602) 555-0101", "zip_code": "85054", "quantity_interested": 200,
       "lot_id": "31225", "message": "Do you deliver?"}


def test_buy_inquiry_carries_every_field_to_the_alert_list(db):
    row = REAL(BUY, {"source": "fb"})
    assert row and len(db["created"]) == 1
    kw = db["created"][0]
    assert (kw["name"], kw["email"], kw["phone"]) == ("Pat", "Pat@Example.com", "(602) 555-0101")
    assert kw["zip_code"] == "85054" and kw["quantity_wanted"] == 200
    assert kw["source"] == "site_inquiry" and kw["attribution"] == {"source": "fb"}
    assert kw["notes"] == "From inquiry #42 (lot 31225)"


def test_dedupe_matches_email_or_last_ten_phone_digits(db):
    REAL(dict(BUY, phone="+1 602-555-0101"))
    assert db["conn"].params == ("Pat@Example.com", "Pat@Example.com", "6025550101", "6025550101")


def test_someone_already_on_the_list_is_not_added_again(db):
    db["dup"] = True                 # also how an unsubscribed person stays off
    assert REAL(BUY) is None and db["created"] == []


@pytest.mark.parametrize("inquiry", [
    None,
    dict(BUY, kind="sell"),
    dict(BUY, message="[PLATFORM PAGE] wants access"),
    dict(BUY, email=None, phone=None),
])
def test_non_buyers_are_left_alone(db, inquiry):
    assert REAL(inquiry) is None and db["created"] == []


def test_a_failure_never_reaches_the_inquiry(db, monkeypatch):
    def boom(**kw): raise RuntimeError("db down")
    monkeypatch.setattr(inventory, "create_subscriber", boom)
    assert REAL(BUY) is None


def test_site_inquiry_is_an_allowed_source():
    assert "site_inquiry" in inventory.SUBSCRIBER_SOURCES


def test_contact_endpoint_hands_buy_inquiries_to_the_alert_list(monkeypatch, _no_real_alert_list_writes):
    from fastapi.testclient import TestClient
    import importlib
    web = importlib.import_module("automation.web.app")
    monkeypatch.setattr(inventory, "create_inquiry", lambda **kw: dict(BUY, **{"created_at": "x"}))
    async def _quiet(row): return None
    monkeypatch.setattr(web, "_notify_new_inquiry", _quiet)
    r = TestClient(web.app, base_url="https://testserver").post(
        "/contact", json={"kind": "buy", "name": "Pat", "email": "p@example.com", "zip_code": "85054"})
    assert r.status_code == 200
    assert [x["id"] for x in _no_real_alert_list_writes] == [42]
