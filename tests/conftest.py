"""Suite-wide guard: no test may put a real person on the alert list.

`POST /contact` hands every buy inquiry to `inventory.subscribe_from_inquiry`,
which writes to `subscribers`. Tests that stub `create_inquiry` would otherwise
reach the real table. Tests of the function itself keep a reference to the real
one (see tests/test_inquiry_subscriber.py).
"""
import pytest

from automation import inventory


@pytest.fixture(autouse=True)
def _no_real_alert_list_writes(monkeypatch):
    calls = []
    monkeypatch.setattr(inventory, "subscribe_from_inquiry",
                        lambda row, attribution=None: calls.append(row))
    return calls


@pytest.fixture(autouse=True)
def _fresh_read_memo():
    """Public pages (`/`, `/listings`, a lot page) are memoised 60 s in
    `readcache`; a memo from one test must never answer the next one."""
    from automation.web import readcache
    readcache.invalidate_all()
    yield
    readcache.invalidate_all()


@pytest.fixture(autouse=True)
def _no_flip_top_read(monkeypatch):
    """`/platform` asks `flip_top.top()`, which starts a background read of
    the real database. Off in tests; a test of it sets the flag back to 1."""
    monkeypatch.setenv("FLIP_TOP_ENABLED", "0")
