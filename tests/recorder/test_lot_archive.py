"""Lot archive — offline, against real maestro payloads (5282/3780 detail captured
2026-09-29, bidbox finals from 2026-09-28). No network, no DB, no R2: a
LocalStore under tmp_path, fake adapters and a generated JPEG."""
import copy
import gzip
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from recorder import cli, lot_archive
from recorder.sources import govdeals

FIX = Path(__file__).parent / "fixtures" / "govdeals"
DETAIL = json.loads((FIX / "detail_5282_3780.json").read_text())["detail"]
BIDBOX = json.loads((FIX / "bidbox_examples.json").read_text())["payloads"]
KEY = (5282, 3780, 2)
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 3, 1, 21, 51, tzinfo=timezone.utc)


def _jpeg(w=2000, h=1500) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 60, 30)).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


class _Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status


class FakeAdapter:
    def __init__(self, detail=None, bidbox=None, bidbox_exc=None):
        self.detail = copy.deepcopy(DETAIL) if detail is None else detail
        self.bidbox = copy.deepcopy(BIDBOX["soa_old_5282_3780_2"]["bidbox"]) if bidbox is None else bidbox
        self.bidbox_exc = bidbox_exc
        self.calls = []

    def fetch_bid_state(self, a, b, c):
        self.calls.append(("bidbox", a, b, c))
        if self.bidbox_exc:
            raise self.bidbox_exc
        return self.bidbox

    def fetch_detail(self, a, b):
        self.calls.append(("detail", a, b))
        return self.detail


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError(f"dry-run / skipped lot must not call {name}")


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(govdeals, "BIDBOX_MIN_INTERVAL_SECONDS", 0.0)


@pytest.fixture
def store(tmp_path):
    return lot_archive.LocalStore(tmp_path)


def _timeline(lk, source="govdeals"):
    return ([{"t": "2026-09-02T11:49:44+00:00", "source": "recorder", "status": "active",
              "current_bid": 270.0, "bid_count": 14},
             {"t": "2026-09-02T23:10:23+00:00", "source": "recorder", "status": "active",
              "current_bid": 460.0, "bid_count": 19}], None)


def _http(url, timeout=30):
    return _Resp(_jpeg())


def _archive(store, adapter=None, **kw):
    kw.setdefault("end_date", END)
    kw.setdefault("now", NOW)
    return lot_archive.archive_lot(KEY, store=store, adapter=adapter or FakeAdapter(),
                                   http_get=kw.pop("http_get", _http), timeline_fn=_timeline, **kw)


# --- keys / pure helpers -------------------------------------------------------

def test_key_layout():
    assert lot_archive.doc_key(KEY) == "archive/lots/govdeals/5282_3780_2.json.gz"
    assert lot_archive.photo_key("5282/3780/2", 3) == "archive/lots/govdeals/5282_3780_2/3.jpg"
    assert lot_archive.meta_key(KEY) == "archive/lots/govdeals/_meta/5282_3780_2.json"
    assert lot_archive.analysis_key(KEY) == "archive/lots/govdeals/_analysis/5282_3780_2.json"
    assert lot_archive.parse_slug("5282_3780_2") == KEY
    assert lot_archive.parse_slug("nope") is None


def test_serialize_roundtrip_is_deterministic():
    doc = {"lot_key": "1/2/3", "x": [1, 2]}
    assert lot_archive.parse(lot_archive.serialize(doc)) == doc
    assert lot_archive.serialize(doc) == lot_archive.serialize(doc)   # mtime=0


def test_resize_caps_longest_side_and_strips_to_jpeg():
    data, w, h = lot_archive.resize_jpeg(_jpeg(2000, 1500))
    assert (w, h) == (1024, 768)
    assert data[:2] == b"\xff\xd8"
    assert lot_archive.resize_jpeg(b"not an image") is None


def test_html_to_text():
    assert lot_archive.html_to_text("a<br />b&amp;c<p>d</p>") == "a\nb&c\nd"
    assert lot_archive.html_to_text(None) == ""


def test_summarize_from_real_payloads():
    s = lot_archive.summarize(DETAIL, BIDBOX["soa_old_5282_3780_2"]["bidbox"], None)
    assert s["title"] == "Lot of Approx. 150 Stacking Chairs"
    assert s["final_price"] == 1725.0 and s["bid_count"] == 54
    assert s["outcome"] == "sold" and s["status_code"] == "SOA"
    assert s["closed_at"] == "2026-09-03T01:21:51+00:00"
    assert s["canonical_category"] == "seating_furniture"
    assert s["city"] == "Pittsburgh" and s["state"] == "PA"
    assert "<br" not in s["description"]


def test_summarize_rnm_is_not_a_sale():
    s = lot_archive.summarize(None, BIDBOX["rnm_8_32408_4"]["bidbox"], None)
    assert s["outcome"] == "reserve_not_met"


def test_summarize_without_bidbox_falls_back_to_timeline_price_and_no_outcome():
    s = lot_archive.summarize(DETAIL, None, None, _timeline("x")[0])
    assert s["final_price"] == 460.0
    assert s["outcome"] is None            # never guessed without the bidbox


# --- archive_lot -----------------------------------------------------------------

def test_archive_writes_doc_photos_meta_and_reads_back(store):
    r = _archive(store, max_photos=6)
    assert r.result == "archived", r.reason
    assert r.photos == 3                    # the lot has 3 photos
    doc = lot_archive.load(store, KEY)
    assert doc["lot_key"] == "5282/3780/2" and doc["completeness"] == "full"
    assert doc["bidbox"]["currentBid"] == 1725.0
    assert len(doc["timeline"]) == 2 and len(doc["gallery_urls"]) == 3
    assert all(store.exists(p["key"]) for p in doc["photos"])
    meta = json.loads(store.get(lot_archive.meta_key(KEY)))
    assert meta["outcome"] == "sold" and meta["photo_count"] == 3
    assert lot_archive.archived_slugs(store) == {"5282_3780_2"}


def test_archive_respects_photo_cap(store):
    assert _archive(store, max_photos=1).photos == 1


def test_archive_is_idempotent_without_a_request(store):
    _archive(store)
    r = lot_archive.archive_lot(KEY, store=store, adapter=NoNetwork(), http_get=None,
                                timeline_fn=None, end_date=END, now=NOW)
    assert r.result == "skipped_exists" and r.requests == 0


def test_stored_bidbox_skips_the_bidbox_read(store):
    a = FakeAdapter()
    _archive(store, adapter=a, bidbox=BIDBOX["soa_old_5282_3780_2"]["bidbox"])
    assert [c[0] for c in a.calls] == ["detail"]


def test_thin_detail_on_a_fresh_close_is_not_ready(store):
    r = _archive(store, adapter=FakeAdapter(detail={"assetId": 5282}), end_date=NOW - timedelta(hours=5))
    assert r.result == "not_ready" and not store.exists(lot_archive.doc_key(KEY))


def test_thin_detail_on_an_old_close_is_archived_partial(store):
    r = _archive(store, adapter=FakeAdapter(detail={}), end_date=NOW - timedelta(days=5))
    assert r.result == "archived"
    assert lot_archive.load(store, KEY)["completeness"] == "partial"


def test_close_under_an_hour_ago_waits(store):
    r = _archive(store, adapter=NoNetwork(), end_date=NOW - timedelta(minutes=20))
    assert r.result == "not_ready"


def test_bidbox_still_live_waits(store):
    live = copy.deepcopy(BIDBOX["sta_live_11_3156_1"]["bidbox"])
    live["assetAuctionEndDateUTC"] = (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = _archive(store, adapter=FakeAdapter(bidbox=live))
    assert r.result == "not_ready" and "extended" in r.reason


def test_bidbox_error_waits(store):
    import requests
    r = _archive(store, adapter=FakeAdapter(bidbox_exc=requests.exceptions.ConnectionError("x")))
    assert r.result == "not_ready"


def test_bidbox_purged_still_archives_as_partial(store):
    r = _archive(store, adapter=FakeAdapter(bidbox={}))
    assert r.result == "archived"
    doc = lot_archive.load(store, KEY)
    assert doc["bidbox_result"] == "purged" and doc["completeness"] == "partial"


def test_failed_photo_is_skipped_not_fatal(store):
    r = _archive(store, http_get=lambda url, timeout=30: _Resp(b"", 404))
    assert r.result == "archived" and r.photos == 0


def test_readback_mismatch_raises(store, monkeypatch):
    real_get = store.get
    monkeypatch.setattr(store, "get", lambda k: b"corrupt" if k.endswith(".jpg") else real_get(k))
    with pytest.raises(RuntimeError, match="readback"):
        _archive(store)
    assert not store.exists(lot_archive.doc_key(KEY))    # no done marker


# --- run_archive / CLI ---------------------------------------------------------------

def test_dry_run_sends_nothing(store):
    rows = [{"source_lot_id": "5282/3780/2", "end_date": END}, {"source_lot_id": "1/2/3", "end_date": END}]
    m = lot_archive.run_archive(rows, store=store, adapter=NoNetwork(), http_get=None,
                                timeline_fn=None, limit=10, apply=False)
    assert m["would_archive"] == 2 and m["requests"] == 0 and not store.list(lot_archive.PREFIX + "/")


def test_run_archive_limit_and_skip_existing(store):
    _archive(store)
    rows = [{"source_lot_id": "5282/3780/2", "end_date": END}]
    m = lot_archive.run_archive(rows, store=store, adapter=NoNetwork(), http_get=None,
                                timeline_fn=None, limit=10, apply=True)
    assert m["skipped_exists"] == 1 and m["archived"] == 0


def test_run_archive_isolates_a_failing_lot(store):
    rows = [{"source_lot_id": "5282/3780/2", "end_date": END}]
    boom = FakeAdapter()
    boom.fetch_detail = lambda a, b: (_ for _ in ()).throw(RuntimeError("boom"))
    m = lot_archive.run_archive(rows, store=store, adapter=boom, http_get=_http,
                                timeline_fn=lambda lk, source="govdeals": (_ for _ in ()).throw(RuntimeError("db down")),
                                limit=10, apply=True)
    assert m["error"] == 1


def test_store_from_env(monkeypatch, tmp_path):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("LOT_ARCHIVE_STORE", raising=False)
    assert lot_archive.store_from_env() is None
    with pytest.raises(lot_archive.StoreNotConfigured):
        lot_archive.require_store()
    monkeypatch.setenv("LOT_ARCHIVE_STORE", "local")
    monkeypatch.setenv("LOT_ARCHIVE_LOCAL_DIR", str(tmp_path))
    assert isinstance(lot_archive.store_from_env(), lot_archive.LocalStore)


def test_local_store_refuses_escaping_keys(store):
    with pytest.raises(ValueError):
        store.put("../evil", b"x", "text/plain")


def test_apply_without_a_store_is_a_hard_error(monkeypatch, capsys):
    monkeypatch.setattr(lot_archive, "store_from_env", lambda: None)
    assert cli.cmd_archive_backfill("govdeals", 30, 5, apply=True, lots=["1/2/3"]) == 1
    assert "refusing to archive" in capsys.readouterr().err


def test_archive_backfill_rejects_other_sources():
    assert cli.cmd_archive_backfill("mibid", 30, 5, apply=False) == 2


def test_archive_backfill_apply_named_lots(store, monkeypatch, capsys):
    monkeypatch.setattr(cli.store, "lot_timeline", _timeline)
    monkeypatch.setattr(cli.store, "lot_archive_index_exists", lambda: False)
    rc = cli.cmd_archive_backfill("govdeals", 30, 5, apply=True, lots=["5282/3780/2"],
                                  archive_store=store, adapter=FakeAdapter(), http_get=_http)
    out = capsys.readouterr().out
    assert rc == 0 and "archived=1" in out and "KB/lot" in out and "GB/month" in out
    assert store.exists(lot_archive.doc_key(KEY))


def test_archive_pending_without_store_is_a_note_not_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(lot_archive, "store_from_env", lambda: None)
    assert cli.cmd_archive_pending({}) == 0
    assert "R2 not configured" in capsys.readouterr().err


def test_archive_pending_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RECORDER_ARCHIVE_ENABLED", "0")
    monkeypatch.setattr(lot_archive, "store_from_env", lambda: (_ for _ in ()).throw(AssertionError))
    assert cli.cmd_archive_pending({}) == 0


def test_storage_projection_math():
    p = cli.storage_projection(800_000, monthly_lots=125_000, months=12)
    assert p["gb_per_month"] == 100.0
    assert p["gb_after"] == 1200.0
    assert p["usd_per_month_after"] == round((1200 - 10) * 0.015, 2)
    assert cli.storage_projection(1000, monthly_lots=1000, months=1)["usd_per_month_after"] == 0.0


def test_parser_archive_commands_default_to_dry_run():
    a = cli.build_parser().parse_args(["archive-backfill"])
    assert a.apply is False and a.since_days == 30 and a.source == "govdeals"
    a = cli.build_parser().parse_args(["archive-analyze", "--lot", "1/2/3"])
    assert a.lot == "1/2/3" and a.force is False


def test_scope_all_refused_when_database_is_too_big(monkeypatch, capsys):
    monkeypatch.delenv("RECORDER_GOVDEALS_SCOPE", raising=False)
    monkeypatch.setattr(cli.store, "database_size_mb", lambda: 6500.0)
    assert cli._govdeals_scope_for_run() == "furniture"
    assert "refused" in capsys.readouterr().err
    monkeypatch.setattr(cli.store, "database_size_mb", lambda: 595.0)
    assert cli._govdeals_scope_for_run() == "all"
    monkeypatch.setenv("RECORDER_GOVDEALS_SCOPE", "furniture")
    assert cli._govdeals_scope_for_run() == "furniture"


def test_numeric_fields_do_not_break_the_summary():
    d = dict(DETAIL, zipCode=15208, city=None, companyName=12)
    s = lot_archive.summarize(d, None, {"locationCity": "Pittsburgh"})
    assert s["zip"] == "15208" and s["city"] == "Pittsburgh" and s["seller"] == "12"


def test_named_lot_takes_its_close_from_the_bidbox(store):
    # a purged detail (204) for a reserve-not-met lot a week old → partial archive now
    rnm = copy.deepcopy(BIDBOX["rnm_8_32408_4"]["bidbox"])
    r = lot_archive.archive_lot((8, 32408, 4), store=store, adapter=FakeAdapter(detail={}, bidbox=rnm),
                                http_get=_http, timeline_fn=_timeline, end_date=None,
                                now=datetime(2026, 10, 5, tzinfo=timezone.utc))
    assert r.result == "archived" and r.outcome == "reserve_not_met"
    assert lot_archive.load(store, (8, 32408, 4))["completeness"] == "partial"


def test_non_sale_with_purged_detail_is_archived_at_once(store):
    rnm = copy.deepcopy(BIDBOX["rnm_8_32408_4"]["bidbox"])
    r = lot_archive.archive_lot((8, 32408, 4), store=store, adapter=FakeAdapter(detail={}, bidbox=rnm),
                                http_get=_http, timeline_fn=_timeline,
                                end_date=NOW - timedelta(hours=6), now=NOW)
    assert r.result == "archived" and r.outcome == "reserve_not_met"


def test_sold_lot_with_purged_detail_still_waits(store):
    r = _archive(store, adapter=FakeAdapter(detail={}), end_date=NOW - timedelta(hours=6))
    assert r.result == "not_ready"


def test_purged_detail_falls_back_to_the_sweep_cover_photo(store):
    rnm = copy.deepcopy(BIDBOX["rnm_8_32408_4"]["bidbox"])
    seen = []
    r = lot_archive.archive_lot((8, 32408, 4), store=store, adapter=FakeAdapter(detail={}, bidbox=rnm),
                                http_get=lambda u, timeout=30: seen.append(u) or _Resp(_jpeg()),
                                timeline_fn=lambda lk, source="govdeals": ([], {"photo": "abc.jpg", "assetShortDescription": "Desk"}),
                                end_date=NOW - timedelta(hours=6), now=NOW)
    assert r.photos == 1 and seen == ["https://webassets.lqdt1.com/assets/photos/32408/abc.jpg"]


def test_force_rearchives(store):
    _archive(store)
    r = _archive(store, force=True)
    assert r.result == "archived"
