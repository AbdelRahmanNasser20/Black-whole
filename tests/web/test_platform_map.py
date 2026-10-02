"""The lot → buyers-in-radius map in the Buyers section of the GET /platform window
(templates/_platform_map.html + static/site/platform/map.*).

What this guards:
  · the map renders inside the Buyers pane and labels both layers as sample in the server HTML;
  · it shows sample lots and sample buyers only — real auctions have their own map (auctions.js);
  · map.sample.json is coordinates only, city-level, and agrees with the sample sections (same ids);
  · the map's own files stay inside their lane: pf-map- classes, token colours, no writes.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import public_deals
from automation.web.app import app

WEB = Path("automation/web")
SAMPLE_DIR = WEB / "static/site/platform"
PARTIAL = (WEB / "templates/_platform_map.html").read_text()
JS = (SAMPLE_DIR / "map.js").read_text()
CSS = (SAMPLE_DIR / "map.css").read_text()
GEO = json.loads((SAMPLE_DIR / "map.sample.json").read_text())

@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _client():
    return TestClient(app, base_url="https://testserver")


def _section() -> str:
    r = _client().get("/platform")
    assert r.status_code == 200
    pane = r.text.split('id="pf-pane-buyers"', 1)[1].split('role="tabpanel"', 1)[0]
    return pane.split('<div class="pf-map-shell"', 1)[1]


# ───────────────────────── the section ─────────────────────────

def test_map_renders_inside_the_buyers_section(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("rendering the page must not query auctions")
    monkeypatch.setattr(public_deals, "fetch_pins", boom)
    html = _client().get("/platform").text
    assert html.count("data-pf-map") == 1
    assert "/static/site/platform/map.js" in html and "/static/site/platform/map.css" in html
    sec = _section()
    assert "data-pf-map" in sec and 'id="pf-map-match"' in sec
    for layer in ("lots", "buyers"):
        assert f'data-layer="{layer}"' in sec
    assert 'data-layer="auctions"' not in sec
    for mi in ("150", "300", "500"):
        assert f'data-mi="{mi}"' in sec


def test_sample_layers_are_labelled_in_the_html():
    sec = _section()
    for layer in ("lots", "buyers"):
        btn = sec.split(f'data-layer="{layer}"', 1)[1].split("</button>", 1)[0]
        assert "sample" in btn, layer
    # the page test counts the window's panes by this attribute — the map must not add to it
    assert "data-source=" not in PARTIAL


def test_few_words():
    title = re.search(r'<h3 class="pf-map-title">(.*?)</h3>', PARTIAL, re.S).group(1)
    assert 1 <= len(title.split()) <= 4
    body = re.sub(r"\{#.*?#\}", "", PARTIAL, flags=re.S)
    for p in re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S):
        assert len(re.sub(r"<[^>]+>", " ", p).split()) <= 12, "no paragraphs in the map section"


def test_every_class_is_prefixed():
    body = re.sub(r"\{#.*?#\}", "", PARTIAL, flags=re.S)
    for attr in re.findall(r'class="([^"]+)"', body):
        for name in attr.split():
            assert name.startswith("pf-map") or name in ("is-on",), name
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    for name in re.findall(r"(?<![\w-])\.([a-zA-Z][\w-]*)", css):
        assert name.startswith("pf-map") or name.startswith("is-"), name


def test_css_uses_tokens_only():
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "use a token"
    assert not re.search(r"\brgba?\(", css), "use a token"


# ───────────────────────── the sample coordinates ─────────────────────────

def test_map_sample_is_served_and_flagged():
    r = _client().get("/static/site/platform/map.sample.json")
    assert r.status_code == 200 and r.json()["sample"] is True


def test_map_sample_agrees_with_the_tabs():
    inv = json.loads((SAMPLE_DIR / "inventory.sample.json").read_text())
    crm = json.loads((SAMPLE_DIR / "crm.sample.json").read_text())
    assert {g["lot_id"] for g in GEO["lots"]} == {l["lot_id"] for l in inv["lots"]}
    assert {g["id"] for g in GEO["buyers"]} == {b["id"] for b in crm["buyers"]}


def test_map_sample_is_coordinates_only_and_city_level():
    for g in GEO["lots"]:
        assert set(g) == {"lot_id", "lat", "lng"}, g
    for g in GEO["buyers"]:
        assert set(g) == {"id", "city", "state", "lat", "lng"}, g
        assert re.fullmatch(r"[A-Z]{2}", g["state"])
    for g in GEO["lots"] + GEO["buyers"]:
        # a city centre to two decimals (~0.7 mi) — never a street-level fix
        assert round(g["lat"], 2) == g["lat"] and round(g["lng"], 2) == g["lng"], g
        assert 24 < g["lat"] < 50 and -125 < g["lng"] < -66, g
    raw = (SAMPLE_DIR / "map.sample.json").read_text().replace(GEO["_note"], "")
    assert "@" not in raw and "http" not in raw.lower()
    for banned in ("address", "storage", "zip", "phone", "street"):
        assert banned not in raw.lower(), banned


# ───────────────────────── the script ─────────────────────────

def test_script_reads_only_the_sample_files():
    assert not re.search(r"['\"`]/api/", JS), "never the auth-walled admin API"
    assert "/deals/api/" not in JS and "/platform/api/" not in JS, "real auctions live in auctions.js"
    assert "fetch(" not in JS, "go through ui/state.js api()"
    for verb in ("POST", "PATCH", "PUT", "DELETE"):
        assert verb not in JS
    assert "localStorage" not in JS and "sessionStorage" not in JS
    for name in ("inventory.sample.json", "crm.sample.json", "map.sample.json"):
        assert name in JS


def test_map_starts_when_the_buyers_section_comes_on_screen():
    assert "IntersectionObserver" in JS
    start = JS.split("async function start(el)", 1)[1].split("\n}\n", 1)[0]
    assert "drawSample()" in start and "fitAll()" in start
    assert "'Click a lot to see buyers in range.'" in JS


def test_script_reuses_the_storefront_map_library():
    assert "/static/admin_map.js" in JS
    assert "cdnjs" not in JS and "unpkg" not in JS
    assert not re.search(r"#[0-9a-fA-F]{6}\b", JS), "colours come from map.css tokens"
