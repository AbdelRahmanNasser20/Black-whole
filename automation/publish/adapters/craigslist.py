"""Craigslist publish adapter (BLACKWHOLE-20 / channels Phase 4).

Two faces, one flow (`automation.craigslist.post_listing`, recorded live):

  * `publish(ctx, request)` — the async orchestrator entry point (`run.py
    --platforms cl`), per-city: one post per requested city, copy from the
    normalized `ListingData`.
  * `publish_lot(row)` / `unpublish_lot(row, current)` / `renew_lot(row,
    current)` — the sync-callable entry points the channel sync loop
    (`automation.channels.sync`) and `scripts/craigslist_lot.py` use. They open
    the persistent Chrome profile themselves (never headless) and post the lot
    once, in its own city.

Bookkeeping after a post: `inventory.craigslist_url` + a `listing_channels`
row (`channels.store.upsert`). Both are wrapped — a bookkeeping failure is
logged and swallowed, never thrown over a post that already went out.

Gates live OUTSIDE this module: the sync loop skips the channel while
`channel_craigslist_enabled` is 0 and paces it; the CLI needs
`CRAIGSLIST_LIVE=1`. Nothing here reads `storage_note`.
"""
from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from automation import craigslist, db
from automation.channels import store as channel_store

from ..models import STATUS_DRY_RUN, STATUS_PUBLISHED, PublishRequest, PublishResult
from ..registry import register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import BrowserContext

CHANNEL = "craigslist"


def _log(msg: str) -> None:
    print(f"[craigslist] {msg}", flush=True)


def run_sync(coro_fn: Callable[[], Awaitable[Any]]) -> Any:
    """Run an async flow from sync code. Inside a running event loop (the web
    process calls sync from `asyncio.to_thread`, but a caller may not), hop to
    a fresh thread so `asyncio.run` has a loop of its own."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_fn())
    box: dict[str, Any] = {}

    def _target():
        try:
            box["result"] = asyncio.run(coro_fn())
        except BaseException as e:  # noqa: BLE001 — re-raised below
            box["error"] = e

    t = threading.Thread(target=_target, name="craigslist-post", daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


# ── bookkeeping (never breaks a post) ──────────────────────────────────────

def _record_url(lot_id: str, url: str | None) -> None:
    try:
        db.execute("UPDATE inventory SET craigslist_url = %s, updated_at = now() WHERE lot_id = %s",
                   (url, str(lot_id)))
    except Exception as e:  # noqa: BLE001
        _log(f"inventory.craigslist_url not updated for {lot_id} ({type(e).__name__}: {e})")


def _record_channel(lot_id: str, **kw) -> None:
    try:
        channel_store.upsert(str(lot_id), CHANNEL, **kw)
    except Exception as e:  # noqa: BLE001
        _log(f"listing_channels not updated for {lot_id} ({type(e).__name__}: {e})")


# ── the browser runs ───────────────────────────────────────────────────────

async def _post_live(post: craigslist.LotPost, images: list) -> tuple[str, str]:  # pragma: no cover - browser
    from automation import browser

    async with browser.persistent_context(headless=False) as ctx:
        return await craigslist.post_listing(
            ctx, subdomain=post.subdomain, title=post.title, body=post.body, price=post.price,
            postal=post.postal, city=post.city, images=images, subarea_pref=post.subarea_pref,
        )


async def _manage_live(external_id: str, action: str) -> bool:  # pragma: no cover - browser
    from automation import browser

    async with browser.persistent_context(headless=False) as ctx:
        if action == "delete":
            return await craigslist.delete_posting(ctx, external_id)
        return await craigslist.renew_posting(ctx, external_id)


def _external_id(row: dict, current: dict | None) -> str:
    ext = (current or {}).get("external_id")
    if not ext:
        url = (current or {}).get("url") or row.get("craigslist_url") or ""
        m = craigslist._POST_URL_RE.match(url)
        ext = m.group(2) if m else ""
    if not ext:
        raise RuntimeError(f"lot {row.get('lot_id')!r} has no Craigslist post id to manage")
    return str(ext)


class CraigslistAdapter:
    platform = "craigslist"
    aliases = ("cl",)
    per_city = True

    # ── orchestrator path (async, per city) ──
    async def publish(self, ctx: "BrowserContext", request: PublishRequest) -> PublishResult:
        data = request.data
        city = request.effective_city
        row = {
            "lot_id": data.lot_id, "title": data.title, "chair_type": data.chair_type,
            "quantity_remaining": data.quantity, "price_per_chair": data.price_per_chair,
            "city": data.city, "state": data.state, "zip_code": data.zip_code,
            "description": data.description_text,
        }
        post = craigslist.build_lot_post(row, city=city or None)
        if request.dry_run:
            return PublishResult(
                platform=self.platform, city=city, status=STATUS_DRY_RUN,
                detail=f"[dry-run] would post {post.title!r} on {post.subdomain}.craigslist.org",
            )
        url, external_id = await craigslist.post_listing(
            ctx, subdomain=post.subdomain, title=post.title, body=post.body, price=post.price,
            postal=post.postal, city=post.city, images=list(data.images)[:craigslist.CL_MAX_PHOTOS],
            subarea_pref=post.subarea_pref,
        )
        if data.lot_id:
            _record_url(data.lot_id, url)
            _record_channel(data.lot_id, state="live", url=url, external_id=external_id)
        return PublishResult(platform=self.platform, city=city, status=STATUS_PUBLISHED, url=url)

    # ── sync-callable path (channel sync loop, scripts/craigslist_lot.py) ──
    def publish_lot(self, row: dict) -> dict:
        """Post the lot once, in its own city. Returns {"url", "external_id"}.
        Raises on anything that stops the post — the sync engine records it."""
        post = craigslist.build_lot_post(row)
        images = craigslist.download_lot_photos(row, log=_log)
        _log(f"posting {post.lot_id} → {post.subdomain}: {post.title!r} ({len(images)} photos)")
        url, external_id = run_sync(lambda: _post_live(post, images))
        _log(f"live: {url}")
        _record_url(post.lot_id, url)
        _record_channel(post.lot_id, state="live", url=url, external_id=external_id)
        return {"url": url, "external_id": external_id}

    def update_lot(self, row: dict, current: dict | None) -> dict:
        raise NotImplementedError(
            "Craigslist edit is not automated — delist and re-list (scripts/craigslist_lot.py)")

    def unpublish_lot(self, row: dict, current: dict | None) -> None:
        external_id = _external_id(row, current)
        clicked = run_sync(lambda: _manage_live(external_id, "delete"))
        if not clicked:
            raise RuntimeError(f"no delete control found on /manage/{external_id} — delete it by hand")
        _record_url(row.get("lot_id") or "", None)
        _record_channel(row.get("lot_id") or "", state="delisted")

    def renew_lot(self, row: dict, current: dict | None) -> bool:
        external_id = _external_id(row, current)
        return bool(run_sync(lambda: _manage_live(external_id, "renew")))


register(CraigslistAdapter())
