"""BLACKWHOLE-22 cloud-browser / Firecrawl pilot scaffold.

Contract under test: SHIPS OFF and makes ZERO network calls unless BOTH an env
flag and an API key are set. With no config the resolver returns None (local
browser path) and the Firecrawl helper refuses — mirroring the
PUBLICSURPLUS_ALLOW_BROWSER guard that keeps the cloud image from launching
Chromium. All `requests` calls are mocked; nothing hits the network, no account
is required to run this suite.
"""
from __future__ import annotations

import os
from unittest import mock

import pytest

from automation import cloud_browser as cb


# ───────── gates default OFF ─────────

def test_cloud_browser_disabled_with_no_env():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert cb.cloud_browser_enabled() is False


def test_cloud_browser_disabled_when_flag_set_but_no_key():
    with mock.patch.dict(os.environ, {"LISTING_CLOUD_BROWSER": "browserbase"}, clear=True):
        assert cb.cloud_browser_enabled() is False


def test_cloud_browser_enabled_only_with_flag_and_key():
    env = {"LISTING_CLOUD_BROWSER": "browserbase", "BROWSERBASE_API_KEY": "bb_test"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert cb.cloud_browser_enabled() is True


def test_firecrawl_disabled_with_no_env():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert cb.firecrawl_enabled() is False


# ───────── resolve_cloud_cdp_endpoint: OFF => no network, returns None ─────────

def test_resolver_returns_none_and_never_calls_network_when_off():
    with mock.patch.dict(os.environ, {}, clear=True), \
            mock.patch.object(cb.requests, "post") as post:
        assert cb.resolve_cloud_cdp_endpoint() is None
        post.assert_not_called()


def test_resolver_returns_none_when_key_but_no_project_id():
    env = {"LISTING_CLOUD_BROWSER": "browserbase", "BROWSERBASE_API_KEY": "bb_test"}
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(cb.requests, "post") as post:
        assert cb.resolve_cloud_cdp_endpoint() is None
        post.assert_not_called()  # missing project id short-circuits before the call


# ───────── resolve_cloud_cdp_endpoint: ON => returns connectUrl ─────────

def _fake_response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_resolver_returns_connect_url_when_configured():
    env = {
        "LISTING_CLOUD_BROWSER": "browserbase",
        "BROWSERBASE_API_KEY": "bb_test",
        "BROWSERBASE_PROJECT_ID": "proj_123",
    }
    wss = "wss://connect.browserbase.com/session/abc"
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(cb.requests, "post",
                              return_value=_fake_response({"id": "sess_1", "connectUrl": wss})) as post:
        assert cb.resolve_cloud_cdp_endpoint() == wss
        post.assert_called_once()
        # Auth header + project id are sent as Browserbase expects.
        _, kwargs = post.call_args
        assert kwargs["headers"]["X-BB-API-Key"] == "bb_test"
        assert kwargs["json"]["projectId"] == "proj_123"


def test_resolver_swallows_network_error_and_returns_none():
    env = {
        "LISTING_CLOUD_BROWSER": "browserbase",
        "BROWSERBASE_API_KEY": "bb_test",
        "BROWSERBASE_PROJECT_ID": "proj_123",
    }
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(cb.requests, "post", side_effect=RuntimeError("boom")):
        # A cloud failure must degrade to the local path, never raise.
        assert cb.resolve_cloud_cdp_endpoint() is None


def test_resolver_adds_stealth_and_proxies_when_opted_in():
    env = {
        "LISTING_CLOUD_BROWSER": "browserbase",
        "BROWSERBASE_API_KEY": "bb_test",
        "BROWSERBASE_PROJECT_ID": "proj_123",
        "BROWSERBASE_STEALTH": "1",
    }
    wss = "wss://connect.browserbase.com/session/abc"
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(cb.requests, "post",
                              return_value=_fake_response({"connectUrl": wss})) as post:
        cb.resolve_cloud_cdp_endpoint()
        _, kwargs = post.call_args
        assert kwargs["json"]["proxies"] is True
        assert "browserSettings" in kwargs["json"]


# ───────── firecrawl_scrape: OFF => raises, no network ─────────

def test_firecrawl_scrape_raises_when_off_and_never_calls_network():
    with mock.patch.dict(os.environ, {}, clear=True), \
            mock.patch.object(cb.requests, "post") as post:
        with pytest.raises(cb.CloudBrowserNotConfigured):
            cb.firecrawl_scrape("https://example.gov/lot/1")
        post.assert_not_called()


def test_firecrawl_scrape_returns_data_when_configured():
    env = {"FIRECRAWL_ENABLED": "1", "FIRECRAWL_API_KEY": "fc_test"}
    data = {"markdown": "# Lot title", "metadata": {"title": "Lot title"}}
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(cb.requests, "post",
                              return_value=_fake_response({"data": data})) as post:
        out = cb.firecrawl_scrape("https://example.gov/lot/1")
        assert out == data
        _, kwargs = post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer fc_test"
