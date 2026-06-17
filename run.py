#!/usr/bin/env python3
"""GovDeals -> FB Marketplace + eBay draft automation.

Usage:
    python run.py https://www.govdeals.com/en/asset/<seller>/<lot>
    python run.py --login-only
"""
import asyncio
import os
import sys
from pathlib import Path

import click

from automation import browser, govdeals, downloader, dewatermark as dw, facebook, ebay, inventory, listing_images
from automation.llm import default_extractors, run_parallel
from automation import progress, templates


async def _wait_for_price_confirmation(suggested: int, timeout: float) -> int | None:
    """Read one line from stdin (dashboard pipes the chosen price here).

    Returns the parsed int, or None on timeout / unparseable input.
    """
    loop = asyncio.get_event_loop()
    try:
        line = await asyncio.wait_for(
            loop.run_in_executor(None, sys.stdin.readline),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return None
    line = (line or "").strip()
    if not line:
        return None
    try:
        return int(line)
    except ValueError:
        return None


async def _login_only() -> None:
    print("Opening persistent browser with two tabs (Facebook + eBay).")
    print("Log into each tab, then CLOSE THE BROWSER WINDOW to save the session.")
    urls = [
        "https://www.facebook.com/login",
        "https://www.ebay.com/signin/",
    ]
    async with browser.persistent_context() as ctx:
        for url in urls:
            page = await ctx.new_page()
            await page.goto(url)
        closed = asyncio.Event()
        ctx.on("close", lambda _ctx: closed.set())
        await closed.wait()
        print("Browser closed. Session saved.")


async def _run(
    url: str,
    price_override: int | None,
    skip_dw: bool,
    skip_fb: bool,
    skip_ebay: bool,
    force_republish: bool = False,
) -> None:
    async with browser.persistent_context() as ctx:
        progress.emit("run", status="started", url=url)
        print(f"[1/5] scraping {url}")
        progress.emit("phase", phase="scrape", status="running")
        meta = await govdeals.scrape(ctx, url)
        print(f"  title={meta.title!r}")
        print(f"  location={meta.location!r}  qty={meta.quantity}  images={len(meta.image_urls)}")
        progress.emit(
            "phase", phase="scrape", status="done",
            title=meta.title, location=meta.location,
            quantity=meta.quantity, image_count=len(meta.image_urls),
            folder=str(meta.folder_path), lot_id=meta.lot_id,
        )

        print("[LLM] extracting metadata (primary + secondary in parallel)")
        progress.emit("phase", phase="llm", status="running")
        primary_ext, secondary_ext = default_extractors()
        dom_hint = {
            "title": meta.title, "location": meta.location, "city": meta.city,
            "state": meta.state, "zip_code": meta.zip_code,
            "contact_email": meta.contact_email, "contact_phone": meta.contact_phone,
            "quantity": meta.quantity,
            "description_head": meta.description_text[:1000],
        }
        primary, secondary = await run_parallel(
            primary_ext, secondary_ext, dom_hint, meta.screenshots,
        )
        print(f"  primary ({primary.source}): {primary.to_dict()}")
        if secondary:
            print(f"  secondary ({secondary.source}): {secondary.to_dict()}")
        progress.emit(
            "phase", phase="llm", status="done",
            primary=primary.to_dict() if primary else None,
            secondary=secondary.to_dict() if secondary else None,
        )

        # Folder name uses the LLM-corrected quantity, chair title, and state.
        # Before this point, meta carries a provisional folder; finalize_folder()
        # moves screenshots into the real folder and updates meta.folder_path.
        govdeals.finalize_folder(
            meta, primary.quantity,
            chair_title=(primary.chair_title or primary.chair_type),
            state=primary.state,
            city=primary.city,
        )
        progress.emit(
            "folder", folder=str(meta.folder_path), folder_name=meta.folder_name,
            quantity=meta.quantity,
        )

        suggested_price = primary.suggested_price_per_chair
        if price_override is not None:
            confirmed = price_override
            print(f"  per-chair price: ${confirmed} (--price override; suggested was ${suggested_price})")
            progress.emit("price", suggested=suggested_price, confirmed=confirmed)
        elif sys.stdin.isatty():
            # Announce BEFORE blocking so the dashboard can show the input UI.
            progress.emit("price", suggested=suggested_price, confirmed=None)
            confirmed = click.prompt(
                f"  per-chair price? [${suggested_price}]",
                default=suggested_price, type=int,
            )
            progress.emit("price", suggested=suggested_price, confirmed=confirmed)
        else:
            # Non-interactive (dashboard subprocess, background): emit a "needs
            # confirmation" event and wait briefly for the dashboard UI to POST
            # to /api/runs/stdin. Times out → auto-accept the suggestion so
            # truly unattended runs aren't blocked forever.
            confirm_timeout = float(os.getenv("LISTING_PRICE_CONFIRM_TIMEOUT", "0") or 0)
            progress.emit("price", suggested=suggested_price, confirmed=None)
            confirmed_via_stdin = None
            if confirm_timeout > 0:
                confirmed_via_stdin = await _wait_for_price_confirmation(
                    suggested_price, confirm_timeout,
                )
            if confirmed_via_stdin is not None:
                confirmed = confirmed_via_stdin
                print(f"  per-chair price: ${confirmed} (confirmed via dashboard)")
            else:
                confirmed = suggested_price
                print(f"  per-chair price: ${confirmed} (auto-accepted after no-TTY timeout)")
            progress.emit("price", suggested=suggested_price, confirmed=confirmed)

        print(f"[2/5] downloading {len(meta.image_urls)} images -> {meta.folder_path}")
        progress.emit(
            "phase", phase="download", status="running",
            image_count=len(meta.image_urls),
        )
        local_imgs = await downloader.download_all(
            meta.image_urls, meta.folder_path, meta.folder_name,
        )
        print(f"  downloaded {len(local_imgs)} files")
        progress.emit(
            "phase", phase="download", status="done",
            downloaded=len(local_imgs), folder=str(meta.folder_path),
        )

        cleaned: list[Path] = local_imgs
        if not skip_dw:
            print("[3/5] dewatermarking via dewatermark.ai API")
            progress.emit("phase", phase="dewatermark", status="running")
            cleaned = await dw.dewatermark(ctx, local_imgs, meta.folder_path) or local_imgs
            print(f"  {len(cleaned)} cleaned images ready")
            progress.emit(
                "phase", phase="dewatermark", status="done", cleaned=len(cleaned),
            )
        else:
            print("[3/5] skipped")
            progress.emit("phase", phase="dewatermark", status="skipped")

        existing_inv = inventory.get(meta.lot_id) if meta.lot_id else None
        fb_url: str | None = None
        fb_rendered_description: str | None = None
        if not skip_fb:
            if existing_inv and existing_inv.get("facebook_url") and not force_republish:
                fb_url = existing_inv["facebook_url"]
                print(f"[4/5] Facebook — skipped (already published: {fb_url})")
                progress.emit(
                    "phase", phase="facebook", status="skipped_duplicate", url=fb_url,
                )
            else:
                print("[4/5] Facebook Marketplace draft")
                progress.emit("phase", phase="facebook", status="running")
                fb_url, fb_rendered_description = await facebook.create_draft(
                    ctx=ctx,
                    title=primary.title,
                    price_per_chair=confirmed,
                    chair_type=primary.chair_type,
                    location=primary.location,
                    quantity=primary.quantity,
                    dimensions=primary.dimensions,
                    style_suffix=primary.style_suffix,
                    images=cleaned,
                    description_text=primary.description_text,
                    city=primary.city or meta.city,
                    state=primary.state or meta.state,
                    zip_code=primary.zip_code or meta.zip_code,
                    sku=meta.lot_id or "",
                )
                print(f"  FB draft: {fb_url}")
                progress.emit("phase", phase="facebook", status="done", url=fb_url)
        else:
            print("[4/5] skipped")
            progress.emit("phase", phase="facebook", status="skipped")

        ebay_url: str | None = None
        if not skip_ebay:
            if existing_inv and existing_inv.get("ebay_url") and not force_republish:
                ebay_url = existing_inv["ebay_url"]
                print(f"[5/5] eBay — skipped (already published: {ebay_url})")
                progress.emit(
                    "phase", phase="ebay", status="skipped_duplicate", url=ebay_url,
                )
            else:
                print("[5/5] eBay draft")
                progress.emit("phase", phase="ebay", status="running")
                ebay_url = await ebay.create_draft(
                    ctx=ctx,
                    title=primary.title,
                    chair_type=primary.chair_type,
                    location=primary.location,
                    city=primary.city,
                    state=primary.state,
                    zip_code=primary.zip_code or meta.zip_code,
                    quantity=primary.quantity,
                    dimensions=primary.dimensions,
                    price_each=confirmed,
                    lot_id=meta.lot_id,
                    images=cleaned,
                    description_text=primary.description_text,
                )
                print(f"  eBay draft: {ebay_url}")
                progress.emit("phase", phase="ebay", status="done", url=ebay_url)
        else:
            print("[5/5] skipped")
            progress.emit("phase", phase="ebay", status="skipped")

        # Persist the run to the inventory ledger. Done unconditionally so a row
        # exists even when both marketplaces were skipped — the public site and
        # admin tab need the metadata regardless.
        if meta.lot_id:
            try:
                qty_int = int(primary.quantity) if str(primary.quantity).isdigit() else None
                # SKU is the raw GovDeals lot id — same value that appears in
                # the source URL (/asset/<seller>/<lot_id>) and in the eBay
                # custom-label field. Easy to grep across systems.
                sku = meta.lot_id
                hero = cleaned[0].name if cleaned else None
                # Mirror cleaned photos into the shared Supabase Storage bucket
                # so the deployed site can serve them (BLACKWHOLE-6). Best-effort:
                # if storage is unconfigured or upload fails, URLs stay None and
                # the site falls back to local /image/ serving.
                hero_image_url: str | None = None
                image_urls: list[str] | None = None
                if cleaned:
                    progress.emit("phase", phase="upload", status="running")
                    try:
                        uploaded = listing_images.upload_lot_images(meta.lot_id, cleaned)
                    except Exception as e:  # never let storage crash a run
                        uploaded = None
                        print(f"  [warn] image upload failed: {e!r}")
                    if uploaded:
                        hero_image_url = uploaded.get("hero_image_url")
                        image_urls = uploaded.get("image_urls") or None
                        print(f"  uploaded {len(image_urls or [])} images to storage")
                        progress.emit(
                            "phase", phase="upload", status="done",
                            uploaded=len(image_urls or []), hero=hero_image_url,
                        )
                    else:
                        progress.emit("phase", phase="upload", status="skipped")
                # Prefer the exact FB copy we just posted; otherwise render it
                # locally so the public site has a clean product description.
                rendered_desc = fb_rendered_description or templates.fb_description(
                    location=primary.location,
                    chair_type=primary.chair_type,
                    quantity=primary.quantity,
                    dimensions=primary.dimensions,
                    description_text=primary.description_text,
                    city=primary.city or meta.city,
                    state=primary.state or meta.state,
                    zip_code=primary.zip_code or meta.zip_code,
                )
                inventory.upsert_from_run(
                    lot_id=meta.lot_id,
                    seller_id=meta.seller_id or None,
                    govdeals_url=url,
                    folder_name=meta.folder_name,
                    folder_path=str(meta.folder_path),
                    sku=sku,
                    title=primary.title,
                    description=rendered_desc,
                    city=primary.city or meta.city,
                    state=primary.state or meta.state,
                    zip_code=primary.zip_code or meta.zip_code,
                    contact_name=primary.contact_name,
                    contact_email=primary.contact_email or meta.contact_email,
                    contact_phone=primary.contact_phone or meta.contact_phone,
                    chair_type=primary.chair_type,
                    dimensions=primary.dimensions,
                    quantity=qty_int,
                    price_per_chair=float(confirmed) if confirmed else None,
                    hero_image=hero,
                    hero_image_url=hero_image_url,
                    image_urls=image_urls,
                )
                if fb_url:
                    inventory.set_platform_url(meta.lot_id, "facebook", fb_url)
                if ebay_url:
                    inventory.set_platform_url(meta.lot_id, "ebay", ebay_url)
                progress.emit("inventory", status="stored", lot_id=meta.lot_id)
            except Exception as e:
                print(f"  [warn] inventory upsert failed: {e!r}")
                progress.emit("inventory", status="error", error=repr(e))

        progress.emit("run", status="finished")
        print("\nBoth drafts ready. Review in the browser, then publish manually.")
        if sys.stdin.isatty():
            print("Press Enter to close the browser...")
            await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
        else:
            print("(Non-interactive run — leaving the browser open for review.)")
            # Keep the browser alive so the user can interact with the FB/eBay drafts.
            try:
                while True:
                    await asyncio.sleep(60)
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass


@click.command()
@click.argument("url", required=False)
@click.option("--login-only", is_flag=True, help="Open browser so you can log into FB/eBay/dewatermark.ai once.")
@click.option("--price", type=int, default=None, help="Override LLM-suggested per-chair price.")
@click.option("--skip-dewatermark", is_flag=True)
@click.option("--skip-fb", is_flag=True)
@click.option("--skip-ebay", is_flag=True)
@click.option("--force-republish", is_flag=True,
              help="Ignore inventory ledger — create fresh FB/eBay drafts even if this lot was published before.")
def main(url, login_only, price, skip_dewatermark, skip_fb, skip_ebay, force_republish):
    if login_only:
        asyncio.run(_login_only())
        return
    if not url:
        raise click.UsageError("URL required (or pass --login-only)")
    asyncio.run(_run(url, price, skip_dewatermark, skip_fb, skip_ebay, force_republish))


if __name__ == "__main__":
    main()
