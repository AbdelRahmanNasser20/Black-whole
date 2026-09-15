"""Migration 010 columns on the Favorite model + the writer that stamps them.

The public map shows only dewatermarked R2 copies, never the raw GovDeals
image_url, and never a favorite the operator tagged ``#private``.
"""
from automation import favorites


def _favorite(**over):
    kw = dict(asset_id="1/2", link="x", title="t", quantity=1, end_date_iso=None,
              end_date_raw=None, image_url="raw", location="Boise, ID",
              starred_at="2026-01-01", last_synced_at="2026-01-01",
              notes="keep #Private", sent_intervals=[])
    kw.update(over)
    return favorites.Favorite(**kw)


def test_favorite_defaults_and_private():
    f = _favorite()
    assert f.clean_hero_url is None and f.clean_image_urls == []
    assert f.is_private is True
    d = f.to_dict()
    assert d["clean_hero_url"] is None and d["clean_image_urls"] == []


def test_is_private_false_without_the_tag():
    assert _favorite(notes="ATL pickup").is_private is False
    assert _favorite(notes=None).is_private is False


def test_row_mapper_without_migration_degrades_to_no_photo():
    """An unapplied migration 010 means "no photo", never a KeyError/500."""
    row = {
        "asset_id": "1/2", "link": "x", "title": "t", "quantity": 1,
        "end_date_iso": None, "end_date_raw": None, "image_url": "raw",
        "location": "Boise, ID", "starred_at": "2026-01-01",
        "last_synced_at": "2026-01-01", "notes": None,
    }
    f = favorites._row_to_favorite(row, [])
    assert f.clean_hero_url is None and f.clean_image_urls == []


def test_row_mapper_reads_the_clean_columns():
    row = {
        "asset_id": "1/2", "link": "x", "title": "t", "quantity": 1,
        "end_date_iso": None, "end_date_raw": None, "image_url": "raw",
        "location": "Boise, ID", "starred_at": "2026-01-01",
        "last_synced_at": "2026-01-01", "notes": None,
        "clean_hero_url": "https://r2/fav-1-2.jpg",
        "clean_image_urls": ["https://r2/fav-1-2/00.jpg"],
    }
    f = favorites._row_to_favorite(row, [])
    assert f.clean_hero_url == "https://r2/fav-1-2.jpg"
    assert f.clean_image_urls == ["https://r2/fav-1-2/00.jpg"]
    assert f.to_dict()["clean_image_urls"] == ["https://r2/fav-1-2/00.jpg"]


def test_set_clean_images_sql(monkeypatch):
    calls = []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): calls.append((sql, params))

    monkeypatch.setattr(favorites.inventory, "connect", lambda: _Conn())
    favorites.set_clean_images("1/2", "https://r2/fav-1-2.jpg", ["https://r2/fav-1-2/00.jpg"])
    sql, params = calls[0]
    assert "UPDATE auction_favorites" in sql and "clean_hero_url" in sql
    assert params[0] == "https://r2/fav-1-2.jpg" and params[-1] == "1/2"


def test_set_clean_images_serialises_urls_as_json(monkeypatch):
    import json
    calls = []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): calls.append((sql, params))

    monkeypatch.setattr(favorites.inventory, "connect", lambda: _Conn())
    favorites.set_clean_images("1/2", None, None)
    sql, params = calls[0]
    assert json.loads(params[1]) == []
    assert params[0] is None
