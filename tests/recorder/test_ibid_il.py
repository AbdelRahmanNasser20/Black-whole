"""Offline tests for the Illinois iBid adapter, against pages captured live
2026-10-09 (see recorder/sources/ibid_il.py's docstring). No network —
polite_get is monkeypatched.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from recorder.models import Observation
from recorder.sources import ibid_il
from recorder.sources.base import SourceFetchFailed

FIXTURES = Path(__file__).parent / "fixtures" / "ibid_il"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text()


class _Resp:
    def __init__(self, text="", status_code=200, url=ibid_il.BROWSE_URL):
        self.text = text
        self.status_code = status_code
        self.url = url


def _router(item_pages: dict[str, str] | None = None, *, browse_all_status=200,
            item_status: dict[str, int] | None = None, calls: list | None = None):
    item_pages = item_pages or {}
    item_status = item_status or {}

    def fake(url, *, params=None, headers=None, **kw):
        if calls is not None:
            calls.append((url, dict(params or {}), dict(headers or {})))
        pid = str((params or {}).get("id"))
        if url == ibid_il.BROWSE_URL:
            if pid == ibid_il.ALL_CATEGORY_ID:
                return _Resp(_html("browse_all.html"), browse_all_status)
            return _Resp(_html("browse_furniture.html"))
        if pid in item_status:
            return _Resp("", item_status[pid], url)
        if pid in item_pages:
            return _Resp(_html(item_pages[pid]), 200, url)
        return _Resp("", 502, url)
    return fake


ITEMS = {
    "418484": "item_active_418484.html",
    "418064": "item_active_418064.html",
    "418000": "item_closed_418000.html",
    "417990": "item_closed_417990.html",
}


# --- parsers -----------------------------------------------------------

def test_parse_browse_reads_every_row_and_central_clock():
    rows, now = ibid_il.parse_browse(_html("browse_all.html"))
    assert len(rows) == 86
    # 11:44:44 CDT on the page == 16:44:44 UTC
    assert now == datetime(2026, 10, 9, 16, 44, 44, tzinfo=timezone.utc)
    assert all(r["url"].startswith(ibid_il.ITEM_URL) for r in rows)
    assert all(r["end_date"] is not None and r["end_date"].tzinfo for r in rows)


def test_parse_browse_furniture_row():
    rows, _ = ibid_il.parse_browse(_html("browse_furniture.html"))
    assert [r["lot_id"] for r in rows] == ["418484"]
    r = rows[0]
    assert r["current_bid"] == Decimal("5.00")
    assert r["bid_count"] == 0
    assert r["closed"] is False


def test_parse_item_active_uses_absolute_end_in_central():
    d = ibid_il.parse_item(_html("item_active_418484.html"))
    assert d["lot_id"] == "418484"
    assert d["category"] == "Furniture"
    assert (d["city"], d["state"]) == ("Springfield", "IL")
    assert d["closed"] is False
    assert d["end_date"] == datetime(2026, 10, 11, 0, 28, tzinfo=timezone.utc)
    assert d["images"]


def test_parse_item_closed_keeps_final_bid_and_count():
    d = ibid_il.parse_item(_html("item_closed_418000.html"))
    assert d["closed"] is True
    assert d["title"] == "Lot # 6169 STOOLS (6)"
    assert d["current_bid"] == Decimal("28.00")
    assert d["bid_count"] == 7


def test_parse_item_rejects_non_lot_page():
    assert ibid_il.parse_item("<html><body>nothing</body></html>") is None


def test_relative_parser():
    assert ibid_il._relative("1d 7h 16m") == (timedelta(days=1, hours=7, minutes=16), False)
    assert ibid_il._relative("closed") == (None, True)
    assert ibid_il._relative("??") == (None, False)


# --- discover() --------------------------------------------------------

def test_discover_keeps_furniture_category_and_title_matches(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ibid_il, "polite_get", _router(ITEMS, calls=calls))
    obs = ibid_il.IBidIllinoisSource().discover()
    ids = {o.source_lot_id for o in obs}
    assert "418484" in ids                     # Furniture category, no term match needed
    assert "418064" not in ids                 # TVs
    for o in obs:
        assert isinstance(o, Observation)
        assert o.source == "ibid_il"
        assert o.status == "active"
        assert o.end_date is not None and o.end_date.tzinfo is not None
        for key in ("title", "city", "state", "category", "image_url", "url"):
            assert key in o.raw
    # every browse request sends the 200-per-page cookie
    browse = [c for c in calls if c[0] == ibid_il.BROWSE_URL]
    assert browse and all(c[2].get("Cookie") == ibid_il.PAGING_COOKIE for c in browse)


def test_discover_raw_is_json_safe(monkeypatch):
    import json
    monkeypatch.setattr(ibid_il, "polite_get", _router(ITEMS))
    for o in ibid_il.IBidIllinoisSource().discover():
        json.dumps(o.raw)


def test_discover_raises_when_all_browse_fails(monkeypatch):
    monkeypatch.setattr(ibid_il, "polite_get", _router(ITEMS, browse_all_status=503))
    with pytest.raises(SourceFetchFailed):
        ibid_il.IBidIllinoisSource().discover()


def test_discover_raises_on_transport_error(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(ibid_il, "polite_get", boom)
    with pytest.raises(SourceFetchFailed):
        ibid_il.IBidIllinoisSource().discover()


# --- poll() ------------------------------------------------------------

def test_poll_reads_closed_final(monkeypatch):
    monkeypatch.setattr(ibid_il, "polite_get", _router(ITEMS))
    obs = ibid_il.IBidIllinoisSource().poll([{"source_lot_id": "418000", "end_date": None}])
    assert len(obs) == 1
    assert obs[0].status == "closed"
    assert obs[0].current_bid == Decimal("28.00")
    assert obs[0].bid_count == 7


def test_poll_404_past_end_is_gone(monkeypatch):
    monkeypatch.setattr(ibid_il, "polite_get", _router(ITEMS, item_status={"1": 404}))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    obs = ibid_il.IBidIllinoisSource().poll([{"source_lot_id": "1", "end_date": past}])
    assert [o.status for o in obs] == ["gone"]


def test_poll_502_is_never_gone(monkeypatch):
    monkeypatch.setattr(ibid_il, "polite_get", _router(ITEMS, item_status={"1": 502}))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert ibid_il.IBidIllinoisSource().poll([{"source_lot_id": "1", "end_date": past}]) == []


def test_poll_empty():
    assert ibid_il.IBidIllinoisSource().poll([]) == []


# --- sold_sweep() ------------------------------------------------------

def test_sold_sweep_zero_closed_rows_is_empty(monkeypatch):
    monkeypatch.setattr(ibid_il, "polite_get", _router(ITEMS))
    assert ibid_il.IBidIllinoisSource().sold_sweep() == []


def test_sold_sweep_picks_up_closed_furniture_row(monkeypatch):
    furn = _html("browse_furniture.html")
    # flip the furniture row's "Ends in" cell to `closed` and point it at 418000
    rows, _ = ibid_il.parse_browse(furn)
    patched = furn.replace("418484", "418000")
    import re
    patched = re.sub(r"(<td[^>]*>)\s*\d+d[^<]*(</td>\s*</tr>)", r"\1closed\2", patched, count=1)
    if "closed" not in patched:
        pytest.skip("fixture row shape changed")

    def fake(url, *, params=None, headers=None, **kw):
        pid = str((params or {}).get("id"))
        if url == ibid_il.BROWSE_URL:
            return _Resp(patched if pid == ibid_il.FURNITURE_CATEGORY_ID else _html("browse_all.html"))
        return _Resp(_html("item_closed_418000.html"), 200, url)

    monkeypatch.setattr(ibid_il, "polite_get", fake)
    obs = ibid_il.IBidIllinoisSource().sold_sweep()
    assert [o.source_lot_id for o in obs] == ["418000"]
    assert obs[0].status == "closed"
    assert obs[0].current_bid == Decimal("28.00")
