"""Channel state model. One row per lot × channel lives in `listing_channels`.

Feed channels are pull-based CSV routes (Commerce Manager / Merchant Center fetch
them) so their "sync" is bookkeeping only. Push channels have adapters that
create/update/delete a listing on the far side.

`inventory` stays the single source of truth; this package only records *where*
each lot currently is and what the sync loop should do next. DDL of record:
`scripts/sql/011_listing_channels.sql` (never created at runtime).
"""
from __future__ import annotations

CHANNELS: tuple[str, ...] = ("site", "fb_catalog", "google", "ebay", "fb_marketplace", "craigslist")
FEED_CHANNELS: tuple[str, ...] = ("site", "fb_catalog", "google")
PUSH_CHANNELS: tuple[str, ...] = ("ebay", "fb_marketplace", "craigslist")
# Push channels whose (re)list must be approved by the operator in admin.
# FB Marketplace ships here — and its switch ships OFF — because Renew/Relist
# are the spam triggers that already shadowbanned the family account (D2).
APPROVAL_CHANNELS: frozenset[str] = frozenset({"fb_marketplace"})
# Push channels driven through a real Chrome profile. These are paced by
# `browser_channel_daily_cap` / `browser_channel_spacing_s` in site_settings.
BROWSER_CHANNELS: frozenset[str] = frozenset({"fb_marketplace", "craigslist"})
STATES: tuple[str, ...] = ("off", "queued", "pending_approval", "live", "delisted", "error")
