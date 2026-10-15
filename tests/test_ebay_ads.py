"""automation/ebay_ads.py with eBay's HTTP mocked (httpx.MockTransport) — no network.

Properties defended:
  1. A 409 / errorId 35078 on the ads read is the `not_eligible` state carrying eBay's
     message — never an exception (the old script crashed here).
  2. apply() refuses to write while not eligible, and only writes the capped suggested rate.
  3. snapshot() calls eBay at most once per TTL, and reports missing keys as not_configured.
"""
from __future__ import annotations

import json

import httpx
import pytest

from automation import ebay_ads

NOT_ELIGIBLE = {"errors": [{"errorId": 35078, "domain": "API_MARKETING", "category": "BUSINESS",
                            "message": "To gain access to Promoted Listings, you must be in good standing "
                                       "with recent sales activity."}]}


def _selling_xml(items):
    its = "".join(f"<Item><ItemID>{i}</ItemID><Title>{t}</Title>"
                  f"<SellingStatus><CurrentPrice>25.0</CurrentPrice></SellingStatus>"
                  f"<QuantityAvailable>3</QuantityAvailable></Item>" for i, t in items)
    return (f'<?xml version="1.0"?><GetMyeBaySellingResponse xmlns="{ebay_ads.NS}"><Ack>Success</Ack>'
            f"<ActiveList><ItemArray>{its}</ItemArray><PaginationResult><TotalNumberOfPages>1"
            f"</TotalNumberOfPages></PaginationResult></ActiveList></GetMyeBaySellingResponse>")


class FakeEbay:
    def __init__(self, *, campaign_status="SYSTEM_PAUSED", ads_status=409, ads=None, sugg=None):
        self.campaign_status, self.ads_status = campaign_status, ads_status
        self.ads = ads or []
        self.sugg = sugg if sugg is not None else {"111111111": 16.0, "222222222": 12.2}
        self.calls: list[tuple[str, str]] = []
        self.writes: list[tuple[str, dict]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        if path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok"})
        if path == "/ws/api.dll":
            return httpx.Response(200, text=_selling_xml([("111111111", "Blue chairs"), ("222222222", "Red chairs")]))
        if path == "/sell/marketing/v1/ad_campaign" and req.method == "GET":
            return httpx.Response(200, json={"campaigns": [{
                "campaignId": "C1", "campaignName": "General", "campaignStatus": self.campaign_status,
                "fundingStrategy": {"fundingModel": "COST_PER_SALE", "bidPercentage": "9.0"}}]})
        if path == "/sell/marketing/v1/ad_campaign/C1/ad":
            if self.ads_status == 409:
                return httpx.Response(409, json=NOT_ELIGIBLE)
            if self.ads_status != 200:
                return httpx.Response(self.ads_status, json={"errors": [{"errorId": 35001, "message": "boom"}]})
            return httpx.Response(200, json={"ads": self.ads})
        if path == "/sell/recommendation/v1/find":
            return httpx.Response(200, json={"listingRecommendations": [
                {"listingId": k, "marketing": {"ad": {"bidPercentages": [{"value": str(v)}]}}}
                for k, v in self.sugg.items()]})
        if req.method == "POST" and path.startswith("/sell/marketing/v1/ad_campaign/C1/bulk_"):
            self.writes.append((path, json.loads(req.content)))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"errors": [{"errorId": 1, "message": "unmocked " + path}]})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "app")
    monkeypatch.setenv("EBAY_CERT_ID", "cert")
    monkeypatch.setenv("EBAY_REFRESH_TOKEN", "refresh")
    monkeypatch.setenv("EBAY_AD_RATE_CAP", "15")
    ebay_ads.reset_cache()
    yield
    ebay_ads.reset_cache()


_RealClient = ebay_ads.EbayClient


def _client(fake):
    return _RealClient(http=httpx.Client(transport=httpx.MockTransport(fake)))


def test_409_is_not_eligible_state_with_ebays_message(env):
    p = ebay_ads.plan(_client(FakeEbay()))
    assert p["state"] == ebay_ads.STATE_NOT_ELIGIBLE
    assert "good standing with recent sales activity" in p["reason"]
    assert p["campaign"] == {"id": "C1", "name": "General", "status": "SYSTEM_PAUSED",
                             "model": "COST_PER_SALE", "rate": 9.0}
    by_id = {r["id"]: r for r in p["rows"]}
    assert by_id["111111111"]["want"] == 15.0          # 16 suggested → capped
    assert by_id["222222222"]["want"] == 12.2
    assert all(r["now"] is None for r in p["rows"])


def test_apply_refuses_when_not_eligible(env):
    fake = FakeEbay()
    c = _client(fake)
    with pytest.raises(RuntimeError, match="not_eligible"):
        ebay_ads.apply(c, ebay_ads.plan(c))
    assert fake.writes == []


def test_apply_writes_capped_rates_when_running(env):
    fake = FakeEbay(campaign_status="RUNNING", ads_status=200,
                    ads=[{"listingId": "222222222", "bidPercentage": "9.0"}])
    c = _client(fake)
    p = ebay_ads.plan(c)
    assert p["state"] == ebay_ads.STATE_RUNNING and p["reason"] is None
    out = ebay_ads.apply(c, p)
    assert out == {"campaign_id": "C1", "updated": 1, "added": 1}
    writes = dict(fake.writes)
    assert writes["/sell/marketing/v1/ad_campaign/C1/bulk_update_ads_bid_by_listing_id"] == {
        "requests": [{"listingId": "222222222", "bidPercentage": "12.2"}]}
    assert writes["/sell/marketing/v1/ad_campaign/C1/bulk_create_ads_by_listing_id"] == {
        "requests": [{"listingId": "111111111", "bidPercentage": "15.0"}]}


def test_apply_skips_rates_already_set(env):
    fake = FakeEbay(campaign_status="RUNNING", ads_status=200,
                    ads=[{"listingId": "111111111", "bidPercentage": "15.0"},
                         {"listingId": "222222222", "bidPercentage": "12.2"}])
    c = _client(fake)
    assert ebay_ads.apply(c, ebay_ads.plan(c)) == {"campaign_id": "C1", "updated": 0, "added": 0}
    assert fake.writes == []


def test_other_ebay_errors_still_raise(env):
    fake = FakeEbay(ads_status=500)
    with pytest.raises(ebay_ads.EbayError):
        ebay_ads.plan(_client(fake))


def test_item_id_from_url():
    assert ebay_ads.item_id_from_url("https://www.ebay.com/itm/336648966527?x=1") == "336648966527"
    assert ebay_ads.item_id_from_url("https://www.ebay.com/itm/some-title/336648966527") == "336648966527"
    assert ebay_ads.item_id_from_url("https://www.ebay.com/sh/lst/active") is None
    assert ebay_ads.item_id_from_url(None) is None


def test_snapshot_caches_the_ebay_read(env, monkeypatch):
    fake = FakeEbay()
    monkeypatch.setattr(ebay_ads, "EbayClient", lambda: _client(fake))
    a = ebay_ads.snapshot(ttl=900)
    n = len(fake.calls)
    b = ebay_ads.snapshot(ttl=900)
    assert b is a and len(fake.calls) == n            # second read = no eBay traffic
    ebay_ads.snapshot(force=True, ttl=900)
    assert len(fake.calls) > n                         # force re-reads
    assert a["state"] == "not_eligible" and a["fetched_at"]


def test_snapshot_not_configured(monkeypatch):
    for v in ("EBAY_APP_ID", "EBAY_CERT_ID", "EBAY_REFRESH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    ebay_ads.reset_cache()
    s = ebay_ads.snapshot(force=True)
    assert s["state"] == ebay_ads.STATE_NOT_CONFIGURED and s["rows"] == []
    ebay_ads.reset_cache()
