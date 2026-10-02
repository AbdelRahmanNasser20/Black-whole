"""AllSurplus (maestro businessId "GI") — offline, from fixtures captured
read-only 2026-10-02 (tests/recorder/fixtures/allsurplus/, seller contact
redacted). No network, no DB."""
import copy
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from deals import sites
from deals.adapters import govdeals as deals_gd
from deals.mapping import asset_to_lot
from recorder import cli, lot_archive, lot_analysis
from recorder.sources import allsurplus, govdeals

FIX = Path(__file__).parent / "fixtures" / "allsurplus"
SEARCH = json.loads((FIX / "search_gi_page1.json").read_text())["assetSearchResults"]
DETAIL = json.loads((FIX / "detail_gi.json").read_text())
BIDBOX = json.loads((FIX / "bidbox_gi.json").read_text())
GD_RAW = json.loads((Path(__file__).parent / "fixtures" / "govdeals" / "lot_raw_examples.json").read_text())
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(govdeals, "BIDBOX_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(deals_gd._g, "_resolve_maestro_key", lambda: "k")


# --- deals adapter -----------------------------------------------------------------

GD_BODY_BEFORE = {"categoryIds": "372", "searchText": "chairs", "isQAL": False, "page": 3,
                  "displayRows": 120, "sortField": "auctionclose", "sortOrder": "asc",
                  "requestType": "search", "responseStyle": "fullResponse", "facets": [],
                  "facetsFilter": ""}


def test_govdeals_search_body_is_byte_identical():
    body = deals_gd.GovDealsAdapter()._search_body("372", "chairs", 3)
    assert json.dumps(body) == json.dumps(GD_BODY_BEFORE)
    assert "businessId" not in body


def test_allsurplus_body_detail_and_bidbox_use_gi(monkeypatch):
    a = deals_gd.GovDealsAdapter(business_id="GI")
    assert a._search_body("", "", 1)["businessId"] == "GI"
    assert a._headers()["Origin"] == "https://www.allsurplus.com"
    seen = []

    class R:
        status_code, content = 200, b"{}"
        def raise_for_status(self): pass
        def json(self): return {"assetSearchResults": []}

    monkeypatch.setattr(deals_gd.requests, "post", lambda url, json=None, **k: seen.append((url, json)) or R())
    monkeypatch.setattr(deals_gd.requests, "get", lambda url, **k: seen.append((url, None)) or R())
    a.fetch_detail(257, 20948)
    a.fetch_bid_state(257, 20948, 13)
    a._search_page("", "", 1)
    assert seen[0] == (f"{deals_gd._g.MAESTRO_URL}/assets/257/20948/false", {"businessId": "GI", "siteId": 1})
    assert seen[1][0].endswith("/bids/bidbox/GI/257/20948/13")
    assert seen[2][1]["businessId"] == "GI"
    seen.clear()
    deals_gd.GovDealsAdapter().fetch_detail(1, 2)
    assert seen[0][1] == {"businessId": "GD", "siteId": 1}


def test_site_spec_is_registered_but_off():
    spec = sites.SITES["allsurplus"]
    assert spec.enabled is False and "allsurplus" not in sites.enabled_sites()
    assert sites.lot_url({"site": "allsurplus", "asset_id": 257, "account_id": 20948}) == \
        "https://www.allsurplus.com/en/asset/257/20948"
    assert sites.get_adapter("allsurplus").business_id == "GI"


# --- recorder routing ----------------------------------------------------------------

def test_gi_assets_route_to_allsurplus_ad_stays_govdeals():
    gi = govdeals._lot_to_observation(asset_to_lot(SEARCH[0]))
    assert gi.source == "allsurplus" and gi.raw["businessId"] == "GI"
    assert gi.raw["currencyCode"] == "EUR"
    ad = copy.deepcopy(GD_RAW[0]); ad["businessId"] = "AD"
    assert govdeals._lot_to_observation(asset_to_lot(ad)).source == "govdeals"


def test_govdeals_sweep_stores_zero_gi_under_govdeals(monkeypatch):
    gd_lots = [asset_to_lot(r) for r in GD_RAW]
    gi_lots = [asset_to_lot(r) for r in SEARCH]

    def fake_discover(self, *, category_ids="", search_text="", max_pages=60, end_before=None):
        return iter(gd_lots + gi_lots)

    monkeypatch.setattr(govdeals.GovDealsAdapter, "discover", fake_discover)
    obs = govdeals.GovDealsSource().discover(scope_override=govdeals.SCOPE_ALL)
    gi_ids = {f"{l.asset_id}/{l.account_id}/{l.auction_id}" for l in gi_lots}
    assert not [o for o in obs if o.source == "govdeals" and o.source_lot_id in gi_ids]
    assert {o.source for o in obs if o.source_lot_id in gi_ids} == {"allsurplus"}


def test_allsurplus_discover_sweeps_gi_and_drops_anything_else(monkeypatch):
    seen = {}

    def fake_discover(self, *, category_ids="", search_text="", max_pages=60, end_before=None):
        seen["business_id"], seen["max_pages"] = self.business_id, max_pages
        return iter([asset_to_lot(r) for r in SEARCH] + [asset_to_lot(GD_RAW[0])])

    monkeypatch.setattr(govdeals.GovDealsAdapter, "discover", fake_discover)
    obs = allsurplus.AllSurplusSource().discover()
    assert seen == {"business_id": "GI", "max_pages": 40}
    assert len(obs) == 3 and {o.source for o in obs} == {"allsurplus"}


def test_allsurplus_poll_reads_the_gi_bidbox_and_records_allsurplus(monkeypatch):
    final = dict(BIDBOX, assetStatusCd="SOA", bidCount=3, currentBid=2750.0,
                 assetAuctionEndDateUTC="2026-10-01T09:00:49Z")
    calls = []

    def fake_bidbox(self, a, b, c):
        calls.append((self.business_id, a, b, c))
        return final

    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_bid_state", fake_bidbox)
    obs = allsurplus.AllSurplusSource().poll([{"source_lot_id": "257/20948/13",
                                               "end_date": NOW - timedelta(days=1)}])
    assert calls == [("GI", 257, 20948, 13)]
    assert len(obs) == 1 and obs[0].source == "allsurplus" and obs[0].status == "closed"
    assert obs[0].raw["recorder_capture"]["outcome"] == "sold"
    assert obs[0].raw["recorder_capture"]["url"].startswith("allsurplus-maestro-bidbox/")


def test_cli_knows_allsurplus():
    assert "allsurplus" in cli.SOURCE_NAMES
    assert isinstance(cli.build_registry()["allsurplus"], allsurplus.AllSurplusSource)
    a = cli.build_parser().parse_args(["archive-backfill", "--source", "allsurplus"])
    assert a.source == "allsurplus"


# --- lot archive per source ----------------------------------------------------------

def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(buf, format="JPEG")
    return buf.getvalue()


class _Adapter:
    business_id = "GI"

    def fetch_bid_state(self, a, b, c):
        return dict(BIDBOX, assetStatusCd="SOA", bidCount=3, currentBid=2750.0,
                    assetAuctionEndDateUTC="2026-10-01T09:00:49Z")

    def fetch_detail(self, a, b):
        return copy.deepcopy(DETAIL)


class _R:
    status_code, content = 200, _jpeg()


def test_archive_writes_under_the_allsurplus_prefix(tmp_path):
    store = lot_archive.LocalStore(tmp_path)
    res = lot_archive.archive_lot((257, 20948, 13), store=store, adapter=_Adapter(),
                                  http_get=lambda url, timeout=30: _R(),
                                  timeline_fn=lambda lk, source: ([], SEARCH[0]),
                                  max_photos=2, now=NOW, source="allsurplus")
    assert res.result == "archived"
    assert store.exists("archive/lots/allsurplus/257_20948_13.json.gz")
    assert store.exists("archive/lots/allsurplus/257_20948_13/0.jpg")
    assert not store.exists("archive/lots/govdeals/257_20948_13.json.gz")
    doc = lot_archive.load(store, "257/20948/13", "allsurplus")
    assert doc["source"] == "allsurplus" and doc["summary"]["currency"] == "EUR"
    meta = json.loads(store.get(lot_archive.meta_key((257, 20948, 13), "allsurplus")))
    assert meta["source"] == "allsurplus" and meta["currency"] == "EUR"
    assert lot_archive.archived_slugs(store, "allsurplus") == {"257_20948_13"}
    assert lot_archive.archived_slugs(store) == set()


def test_unknown_archive_source_is_refused():
    with pytest.raises(ValueError):
        lot_archive.doc_key((1, 2, 3), "mibid")


def test_non_usd_lot_is_never_compared_with_usd_comps():
    doc = {"lot_key": "257/20948/13", "source": "allsurplus",
           "summary": {"title": "Anton Paar HPA-2 High Pressure Asher", "final_price": 2750.0,
                       "currency": "EUR", "outcome": "sold"}}
    from deals.llm_steps import LotIdentity
    called = []
    a = lot_analysis.analyze(
        doc, identity_fn=lambda lot: LotIdentity(brand="Anton Paar", model="HPA-2", item_type="asher",
                                                 quantity=1, condition=None, queries=["anton paar"],
                                                 est_resale_per_unit=None),
        classify_fn=lambda t, d: ("lab_test_equipment", 0.9),
        comps_fn=lambda plan, key: called.append(1) or [],
        judge_fn=lambda i, c: [])
    assert called == [] and "EUR" in a["comps_note"]
    assert a["deal"]["verdict"] is None


def test_fetch_detail_204_is_empty_dict_and_stays_an_unverified_gone(monkeypatch):
    class R204:
        status_code, content = 204, b""
        def raise_for_status(self): pass
        def json(self): raise AssertionError("must not parse an empty body")

    monkeypatch.setattr(deals_gd.requests, "post", lambda *a, **k: R204())
    a = deals_gd.GovDealsAdapter()
    assert a.fetch_detail(1, 2) == {} and a.last_detail_status == 204
    verdict, payload = govdeals._corroborate_absence(a, 1, 2)
    assert verdict == "gone_unverified" and payload is None   # batch guard still applies


def test_index_upsert_writes_currency_only_when_the_column_exists(monkeypatch):
    from recorder import store
    sent = []
    monkeypatch.setattr(store.db, "execute", lambda sql, params: sent.append((sql, params)))
    meta = {"source": "allsurplus", "lot_key": "257/20948/13", "currency": "EUR"}
    for has in (False, True):
        store._lot_archive_cols["currency"] = has
        store.upsert_archive_index(meta)
    store._lot_archive_cols.clear()
    (sql0, p0), (sql1, p1) = sent
    assert "currency" not in sql0 and len(p0) == sql0.count("%s") == 16
    assert "currency = EXCLUDED.currency" in sql1 and p1[-1] == "EUR" and len(p1) == sql1.count("%s") == 17
