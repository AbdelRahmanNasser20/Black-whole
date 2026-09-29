"""Admin lot archive: the rebuilt page, the list API, photos, re-run — from a
LocalStore fixture, no GovDeals, no R2, no DB (the index lookup is stubbed).
Plus the privacy rules: every route is session-walled and no public template
ever points at the archive."""
import io
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from automation.web import auth as auth_svc
from automation.web import lot_archive_view, readcache
from automation.web.app import app
from recorder import lot_archive

FIX = Path(__file__).resolve().parents[1] / "recorder" / "fixtures" / "govdeals"
DETAIL = json.loads((FIX / "detail_5282_3780.json").read_text())["detail"]
BIDBOX = json.loads((FIX / "bidbox_examples.json").read_text())["payloads"]
TEMPLATES = Path("automation/web/templates")


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(buf, format="JPEG")
    return buf.getvalue()


def _put_lot(store, key, detail, bidbox, timeline=(), photos=1):
    k = tuple(int(p) for p in key.split("/"))
    ph = []
    for i in range(photos):
        store.put(lot_archive.photo_key(k, i), _jpeg(), "image/jpeg")
        ph.append({"i": i, "key": lot_archive.photo_key(k, i), "bytes": 1, "w": 40, "h": 30, "src": "x"})
    doc = lot_archive.build_document(k, detail=detail, gallery=["x"] * photos, bidbox=bidbox,
                                     bidbox_result="ok", search_raw=None, timeline=list(timeline),
                                     photos=ph, completeness="full")
    store.put(lot_archive.meta_key(k), json.dumps(lot_archive.meta_of(doc), default=str).encode(), "application/json")
    store.put(lot_archive.doc_key(k), lot_archive.serialize(doc), "application/gzip")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    monkeypatch.setenv("LOT_ARCHIVE_STORE", "local")
    monkeypatch.setenv("LOT_ARCHIVE_LOCAL_DIR", str(tmp_path))
    monkeypatch.setattr(lot_archive_view, "_metas_from_index", lambda: None)
    lot_archive_view.reset()
    readcache.invalidate_all()
    s = lot_archive.LocalStore(tmp_path)
    timeline = [{"t": "2026-09-02T11:49:44+00:00", "source": "recorder", "current_bid": 270.0, "bid_count": 14},
                {"t": "2026-09-02T23:10:23+00:00", "source": "recorder", "current_bid": 460.0, "bid_count": 19}]
    _put_lot(s, "5282/3780/2", DETAIL, BIDBOX["soa_old_5282_3780_2"]["bidbox"], timeline, photos=2)
    _put_lot(s, "8/32408/4", {"assetId": 8, "assetShortDesc": "Office desks (4)", "assetCategory": "47A"},
             BIDBOX["rnm_8_32408_4"]["bidbox"], photos=0)
    yield s
    lot_archive_view.reset()
    readcache.invalidate_all()
    auth_svc.reset_caches()


def test_admin_archive_redirects_to_the_tab(store):
    r = TestClient(app).get("/admin/archive", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin?tab=archive"


def test_rebuilt_page_renders_from_the_archive(store):
    html = TestClient(app).get("/admin/archive/govdeals/5282/3780/2").text
    assert "Lot of Approx. 150 Stacking Chairs" in html
    assert "$1,725.00" in html and "sold" in html
    assert 'name="robots" content="noindex, nofollow"' in html
    assert '/api/archive/govdeals/5282/3780/2/photo/0' in html and '/photo/1' in html
    assert 'class="arc-chart"' in html and "<path class=\"arc-line\"" in html
    assert "University of Pittsburgh" in html
    assert "Not analyzed yet" in html
    assert "&lt;br" not in html and "<br />" not in html.split("Description", 1)[1][:2000]
    assert "webassets.lqdt1.com" not in html          # no hot-linking GovDeals at view time
    assert "r2.dev" not in html                       # never a public R2 URL


def test_reserve_not_met_page(store):
    html = TestClient(app).get("/admin/archive/govdeals/8/32408/4").text
    assert "reserve not met" in html and "Office desks (4)" in html
    assert "No photos were archived" in html


def test_unknown_lot_is_404(store):
    assert TestClient(app).get("/admin/archive/govdeals/1/2/3").status_code == 404
    assert TestClient(app).get("/api/archive/govdeals/1/2/3").status_code == 404
    assert TestClient(app).get("/api/archive/govdeals/5282/3780/2/photo/9").status_code == 404


def test_photo_is_streamed_privately(store):
    r = TestClient(app).get("/api/archive/govdeals/5282/3780/2/photo/0")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"].startswith("private")
    assert r.content[:2] == b"\xff\xd8"


def test_list_api_filters(store):
    c = TestClient(app)
    b = c.get("/api/archive/lots").json()
    assert b["total"] == 2 and b["archived"] == 2 and b["index"] == "store"
    assert b["items"][0]["lot_key"] == "8/32408/4"     # newest close first
    assert b["facets"]["outcome"] == {"sold": 1, "reserve_not_met": 1}
    assert [m["lot_key"] for m in c.get("/api/archive/lots?outcome=sold").json()["items"]] == ["5282/3780/2"]
    assert c.get("/api/archive/lots?q=stacking").json()["total"] == 1
    assert c.get("/api/archive/lots?min_price=1000&max_price=2000").json()["total"] == 1
    assert c.get("/api/archive/lots?category=seating_furniture").json()["total"] == 2
    assert c.get("/api/archive/lots?since=2026-09-10").json()["total"] == 1


def test_lot_json_has_series(store):
    b = TestClient(app).get("/api/archive/govdeals/5282/3780/2").json()
    assert b["summary"]["final_price"] == 1725.0
    assert [p["price"] for p in b["series"]] == [270.0, 460.0, 1725.0]


def test_cached_analysis_panel_and_unavailable_state(store):
    k = (5282, 3780, 2)
    store.put(lot_archive.analysis_key(k), json.dumps({"status": "unavailable", "error": "groq HTTP 429",
                                                       "analyzed_at": "2026-09-29T10:00:00+00:00"}).encode(), "application/json")
    html = TestClient(app).get("/admin/archive/govdeals/5282/3780/2").text
    assert "Analysis unavailable" in html and "groq HTTP 429" in html and "Nothing was guessed" in html

    ok = {"status": "ok", "analyzed_at": "2026-09-29T10:00:00+00:00", "provider": "groq",
          "category": {"llm": "seating_furniture", "confidence": 0.9},
          "identity": {"brand": None, "model": None, "item_type": "Stacking Chair"},
          "quantity": {"value": 150, "source": "title"}, "condition": {"llm": "Used", "govdeals_code": "SD"},
          "deal": {"verdict": "above market", "verdict_label": "above market — sold at 5.75×",
                   "final_per_unit": 11.5, "median_per_unit": 2.0, "p25_per_unit": 1.5, "p75_per_unit": 4.1,
                   "comps": 11, "keywords": ["stacking", "chair"], "excluded_unknown_count": 50},
          "resale": {"method": "llm_estimate", "low": 2250.0, "high": 3750.0, "confidence": "low"},
          "comps": [{"title": "Lot of (8) Stacking Chairs", "price": 10.0, "per_unit": 1.25, "quantity": 8,
                     "closed_at": "2026-08-01", "used": True}]}
    store.put(lot_archive.analysis_key(k), json.dumps(ok).encode(), "application/json")
    html = TestClient(app).get("/admin/archive/govdeals/5282/3780/2").text
    assert "above market" in html and "$2,250.00–$3,750.00" in html and "Auction comps" in html


def test_rerun_endpoint(store, monkeypatch):
    monkeypatch.setattr(lot_archive_view, "rerun_analysis",
                        lambda key: {"lot_key": key, "status": "unavailable", "error": "groq down"})
    r = TestClient(app).post("/api/archive/govdeals/5282/3780/2/analyze")
    assert r.status_code == 200 and r.json()["status"] == "unavailable"


def test_no_store_is_503(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    monkeypatch.delenv("LOT_ARCHIVE_STORE", raising=False)
    monkeypatch.setattr(lot_archive, "store_from_env", lambda: None)
    lot_archive_view.reset()
    readcache.invalidate_all()
    assert TestClient(app).get("/api/archive/lots").status_code == 503
    assert TestClient(app).get("/admin/archive/govdeals/5282/3780/2").status_code == 503
    lot_archive_view.reset()


def test_every_archive_route_is_session_walled(store, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    auth_svc.reset_caches()
    c = TestClient(app)
    r = c.get("/admin/archive/govdeals/5282/3780/2", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    for url in ("/api/archive/lots", "/api/archive/govdeals/5282/3780/2",
                "/api/archive/govdeals/5282/3780/2/photo/0"):
        assert c.get(url).status_code == 401, url
    assert c.post("/api/archive/govdeals/5282/3780/2/analyze").status_code == 401


def test_no_public_template_points_at_the_archive():
    for p in TEMPLATES.rglob("*.html"):
        if p.name in ("archive_lot.html", "index.html"):
            continue
        text = p.read_text()
        assert "/api/archive" not in text and "archive/lots/" not in text, p


def test_admin_has_the_archive_tab(store):
    html = TestClient(app).get("/admin").text
    assert 'data-tab="archive"' in html and 'data-pane="archive"' in html
    assert 'id="arc-list" data-state="loading"' in html
    assert re.search(r'id="arc-form"', html)
