"""`upsert_from_run` must not stomp operator edits on an existing lot.

Lot 31225 (2026-10-13): the public page kept the GovDeals auction text
("2,100 chairs + 1,600 more") after the lot was split, and a re-run would have
overwritten any fix to `description`. Operator copy now wins once it is set.
"""
from automation import inventory


def _run_update(monkeypatch):
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
    monkeypatch.setattr(inventory, "get", lambda lot_id: {"lot_id": lot_id, "status": "listed"})
    inventory.upsert_from_run(
        lot_id="31225", seller_id=None, govdeals_url=None, folder_name=None,
        folder_path=None, sku=None, title="t", description="2,100 stale chairs",
        city=None, state=None, zip_code=None, contact_name=None, contact_email=None,
        contact_phone=None, chair_type=None, dimensions=None, quantity=3700,
        price_per_chair=None, hero_image=None,
    )
    (sql, params), = statements
    return sql, params


def _assignment(sql, col):
    for line in sql.splitlines():
        if line.strip().startswith(f"{col} "):
            return line.split("=", 1)[1].strip().rstrip(",")
    raise AssertionError(f"{col} not in UPDATE")


def test_existing_description_survives_a_rerun(monkeypatch):
    sql, params = _run_update(monkeypatch)
    assert sql.lstrip().startswith("UPDATE inventory")
    # Existing (non-blank) value first: the run's text only fills a gap.
    assert _assignment(sql, "description") == "COALESCE(NULLIF(description, ''), %s)"
    assert "2,100 stale chairs" in params


def test_other_user_edits_still_preserved(monkeypatch):
    sql, _ = _run_update(monkeypatch)
    assert _assignment(sql, "price_per_chair") == "COALESCE(price_per_chair, %s)"
    assert _assignment(sql, "hero_image") == "COALESCE(hero_image, %s)"
    set_clause = sql.split("SET", 1)[1]
    for col in ("quantity_remaining", "status", "facebook_url", "ebay_url"):
        assert f" {col} " not in set_clause, col
