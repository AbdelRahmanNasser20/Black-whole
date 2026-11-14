"""Unit tests for automation.indexnow (no network: httpx.post is monkeypatched)."""
from __future__ import annotations

import httpx
import pytest

from automation import indexnow

KEY = "0123456789abcdef0123456789abcdef"
BASE = indexnow.PUBLIC_BASE_URL


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _Posts(list):
    status = 200  # what the fake endpoint answers


@pytest.fixture
def posts(monkeypatch):
    calls = _Posts()

    def fake_post(url, **kw):
        calls.append((url, kw))
        return _Resp(calls.status)

    monkeypatch.setattr(indexnow.httpx, "post", fake_post)
    return calls


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", KEY)


def test_unset_key_is_a_noop(monkeypatch, posts):
    monkeypatch.delenv("INDEXNOW_KEY", raising=False)
    assert indexnow.key() == ""
    assert indexnow.submit(["/"]) == (False, "disabled")
    assert indexnow.submit_async(["/"]) is None
    assert posts == []


@pytest.mark.parametrize("bad", ["abc", "bad key!"])
def test_malformed_key_counts_as_unset(monkeypatch, posts, bad):
    monkeypatch.setenv("INDEXNOW_KEY", bad)
    assert indexnow.key() == ""
    assert indexnow.submit(["/"]) == (False, "disabled")
    assert posts == []


def test_payload_endpoint_timeout_absolute_and_deduped(keyed, posts):
    ok, detail = indexnow.submit(["/listings/a", f"{BASE}/listings/a", "/listings", "/listings/a"])
    assert (ok, detail) == (True, "200")
    assert len(posts) == 1
    url, kw = posts[0]
    assert url == indexnow.ENDPOINT == "https://api.indexnow.org/indexnow"
    assert kw["timeout"] == indexnow.TIMEOUT_S
    assert kw["headers"]["Content-Type"] == "application/json; charset=utf-8"
    body = kw["json"]
    assert body["host"] == BASE.split("://", 1)[1]
    assert body["key"] == KEY
    assert body["keyLocation"] == f"{BASE}/{KEY}.txt"
    assert body["urlList"] == [f"{BASE}/listings/a", f"{BASE}/listings"]


def test_drops_foreign_hosts(keyed, posts):
    indexnow.submit(["https://evil.example.com/x", "/listings"])
    assert posts[0][1]["json"]["urlList"] == [f"{BASE}/listings"]
    posts.clear()
    assert indexnow.submit(["https://evil.example.com/x"]) == (False, "no urls")
    assert posts == []


@pytest.mark.parametrize("status", [200, 202])
def test_success_statuses(keyed, posts, status):
    posts.status = status
    assert indexnow.submit(["/"]) == (True, str(status))


@pytest.mark.parametrize("status", [403, 422, 429])
def test_failure_statuses(keyed, posts, status):
    posts.status = status
    assert indexnow.submit(["/"]) == (False, f"HTTP {status}")


def test_network_error_never_raises(keyed, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(indexnow.httpx, "post", boom)
    ok, detail = indexnow.submit(["/"])
    assert ok is False and "ConnectError" in detail


def test_lot_ping_urls_slug_and_city():
    row = {"lot_id": "gd-1-2", "slug": "500-chairs-atlanta-ga", "city": "Atlanta", "state": "GA"}
    assert indexnow.lot_ping_urls(row) == [
        "/listings/500-chairs-atlanta-ga", "/listings", "/chairs/atlanta-ga"]


def test_lot_ping_urls_id_fallback():
    row = {"lot_id": "gd-1-2", "slug": None, "city": "Boise", "state": "ID"}
    assert indexnow.lot_ping_urls(row) == ["/listings/gd-1-2", "/listings", "/chairs/boise-id"]


def test_lot_ping_urls_no_city():
    assert indexnow.lot_ping_urls({"lot_id": "gd-1-2", "state": "GA"}) == ["/listings/gd-1-2", "/listings"]


def test_submit_async_runs_in_a_thread(keyed, posts):
    t = indexnow.submit_async(["/listings"])
    assert t is not None
    t.join(timeout=5)
    assert not t.is_alive()
    assert len(posts) == 1
