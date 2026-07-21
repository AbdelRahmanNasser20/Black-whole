"""list_public(include_sold=True) appends recently-sold rows flagged is_sold,
after available rows. connect() is stubbed — no DB. Run:
  .venv/bin/python -m pytest tests/test_inventory_sold.py -v
"""
import contextlib
from automation import inventory


class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows
    def fetchone(self): return self._rows[0] if self._rows else None


class _FakeConn:
    """Returns available rows for the first execute(), sold rows for the second."""
    def __init__(self, available, sold):
        self._queues = [available, sold]
    def execute(self, sql, params=None):
        rows = self._queues.pop(0) if self._queues else []
        return _FakeCursor(rows)
    def commit(self): pass


def _stub(monkeypatch, available, sold):
    @contextlib.contextmanager
    def fake_connect():
        yield _FakeConn(available, sold)
    monkeypatch.setattr(inventory, "connect", fake_connect)


def test_lost_sold_out_is_a_valid_status():
    assert "lost_sold_out" in inventory.ALL_STATUSES


def test_sold_public_constants():
    assert inventory.SOLD_PUBLIC_STATUSES == ("sold_out", "lost_sold_out")
    assert inventory.SOLD_PUBLIC_LABEL == "SOLD OUT"


def test_include_sold_appends_flagged_rows(monkeypatch):
    available = [{"lot_id": "A", "status": "listed", "quantity_remaining": 5}]
    sold = [{"lot_id": "B", "status": "lost_sold_out", "quantity_remaining": 700}]
    _stub(monkeypatch, available, sold)
    rows = inventory.list_public(include_sold=True)
    assert [r["lot_id"] for r in rows] == ["A", "B"]      # available first
    assert rows[0]["is_sold"] is False
    assert rows[1]["is_sold"] is True


def test_default_excludes_sold(monkeypatch):
    available = [{"lot_id": "A", "status": "listed", "quantity_remaining": 5}]
    _stub(monkeypatch, available, [])
    rows = inventory.list_public()
    assert all(r["is_sold"] is False for r in rows)
    assert [r["lot_id"] for r in rows] == ["A"]
