"""deals/distress.py — offline. Fixtures are real CourtListener v4 / laborcurrent
responses recorded 2026-10-09 (anonymous, a handful of calls, 3 s apart)."""
import json
from datetime import date
from pathlib import Path

import pytest

from deals import distress as d

FIX = Path(__file__).parent / "fixtures" / "distress"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def hits():
    return _load("cl_search_p1.json")["results"] + _load("cl_search_p2.json")["results"]


def _by_name(hits, prefix):
    return next(h for h in hits if d.clean_name(h["caseName"]).startswith(prefix))


# ── org filter ──────────────────────────────────────────────────────────────

def test_org_filter_keeps_businesses_and_churches(hits):
    for name in ("Janam Taccoa Lodging LLC", "American Hospitality Properties REIT, Inc.",
                 "Great Atlantic Grill, Inc", "Rising Hope Church", "Southbridge Associates LLC",
                 "The Cathedral of New Faith Christian Church", "McAuley & Company LLC"):
        assert d.is_org(_by_name(hits, name)), name


def test_org_filter_drops_people_named_church(hits):
    for name in ("Karen Renee Church", "Britanie Dawn Church", "Michael John Ayars, III",
                 "GABRIEL BARTOLO MENDOZA"):
        assert not d.is_org(_by_name(hits, name)), name


def test_clean_name_strips_jointly_administered_html(hits):
    h = _by_name(hits, "True Food Kitchen Holdings")
    name = d.clean_name(h["caseName"])
    assert "<" not in name and "Jointly" not in name and name == "True Food Kitchen Holdings, LLC"


# ── industry tag ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,parties,naics,tag", [
    ("Janam Taccoa Lodging LLC", None, None, "hotel"),
    ("American Hospitality Properties REIT, Inc.", None, None, "hotel"),
    ("Silver Spur Resort LP", None, None, "resort"),
    ("Shin Mi Catering Inc", None, None, "catering"),
    ("RAMH Entertainment LLC", None, None, "event_venue"),
    ("ABC Party Rentals LLC", None, None, "party_rental"),
    ("Rising Hope Church", None, None, "church"),
    ("BAM BAM BBQ, LLC", None, None, "restaurant"),
    ("Holdco 4 LLC", ["Hilton Garden Inn Tucson"], None, "hotel"),
    ("Holdco 4 LLC", None, "721110", "hotel"),
    ("Zeta Grill LLC", None, "8131", "church"),          # specific NAICS wins over keywords
    ("Red Mountain Commercial LLC", None, None, "other"),
])
def test_industry_tag(name, parties, naics, tag):
    assert d.industry_tag(name, parties, naics) == tag


def test_industry_tag_ignores_doc_descriptions(hits):
    # "Lodging Proposed Order" is a court form, not a hotel.
    row = d.hit_to_row(_by_name(hits, "Red Mountain Commercial"))
    assert row["industry_tag"] == "other"


# ── NAICS / ZIP regex ───────────────────────────────────────────────────────

def test_naics_from_petition_text():
    text = ("7. Describe debtor's business ... C. NAICS (North American Industry Classification "
            "System) 4-digit code that best describes debtor. See http://www.uscourts.gov/four-"
            "digit-national-association-naics-codes .\n 7211")
    assert d.naics_from_text(text) == "7211"
    assert d.naics_from_text("NAICS code: 722320") == "722320"
    assert d.naics_from_text("no code here") is None
    assert d.naics_from_text(None) is None


def test_zip_from_text():
    assert d.zip_from_text("Phoenix AZ ZIP Code 85054-1234") == "85054"
    assert d.zip_from_text("nothing") is None


# ── hit → row / WARN → row ──────────────────────────────────────────────────

def test_hit_to_row_shape(hits):
    row = d.hit_to_row(_by_name(hits, "Janam Taccoa Lodging"))
    assert row["source"] == "courtlistener" and row["source_key"] == str(row["docket_id"])
    assert row["court_id"] == "ganb" and row["state"] == "GA"
    assert row["docket_number"] == "26-63515" and row["chapter"] == "11"
    assert row["date_filed"] == date(2026, 10, 5)
    assert row["industry_tag"] == "hotel" and row["debtor_type"] == "org"
    assert set(row) == set(d.COLUMNS)
    assert len(json.dumps(row["raw"], default=str)) <= 8000
    assert all("snippet" not in doc for doc in row["raw"].get("recap_documents", []))


def test_ch7_trustee_carried(hits):
    row = d.hit_to_row(_by_name(hits, "Great Atlantic Grill"))
    assert row["chapter"] == "7" and row["trustee"] == "King, Donald F."


def test_warn_to_row():
    rec = _load("warn_records.json")["records"][0]
    row = d.warn_to_row(rec)
    assert row["source"] == "warn" and row["source_key"] == rec["id"]
    assert row["docket_id"] is None and row["chapter"] is None
    assert row["state"] == "WA" and row["employees_affected"] == 159
    assert row["industry_tag"] == "resort"   # casino
    assert row["date_filed"] == date(2026, 9, 28)
    assert set(row) == set(d.COLUMNS)


def test_sale_notice_from_docs_picks_earliest():
    docs = [{"description": "Order granting motion", "entry_date_filed": "2026-10-01"},
            {"description": "Motion to Sell Property Free and Clear under 363", "entry_date_filed": "2026-10-03",
             "absolute_url": "/docket/1/2/x/"},
            {"short_description": "Notice of Sale", "entry_date_filed": "2026-10-02"}]
    at, url = d.sale_notice_from_docs(docs, "https://cl/docket/1/")
    assert at == date(2026, 10, 2) and url == "https://cl/docket/1/"
    assert d.sale_notice_from_docs([{"description": "Lodging Proposed Order"}]) == (None, None)


# ── upsert SQL shape ────────────────────────────────────────────────────────

def test_upsert_sql_shape():
    sql = d.upsert_sql()
    assert sql.startswith("INSERT INTO distress_cases (")
    assert "ON CONFLICT (source, source_key) DO UPDATE SET" in sql
    assert sql.count("%s") == len(d.COLUMNS)
    assert "first_seen_at" not in sql                 # never rewritten
    assert "sale_noticed_at=COALESCE(distress_cases.sale_noticed_at, EXCLUDED.sale_noticed_at)" in sql
    assert "source=EXCLUDED" not in sql and "source_key=EXCLUDED" not in sql
    assert "last_seen_at=now()" in sql and "RETURNING id, (xmax = 0) AS inserted" in sql


def test_row_values_json_encodes(hits):
    vals = d.row_values(d.hit_to_row(_by_name(hits, "Janam")))
    i = d.COLUMNS.index("parties")
    assert isinstance(vals[i], str) and json.loads(vals[i])


# ── HTTP: 429 backoff, 401 = token needed ───────────────────────────────────

class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers, self.content = status, body or {}, headers or {}, b""

    def json(self):
        return self._body


class _HTTP:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


def test_429_honours_retry_after_then_succeeds():
    slept = []
    http = _HTTP([_Resp(429, headers={"Retry-After": "7"}), _Resp(200, {"results": [], "next": None})])
    c = d.CourtListenerClient(http=http, sleep=slept.append, cache=False, spacing_s=0)
    assert c.get_json("https://x/search/") == {"results": [], "next": None}
    assert slept and slept[0] >= 7 and c.calls == 2


def test_429_gives_up_and_raises_never_empty():
    http = _HTTP([_Resp(429, headers={"Retry-After": "3"})] * (d.MAX_RETRIES + 1))
    c = d.CourtListenerClient(http=http, sleep=lambda s: None, cache=False, spacing_s=0)
    with pytest.raises(d.DistressUnavailable):
        c.get_json("https://x/search/")
    assert c.calls == d.MAX_RETRIES + 1


def test_backoff_grows():
    slept = []
    http = _HTTP([_Resp(429), _Resp(429), _Resp(200, {"ok": 1})])
    c = d.CourtListenerClient(http=http, sleep=slept.append, cache=False, spacing_s=0)
    c.get_json("https://x/")
    assert slept[1] > slept[0]


class _TimeoutHTTP(_HTTP):
    def get(self, url, **kw):
        self.calls.append(url)
        if self.responses:
            return self.responses.pop(0)
        raise TimeoutError("read timed out")


def test_transport_error_retries_then_raises_unavailable():
    c = d.CourtListenerClient(http=_TimeoutHTTP([]), sleep=lambda s: None, cache=False, spacing_s=0)
    with pytest.raises(d.DistressUnavailable, match="transport"):
        c.get_json("https://x/search/")
    assert c.calls == d.MAX_RETRIES + 1


def test_401_needs_token():
    http = _HTTP([_Resp(401, _load("cl_docket_entries_401.json"))])
    c = d.CourtListenerClient(http=http, sleep=lambda s: None, cache=False, spacing_s=0)
    with pytest.raises(d.DistressUnavailable, match="COURTLISTENER_TOKEN"):
        c.get_json("https://x/docket-entries/")


def test_token_header_only_when_set(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_TOKEN", raising=False)
    assert "Authorization" not in d.CourtListenerClient(http=object(), cache=False).headers
    assert d.CourtListenerClient("abc", http=object(), cache=False).headers["Authorization"] == "Token abc"


def test_docket_entries_refuses_anonymous(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_TOKEN", raising=False)
    c = d.CourtListenerClient(http=_HTTP([]), cache=False)
    with pytest.raises(d.DistressUnavailable):
        c.docket_entries(1)


def test_anonymous_spacing_is_polite(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_TOKEN", raising=False)
    assert d.CourtListenerClient(http=object(), cache=False).spacing_s >= 3.0


# ── WARN budget ─────────────────────────────────────────────────────────────

def test_warn_budget_stops_before_cap(tmp_path):
    b = d.WarnBudget(tmp_path / "b.json", max_per_day=2)
    page = _load("warn_records.json")
    http = _HTTP([_Resp(200, dict(page, total=10_000))] * 5)
    with pytest.raises(d.DistressUnavailable, match="budget"):
        d.fetch_warn("2026-09-01", http=http, budget=b, max_calls=10)
    assert len(http.calls) == 2 and b.calls_today() == 2


def test_warn_fetch_paginates_and_filters_industries(tmp_path):
    page = _load("warn_records.json")
    http = _HTTP([_Resp(200, dict(page, total=len(page["records"])))] * 2)
    recs = d.fetch_warn("2026-09-01", http=http, budget=d.WarnBudget(tmp_path / "b.json"))
    assert len(recs) == 2 * len(page["records"])
    assert {c[1]["industry"] for c in http.calls} == set(d.WARN_INDUSTRIES)


# ── run_sync dry-run: no DB, no Telegram ────────────────────────────────────

class _FakeClient:
    token = None
    calls = 0

    def __init__(self, pages):
        self.pages = pages

    def search(self, query, *, filed_after=None, max_pages=5):
        self.calls += 1
        if query == d.SALE_QUERY:
            return iter([])
        return iter(h for p in self.pages for h in p["results"])


def test_run_sync_dry_run_never_touches_db(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "upsert_rows", lambda rows: pytest.fail("dry-run wrote"))
    monkeypatch.setattr(d, "send_alert", lambda *a: pytest.fail("dry-run alerted"))
    lines = []
    rep = d.run_sync(since=date(2026, 9, 25), dry_run=True, warn=False, petitions=False,
                     client=_FakeClient([_load("cl_search_p1.json"), _load("cl_search_p2.json")]),
                     out=lines.append)
    names = " ".join(lines)
    assert "Janam Taccoa Lodging LLC" in names and "American Hospitality Properties REIT, Inc." in names
    assert "Karen Renee Church" not in names
    assert rep.skipped_persons >= 3 and rep.kept == len(lines)
    assert any(r["case_name"].startswith("Great Atlantic Grill") for r in rep.new_ch7_trustee)


def test_alerts_off_by_default(monkeypatch):
    monkeypatch.delenv("DISTRESS_ALERTS", raising=False)
    assert d.send_alert([{"case_name": "x"}], []) == (False, "DISTRESS_ALERTS off")


def test_format_alert_mentions_trustee():
    msg = d.format_alert([{"case_name": "Silver Spur Resort LP", "state": "TX", "industry_tag": "resort",
                           "trustee": "Jane Doe", "petition_url": "https://cl/x"}], [])
    assert "Silver Spur Resort LP" in msg and "Jane Doe" in msg
