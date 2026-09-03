"""list_with_stats must do all its reads on ONE connection (the pooler
handshake is ~1.3 s; the old two-endpoint path opened two)."""
from automation import inventory


class _Cur:
    def __init__(self, sql):
        self.sql = sql
    def fetchall(self):
        return [{"lot_id": "1", "status": "listed"}] if "FROM inventory ORDER BY" in self.sql else []
    def fetchone(self):
        return {"n": 3}


class _Conn:
    def __init__(self, log):
        self.log = log
    def __enter__(self):
        self.log.append("open")
        return self
    def __exit__(self, *a):
        return False
    def execute(self, sql, params=None):
        return _Cur(sql)


def test_list_with_stats_opens_one_connection(monkeypatch):
    log = []
    monkeypatch.setattr(inventory, "connect", lambda: _Conn(log))
    out = inventory.list_with_stats()
    assert log == ["open"]
    assert out["items"] == [{"lot_id": "1", "status": "listed"}]
    assert out["stats"] == {"lots": 3, "chairs": 3, "cities": 3, "moved": 3}
