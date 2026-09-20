"""Phase 1.2 — per-channel switches in site_settings.

Every channel gets a `channel_<c>_enabled` 0/1 switch plus a master switch; the
browser channels (FB Marketplace, Craigslist) and eBay ship OFF by default, the
feed channels ship ON. `channel_enabled()` is the one helper every caller uses,
so "master off" reliably beats "channel on"."""
from automation import channels, site_settings


def test_every_channel_has_a_switch():
    for c in channels.CHANNELS:
        assert f"channel_{c}_enabled" in site_settings.SPEC
    assert "channels_master_enabled" in site_settings.SPEC


def test_defaults_ship_browser_channels_off():
    d = site_settings.defaults()
    assert d["channel_fb_marketplace_enabled"] == 0
    assert d["channel_craigslist_enabled"] == 0
    assert d["channel_ebay_enabled"] == 0
    assert d["channel_site_enabled"] == 1
    assert d["channel_fb_catalog_enabled"] == 1
    assert d["channel_google_enabled"] == 1
    assert d["channels_master_enabled"] == 1


def test_browser_pacing_defaults():
    d = site_settings.defaults()
    assert d["browser_channel_daily_cap"] == 4
    assert d["browser_channel_spacing_s"] == 1200


def test_switch_is_bounded_to_0_or_1():
    import pytest
    with pytest.raises(ValueError):
        site_settings._coerce("channel_site_enabled", 2)
    assert site_settings._coerce("channel_site_enabled", "1") == 1


def test_channel_enabled_respects_master(monkeypatch):
    monkeypatch.setattr(site_settings, "get_all",
        lambda: {**site_settings.defaults(), "channels_master_enabled": 0, "channel_site_enabled": 1})
    assert site_settings.channel_enabled("site") is False


def test_channel_enabled_true_when_both_on(monkeypatch):
    monkeypatch.setattr(site_settings, "get_all",
        lambda: {**site_settings.defaults(), "channels_master_enabled": 1, "channel_ebay_enabled": 1})
    assert site_settings.channel_enabled("ebay") is True
    assert site_settings.channel_enabled("fb_marketplace") is False   # still off by default


def test_channel_enabled_unknown_channel_is_false_never_raises(monkeypatch):
    monkeypatch.setattr(site_settings, "get_all", lambda: site_settings.defaults())
    assert site_settings.channel_enabled("myspace") is False
