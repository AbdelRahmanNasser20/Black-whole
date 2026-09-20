"""Phase 1.1 — channel constants. Every channel is either a feed (pull CSV) or a
push (adapter) channel, never both; FB Marketplace is the one that needs operator
approval; the state vocabulary is fixed (the DB CHECK in 011 mirrors it)."""
from automation import channels


def test_channel_sets_partition():
    assert set(channels.FEED_CHANNELS) | set(channels.PUSH_CHANNELS) == set(channels.CHANNELS)
    assert not set(channels.FEED_CHANNELS) & set(channels.PUSH_CHANNELS)


def test_fb_marketplace_needs_approval():
    assert "fb_marketplace" in channels.APPROVAL_CHANNELS
    assert "ebay" not in channels.APPROVAL_CHANNELS


def test_states():
    assert channels.STATES == ("off", "queued", "pending_approval", "live", "delisted", "error")


def test_browser_channels_are_push_channels():
    assert set(channels.BROWSER_CHANNELS) <= set(channels.PUSH_CHANNELS)
    assert "fb_marketplace" in channels.BROWSER_CHANNELS
