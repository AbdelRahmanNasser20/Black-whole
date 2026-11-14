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
