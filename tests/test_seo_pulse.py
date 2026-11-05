"""SEO Pulse — audit, GSC loading, recommendations, snapshots, report. No network."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date

import pytest

from automation import seo_pulse as sp

BASE = "https://black-whole.com"


class Resp:
    def __init__(self, text, status=200, url=None):
        self.text, self.status_code, self.url = text, status, url

    def json(self):
        return json.loads(self.text)


class FakeHttp:
    def __init__(self, pages: dict[str, Resp]):
        self.pages, self.calls = pages, []

    def get(self, url):
        self.calls.append(url)
        if url in self.pages:
            r = self.pages[url]
            r.url = r.url or url
            return r
        return Resp("<html><title>nope</title></html>", 404, url)


def page(title, h1="Chairs", desc="Used banquet chairs by the lot, pickup in Atlanta or freight anywhere in the US.",
         links=(), words=350, canonical=None, extra=""):
    body = " ".join(["chair"] * words)
    anchors = "".join(f'<a href="{l}">x</a>' for l in links)
    can = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    return (f"<html><head><title>{title}</title><meta name='description' content='{desc}'>{can}{extra}</head>"
            f"<body><main><h1>{h1}</h1><p>{body}</p>{anchors}<img src='a.jpg' alt='chair'><img src='b.jpg'></main></body></html>")


SITEMAP = f"""<?xml version="1.0"?><urlset><url><loc>{BASE}/</loc></url>
<url><loc>{BASE}/listings</loc></url><url><loc>{BASE}/chairs/atlanta-ga</loc></url>
<url><loc>{BASE}/lonely</loc></url></urlset>"""


@pytest.fixture
def site_http():
    return FakeHttp({
        f"{BASE}/sitemap.xml": Resp(SITEMAP),
        f"{BASE}/": Resp(page("Bulk Banquet Chairs for Sale – Black Whole Liquidation", "Bulk banquet chairs",
                              links=["/listings", "/chairs/atlanta-ga"], canonical=f"{BASE}/")),
        f"{BASE}/listings": Resp(page("Chair Lots for Sale — Bulk & Wholesale | Black Whole", "Chair lots for sale",
                                      links=["/", "/chairs/atlanta-ga"], canonical=f"{BASE}/listings")),
        f"{BASE}/chairs/atlanta-ga": Resp(page("Used Banquet Chairs for Sale in Atlanta, GA | Black Whole",
                                               "Used Banquet Chairs in Atlanta, GA", links=["/"],
                                               canonical=f"{BASE}/chairs/atlanta-ga", words=120)),
        f"{BASE}/lonely": Resp(page("Short", "", desc="", words=50)),
    })


def test_audit_html_flags_the_obvious():
    a = sp.audit_html(f"{BASE}/lonely", page("Short", "", desc="", words=50), base=BASE)
    joined = " ".join(a.issues)
    assert "title short" in joined and "missing meta description" in joined and "no H1" in joined
    assert "no canonical" in joined and "thin" in joined and "1/2 images without alt" in joined
    assert a.word_count >= 50


def test_audit_html_clean_page_has_no_issues():
    html = page("Used Banquet Chairs for Sale in Atlanta, GA | Black Whole", "H1", canonical=f"{BASE}/x",
                extra='<script type="application/ld+json">{"@type":"Product"}</script>')
    a = sp.audit_html(f"{BASE}/x", html.replace("<img src='b.jpg'>", ""), base=BASE)
    assert a.issues == [], a.issues
    assert a.jsonld_types == ["Product"]


def test_audit_html_redirect_and_noindex_and_bad_jsonld():
    html = page("Used Banquet Chairs for Sale in Atlanta, GA | Black Whole", canonical=f"{BASE}/new",
                extra='<meta name="robots" content="noindex"><script type="application/ld+json">{oops</script>')
    a = sp.audit_html(f"{BASE}/old", html, base=BASE, final_url=f"{BASE}/new")
    j = " ".join(a.issues)
    assert "redirects" in j and "noindex" in j and "broken JSON-LD" in j
    assert "canonical points elsewhere" not in j   # canonical == final URL is fine


def test_crawl_orphans_inbound_and_site_issues(site_http):
    site = sp.crawl(BASE, site_http, sleep=lambda s: None)
    by = site.by_path()
    assert by["/"].inbound == 2 and by["/lonely"].inbound == 0
    assert any(i.startswith("orphan") for i in by["/lonely"].issues)
    assert not any(i.startswith("orphan") for i in by["/"].issues)   # root is never an orphan
    assert site_http.calls[0].endswith("/sitemap.xml") and len(site.pages) == 4


def test_crawl_survives_a_dead_page(site_http):
    class Boom(FakeHttp):
        def get(self, url):
            if url.endswith("/listings"):
                raise ConnectionError("down")
            return super().get(url)
    http = Boom(site_http.pages)
    site = sp.crawl(BASE, http, sleep=lambda s: None)
    dead = site.by_path()["/listings"]
    assert dead.status == 0 and dead.issues[0].startswith("fetch failed")


# ── GSC ──

QUERIES_CSV = "Top queries,Clicks,Impressions,CTR,Position\nbulk seating,0,95,0%,63.4\nused banquet chairs atlanta,1,12,8.33%,7.2\nbulk banquet chairs,0,30,0%,12\n"
PAGES_CSV = f"Top pages,Clicks,Impressions,CTR,Position\n{BASE}/,11,127,8.66%,18.5\n{BASE}/listings,1,283,0.35%,53.1\n{BASE}/chairs/atlanta-ga,0,40,0%,9.9\n"


def test_load_gsc_export_from_zip_and_folder(tmp_path):
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Queries.csv", "﻿" + QUERIES_CSV)
        zf.writestr("Pages.csv", PAGES_CSV)
    g = sp.load_gsc_export(z)
    assert g.queries[1] == {"query": "used banquet chairs atlanta", "clicks": 1, "impressions": 12,
                            "ctr": pytest.approx(0.0833), "position": 7.2}
    assert g.totals["impressions"] == 450
    d = tmp_path / "folder"
    d.mkdir()
    (d / "Queries.csv").write_text(QUERIES_CSV)
    assert len(sp.load_gsc_export(d).queries) == 3
    with pytest.raises(ValueError):
        (tmp_path / "empty").mkdir()
        sp.load_gsc_export(tmp_path / "empty")


def test_fetch_gsc_api_builds_rows_from_keys():
    class Sess:
        def __init__(self):
            self.bodies = []

        def post(self, url, json=None, timeout=None):
            self.bodies.append(json)
            dims = json["dimensions"]
            keys = ["bulk seating"] if dims == ["query"] else [f"{BASE}/"] if dims == ["page"] else ["bulk seating", f"{BASE}/listings"]
            return Resp(__import__("json").dumps({"rows": [{"keys": keys, "clicks": 2, "impressions": 50, "ctr": 0.04, "position": 12.5}]}))
    s = Sess()
    g = sp.fetch_gsc_api("https://black-whole.com/", days=30, session=s, today=date(2026, 11, 5))
    assert g.queries[0]["query"] == "bulk seating" and g.pages[0]["page"] == f"{BASE}/"
    assert g.query_pages[0]["page"] == f"{BASE}/listings"
    assert s.bodies[0]["endDate"] == "2026-11-02" and s.bodies[0]["startDate"] == "2026-10-03"


# ── recommendations ──

def _gsc():
    g = sp.GscData(source="test")
    g.queries = sp._rows_from_csv(QUERIES_CSV, "query")
    g.pages = sp._rows_from_csv(PAGES_CSV, "page")
    return g


def test_recommend_striking_gap_ctr_and_page_fix(site_http):
    site = sp.crawl(BASE, site_http, sleep=lambda s: None)
    recs = sp.recommend(site, _gsc())
    kinds = {(r.kind, r.target) for r in recs}
    # "bulk banquet chairs" pos 12, covered by "/" (title has bulk banquet chairs) → striking distance
    assert ("striking_distance", "/") in kinds
    # "bulk seating" pos 63 and no page says "seating" → content gap
    assert ("content_gap", "bulk seating") in kinds
    # "used banquet chairs atlanta" pos 7 → covered by the city page → striking distance on it
    assert ("striking_distance", "/chairs/atlanta-ga") in kinds
    # /listings: 283 impressions, pos 53 → NOT a CTR gap (position > 20); /chairs/atlanta-ga 40 impr pos 9.9 ctr 0 → CTR gap
    assert ("ctr_gap", "/chairs/atlanta-ga") in kinds and ("ctr_gap", "/listings") not in kinds
    assert ("page_fix", "/lonely") in kinds and ("orphan", "/lonely") in kinds
    assert recs == sorted(recs, key=lambda r: -r.priority)


def test_recommend_without_gsc_only_audit_items(site_http):
    site = sp.crawl(BASE, site_http, sleep=lambda s: None)
    recs = sp.recommend(site, None)
    assert {r.kind for r in recs} <= {"page_fix", "orphan", "site"}


def test_query_page_rows_beat_token_matching(site_http):
    site = sp.crawl(BASE, site_http, sleep=lambda s: None)
    g = _gsc()
    g.query_pages = [{"query": "bulk seating", "page": f"{BASE}/listings", "clicks": 0, "impressions": 95, "ctr": 0, "position": 63.4}]
    assert sp._best_page_for("bulk seating", g, site) == "/listings"


def test_keyword_ideas_marks_new_and_dedupes():
    http = FakeHttp({
        sp.AUTOCOMPLETE_URL + "bulk%20banquet%20chairs": Resp(json.dumps(["bulk banquet chairs", ["bulk banquet chairs for sale", "Bulk Seating", "bulk banquet chairs"]])),
        sp.AUTOCOMPLETE_URL + "bulk%20banquet%20chairs%20atlanta": Resp(json.dumps(["x", ["bulk banquet chairs atlanta ga"]])),
    })
    ideas = sp.keyword_ideas(http, seeds=["bulk banquet chairs"], cities=["atlanta"], known=["bulk seating"], sleep=lambda s: None)
    terms = {i["term"]: i["new"] for i in ideas}
    assert terms == {"bulk banquet chairs for sale": True, "Bulk Seating": False, "bulk banquet chairs atlanta ga": True}


# ── drafts ──

def test_draft_copy_fills_top_recs_and_caps(site_http):
    site = sp.crawl(BASE, site_http, sleep=lambda s: None)
    recs = sp.recommend(site, _gsc())

    class Http:
        n = 0

        def post(self, url, json=None, timeout=None, headers=None):
            Http.n += 1
            return Resp(__import__("json").dumps({"choices": [{"message": {"content": __import__("json").dumps(
                {"title": "T", "description": "D", "h1": "H", "faqs": [{"q": "Q", "a": "A"}]})}}]}))
    n = sp.draft_copy(recs, site, http=Http(), max_drafts=2, key="k")
    assert n == 2 and Http.n == 2
    assert [r for r in recs if r.draft][0].draft["title"] == "T"
    assert sp.draft_copy(recs, site, http=Http(), key="") == 0


# ── snapshots / trend / report ──

def test_snapshot_roundtrip_and_movers(tmp_path, site_http):
    site = sp.crawl(BASE, site_http, sleep=lambda s: None)
    g = _gsc()
    old = sp.build_snapshot(site, g, today=date(2026, 10, 1))
    sp.save_snapshot(old, tmp_path)
    g2 = _gsc()
    for r in g2.queries:
        if r["query"] == "bulk seating":
            r["position"] = 40.0
    new = sp.build_snapshot(site, g2, today=date(2026, 11, 5))
    prev = sp.load_previous_snapshot(new["date"], tmp_path)
    assert prev["date"] == "2026-10-01"
    t = sp.diff_snapshots(prev, new)
    assert t["since"] == "2026-10-01" and t["movers"][0]["query"] == "bulk seating" and t["movers"][0]["delta"] == 23.4
    assert sp.diff_snapshots(None, new)["since"] is None


def test_report_is_summary_first(site_http):
    site = sp.crawl(BASE, site_http, sleep=lambda s: None)
    g = _gsc()
    recs = sp.recommend(site, g)
    md = sp.render_report(site, g, recs, [{"term": "church chairs bulk", "from": "x", "new": True}], None, today=date(2026, 11, 5))
    lines = md.splitlines()
    assert lines[0].startswith("# SEO Pulse")
    assert lines[2].startswith("- **Search:** 12 clicks / 450 impressions")
    assert lines[3].startswith("- **Site:** 4/4") and lines[4].startswith("- **Do first:**")
    assert "## Recommendations" in md and "## Site audit" in md and "church chairs bulk" in md


def test_run_end_to_end_writes_report_and_degrades_without_gsc(tmp_path, site_http):
    res = sp.run(base=BASE, http=site_http, gsc_spec=str(tmp_path / "missing.zip"), keywords=False,
                 directory=tmp_path, today=date(2026, 11, 5), sleep=lambda s: None)
    assert res["gsc"] is None and "FileNotFoundError" in res["gsc_error"]
    assert res["report_path"].exists() and res["snapshot_path"].exists()
    assert "Search Console failed" in res["report"]


def test_crawl_retries_a_5xx_once_and_flags_it_intermittent(site_http):
    class Flaky(FakeHttp):
        seen = 0

        def get(self, url):
            if url.endswith("/listings"):
                Flaky.seen += 1
                if Flaky.seen == 1:
                    return Resp("bad gateway", 502, url)
            return super().get(url)
    site = sp.crawl(BASE, Flaky(site_http.pages), sleep=lambda s: None)
    p = site.by_path()["/listings"]
    assert p.status == 200 and p.issues[0] == "intermittent HTTP 502 (second try 200)"
    recs = sp.recommend(site, None)
    assert recs[0].kind == "page_fix" and recs[0].target == "/listings"   # flaky beats cosmetic issues
