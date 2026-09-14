"""inventory.stats() must be ONE round trip. It was four sequential queries;
on a ~0.4 s/round-trip pooler link that alone made /api/inventory-stats and
the public landing page multi-second."""
from automation import inventory


class _Cur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self):
        self.sqls: list[str] = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        return _Cur({"lots": 3, "chairs": 1200, "cities": 2, "moved": 850})


def test_stats_is_a_single_statement():
    conn = _Conn()
    out = inventory._stats_on(conn)
    assert len(conn.sqls) == 1
    assert out == {"lots": 3, "chairs": 1200, "cities": 2, "moved": 850}


def test_stats_treats_null_sums_as_zero():
    class _NullConn(_Conn):
        def execute(self, sql, params=None):
            self.sqls.append(sql)
            return _Cur({"lots": 0, "chairs": None, "cities": 0, "moved": None})

    assert inventory._stats_on(_NullConn()) == {"lots": 0, "chairs": 0, "cities": 0, "moved": 0}
