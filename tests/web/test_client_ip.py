"""rate_limit.client_ip: believe CF-Connecting-IP only behind Cloudflare."""
from types import SimpleNamespace

from automation.web import rate_limit
from automation.web.cloudflare_ips import is_cloudflare, is_local

CF_EDGE = "104.16.1.1"          # inside 104.16.0.0/13


def _req(headers: dict, client_host: str | None = "10.0.0.5"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=client_host))


def test_ranges():
    assert is_cloudflare(CF_EDGE)
    assert is_cloudflare("2606:4700::1")
    assert not is_cloudflare("8.8.8.8")
    assert not is_cloudflare("garbage")
    assert is_local("127.0.0.1") and is_local("testclient") and not is_local("8.8.8.8")


def test_header_trusted_when_peer_is_cloudflare():
    r = _req({"cf-connecting-ip": "203.0.113.7", "x-forwarded-for": f"203.0.113.7, {CF_EDGE}"})
    assert rate_limit.client_ip(r) == "203.0.113.7"


def test_header_ignored_when_peer_is_not_cloudflare():
    """A direct-to-origin caller forging the header is keyed by its real address."""
    r = _req({"cf-connecting-ip": "1.1.1.1", "x-forwarded-for": "1.1.1.1, 93.184.216.34"})
    assert rate_limit.client_ip(r) == "93.184.216.34"


def test_no_header_uses_connecting_peer_not_first_hop():
    r = _req({"x-forwarded-for": "9.9.9.9, 93.184.216.34"})
    assert rate_limit.client_ip(r) == "93.184.216.34"


def test_local_peer_still_honours_header():
    """pytest / a dev box: the test client names an IP and gets it back."""
    assert rate_limit.client_ip(_req({"cf-connecting-ip": "203.0.113.9"}, "testclient")) == "203.0.113.9"
    assert rate_limit.client_ip(_req({"cf-connecting-ip": "203.0.113.9"}, "127.0.0.1")) == "203.0.113.9"


def test_nothing_known():
    assert rate_limit.client_ip(_req({}, None)) == "unknown"
