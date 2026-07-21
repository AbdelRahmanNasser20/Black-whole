"""_resolve_folder_dir finds a lot folder whether it's flat or bucketed, and
_list_listing_folders descends status buckets. No DB. Run:
  .venv/bin/python -m pytest tests/web/test_folder_resolver.py -v
"""
import importlib

web_app = importlib.import_module("automation.web.app")


def test_resolves_flat_then_bucketed(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "DOWNLOAD_ROOT", tmp_path)
    (tmp_path / "FlatLot").mkdir()
    (tmp_path / "_lost").mkdir()
    (tmp_path / "_lost" / "LostLot").mkdir()

    assert web_app._resolve_folder_dir("FlatLot") == tmp_path / "FlatLot"
    assert web_app._resolve_folder_dir("LostLot") == tmp_path / "_lost" / "LostLot"
    assert web_app._resolve_folder_dir("NopeLot") is None
    assert web_app._resolve_folder_dir("") is None


def test_list_listing_folders_descends_buckets(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "DOWNLOAD_ROOT", tmp_path)
    (tmp_path / "_active").mkdir(); (tmp_path / "_active" / "A").mkdir()
    (tmp_path / "_lost").mkdir(); (tmp_path / "_lost" / "B").mkdir()
    (tmp_path / "_docs").mkdir(); (tmp_path / "_docs" / "readme").mkdir()
    (tmp_path / "Flat").mkdir()
    names = {p.name for p in web_app._list_listing_folders()}
    assert names == {"A", "B", "Flat"}      # _docs contents excluded
