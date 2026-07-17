# tests/deals/test_comps.py
import httpx
import pytest
from deals.comps import (Comp, CompsResult, CompsUnavailable,
                         PiCompsProvider, comps_provider_from_env)

def _provider(handler) -> PiCompsProvider:
    p = PiCompsProvider("http://pi.test", "k")
    p._client = httpx.Client(transport=httpx.MockTransport(handler),
                             base_url="http://pi.test")
    return p

def test_fetch_parses_result():
    def handler(request):
        assert request.headers["X-Comps-Key"] == "k"
        assert request.url.params["q"] == "steelcase leap v2"
        return httpx.Response(200, json={"query": "steelcase leap v2", "count": 1,
            "median": 80.0, "mean": 80.0, "cached": True, "fetched_at": 1,
            "items": [{"listing_id": "9", "title": "Leap V2", "price": 80.0,
                       "condition": "Pre-owned", "sold_note": None, "url": "https://ebay.com/itm/9"}]})
    r = _provider(handler).fetch("steelcase leap v2")
    assert isinstance(r, CompsResult) and r.items[0] == Comp("9", "Leap V2", 80.0,
                                                            "Pre-owned", "https://ebay.com/itm/9")

def test_503_raises_unavailable():
    def handler(request):
        return httpx.Response(503, json={"detail": {"error": "challenged"}})
    with pytest.raises(CompsUnavailable):
        _provider(handler).fetch("x chair")

def test_network_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("down")
    with pytest.raises(CompsUnavailable):
        _provider(handler).fetch("x chair")

def test_from_env_none_when_unconfigured():
    assert comps_provider_from_env({}) is None
    assert comps_provider_from_env({"COMPS_URL": "http://x"}) is None

def test_from_env_builds_provider():
    p = comps_provider_from_env({"COMPS_URL": "http://x", "COMPS_KEY": "k"})
    assert isinstance(p, PiCompsProvider)
