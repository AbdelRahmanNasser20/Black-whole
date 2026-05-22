import asyncio
import re
import shutil
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from playwright.async_api import Page, BrowserContext

from .config import (
    CAROUSEL_STABLE_CHECKS,
    CAROUSEL_MAX_CLICKS,
    PAGE_LOAD_WAIT_MS,
    DOWNLOAD_ROOT,
    SCRATCH_DIR,
)

EXTRACT_JS = r"""
() => {
  const title = document.querySelector('h1')?.textContent?.trim() || 'Unknown';
  const allLinks = Array.from(document.querySelectorAll('a'));
  const locLink = allLinks.find(a => {
    const t = a.textContent || '';
    return (t.includes('USA') || t.match(/,\s*[A-Z][a-z]+,/) || t.match(/,\s*[A-Z]{2}/))
      && !t.includes('Search') && !t.includes('View');
  });
  const location = locLink?.textContent?.trim() || 'Unknown';
  const city = location.split(',')[0].trim();
  const stateMatch = location.match(/,\s*([A-Za-z ]+?)(?:,|$)/);
  const state = stateMatch ? stateMatch[1].trim() : '';
  const bodyText = document.body.innerText;
  // Pickup ZIP: scan a window after the "Pickup" label so we don't pick up
  // a stray phone-like 5-digit run elsewhere on the page.
  let zip_code = '';
  const pickupIdx = bodyText.search(/Pickup/i);
  if (pickupIdx !== -1) {
    const zipMatch = bodyText.slice(pickupIdx, pickupIdx + 600).match(/\b(\d{5})(?:-(\d{4}))?\b/);
    if (zipMatch) zip_code = zipMatch[2] ? `${zipMatch[1]}-${zipMatch[2]}` : zipMatch[1];
  }

  // Seller contact: scan around the "Contact Information" / "Seller" label.
  // Best-effort regex; the LLM gets the screenshot and can correct anything
  // weird this misses.
  let contact_email = '';
  let contact_phone = '';
  const contactIdx = bodyText.search(/Contact Information|Seller Information|Contact:/i);
  const contactWindow = contactIdx !== -1
    ? bodyText.slice(contactIdx, contactIdx + 800)
    : bodyText;
  const emailMatch = contactWindow.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
  if (emailMatch) contact_email = emailMatch[0];
  const phoneMatch = contactWindow.match(/\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}/);
  if (phoneMatch) contact_phone = phoneMatch[0].trim();
  const qtyMatch = bodyText.match(/\((\d{1,5})\)/);
  const titleQtyMatch = title.match(/(\d+)/);
  const quantity = qtyMatch ? qtyMatch[1] : (titleQtyMatch ? titleQtyMatch[1] : 'NA');

  // Gather from every rendering style: <img src>, <img srcset>, <source srcset>,
  // data-src, and CSS background-image. GovDeals uses a mix of these.
  const srcBag = new Set();
  for (const el of document.querySelectorAll('img')) {
    if (el.src) srcBag.add(el.src);
    if (el.currentSrc) srcBag.add(el.currentSrc);
    const ds = el.getAttribute('data-src'); if (ds) srcBag.add(ds);
    const ss = el.getAttribute('srcset');
    if (ss) ss.split(',').forEach(s => srcBag.add(s.trim().split(' ')[0]));
  }
  for (const el of document.querySelectorAll('source')) {
    const ss = el.getAttribute('srcset');
    if (ss) ss.split(',').forEach(s => srcBag.add(s.trim().split(' ')[0]));
  }
  for (const el of document.querySelectorAll('[style*="background"]')) {
    const m = (el.getAttribute('style') || '').match(/url\(['"]?([^)'"]+)['"]?\)/);
    if (m) srcBag.add(m[1]);
  }
  // Regex-scan full HTML for any lqdt CDN photo URL (catches inline scripts, JSON blobs).
  const html = document.documentElement.outerHTML;
  const reCdn = /https?:\/\/[^"'\s<>]*(?:webassets|files)\.lqdt1\.com\/(?:assets\/)?photos\/[^"'\s<>]+\.(?:jpe?g|png|webp)[^"'\s<>]*/ig;
  (html.match(reCdn) || []).forEach(u => srcBag.add(u));

  // Prefer the largest variant per "logical" image (same path, different ?w= / ?h=).
  // Strip resize query keys; keep the ?cb=<token> cache buster since the CDN wants it.
  const byKey = new Map();
  const scoreOf = u => {
    const m = u.match(/[?&]w=(\d+)/) || u.match(/[?&]h=(\d+)/);
    return m ? parseInt(m[1], 10) : 0;
  };
  for (const raw of srcBag) {
    if (!raw || raw.includes('youtube') || raw.includes('ecomm/')) continue;
    if (!/photos\//.test(raw)) continue;
    const [path, qs=''] = raw.split('?');
    const params = new URLSearchParams(qs);
    // Keep only the cache buster — strip width/height/format knobs so we get the original.
    const cb = params.get('cb');
    const cleanUrl = cb ? `${path}?cb=${cb}` : path;
    const key = path; // same logical image
    const score = scoreOf(raw);
    const prev = byKey.get(key);
    if (!prev || score > prev.score) byKey.set(key, { url: cleanUrl, score });
  }
  const urls = [...byKey.values()].map(v => v.url);

  const counterEl = document.querySelector('.lg-counter, [class*="counter"]');
  const counter = counterEl ? counterEl.textContent.trim() : '';

  return { title, location, city, state, zip_code, contact_email, contact_phone,
           quantity, urls, counter,
           description: bodyText.slice(0, 4000) };
}
"""

ARROW_SELECTORS = [
    ".lg-next",
    "button[aria-label='Next']",
    ".carousel-control-next",
    "[class*='next'][class*='arrow']",
]


@dataclass
class ListingMetadata:
    url: str
    title: str
    location: str
    city: str
    state: str
    zip_code: str
    contact_email: str
    contact_phone: str
    quantity: str
    lot_id: str
    seller_id: str
    image_urls: list[str]
    description_text: str
    folder_name: str
    folder_path: Path
    screenshots: dict[str, Path] = field(default_factory=dict)
    scratch_dir: Path | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["folder_path"] = str(self.folder_path)
        d["screenshots"] = {k: str(v) for k, v in self.screenshots.items()}
        if self.scratch_dir:
            d["scratch_dir"] = str(self.scratch_dir)
        return d


def _slug(s: str) -> str:
    return re.sub(r"\s+", "_", re.sub(r"[^a-zA-Z0-9\s]", "", s)).strip("_")


def _parse_url(url: str) -> tuple[str, str]:
    m = re.search(r"/asset/(\d+)/(\d+)", url)
    if not m:
        return ("", "")
    return m.group(1), m.group(2)


async def _click_carousel_until_stable(page: Page) -> None:
    last_count = -1
    stable = 0
    for _ in range(CAROUSEL_MAX_CLICKS):
        count = await page.evaluate(
            "document.querySelectorAll('.lg-item img, img[src*=\"assets/photos\"]').length"
        )
        if count == last_count:
            stable += 1
            if stable >= CAROUSEL_STABLE_CHECKS:
                return
        else:
            stable = 0
            last_count = count
        clicked = False
        for sel in ARROW_SELECTORS:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click(timeout=500)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            await page.keyboard.press("ArrowRight")
        await asyncio.sleep(0.25)


def build_folder_name(
    city: str,
    state: str,
    chair_title: str,
    quantity: str | int,
) -> str:
    """Folder name shape: `{City}_{State}_{ChairTitle}_{Qty}`.

    Empty parts collapse to 'Unknown' so we never end up with `__` runs that
    make folder paths ambiguous.
    """
    parts = [
        _slug(city) or "Unknown",
        _slug(state) or "Unknown",
        _slug(chair_title) or "Chairs",
        str(quantity),
    ]
    return "_".join(parts)


def finalize_folder(
    meta: "ListingMetadata",
    quantity: str | int,
    *,
    chair_title: str | None = None,
    state: str | None = None,
    city: str | None = None,
) -> "ListingMetadata":
    """Move screenshots out of the scratch dir into the real listing folder.

    Called *after* the LLM finalizes quantity + chair title + location, so the
    folder name reflects the corrected values rather than whatever the brittle
    DOM regex produced. Fallbacks: `meta.city`/`meta.state`/`meta.title`.
    """
    folder_name = build_folder_name(
        city or meta.city,
        state or meta.state,
        chair_title or meta.title,
        quantity,
    )
    folder_path = DOWNLOAD_ROOT / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)
    shots_dir = folder_path / "_screenshots"
    shots_dir.mkdir(exist_ok=True)

    new_screenshots: dict[str, Path] = {}
    for kind, src in meta.screenshots.items():
        if not src.exists():
            new_screenshots[kind] = src
            continue
        dst = shots_dir / src.name
        if src.resolve() == dst.resolve():
            new_screenshots[kind] = dst
            continue
        try:
            shutil.move(str(src), str(dst))
        except Exception:
            shutil.copy2(src, dst)
        new_screenshots[kind] = dst

    # Best-effort: remove the now-empty scratch dir (ignore if non-empty).
    if meta.scratch_dir and meta.scratch_dir.exists():
        try:
            shutil.rmtree(meta.scratch_dir)
        except OSError:
            pass

    meta.folder_name = folder_name
    meta.folder_path = folder_path
    meta.screenshots = new_screenshots
    meta.quantity = str(quantity)
    return meta


async def scrape(ctx: BrowserContext, url: str) -> ListingMetadata:
    page = await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
    await _click_carousel_until_stable(page)
    data = await page.evaluate(EXTRACT_JS)

    seller_id, lot_id = _parse_url(url)
    # Filter to images that belong to THIS lot. Related-listing thumbnails are
    # from /photos/<other_lot_id>/... and would otherwise contaminate the FB/eBay upload.
    if lot_id:
        data["urls"] = [u for u in data["urls"] if f"/photos/{lot_id}/" in u]

    # Screenshots live in scratch until the LLM finalizes quantity (avoids
    # baking a wrong DOM-quantity into the final folder path).
    scratch_dir = SCRATCH_DIR / f"{lot_id or 'unknown'}_{int(time.time())}"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    header_shot = scratch_dir / "header.png"
    full_shot = scratch_dir / "full.png"
    await page.screenshot(path=str(full_shot), full_page=True)
    h1 = await page.query_selector("h1")
    if h1:
        header_el = await h1.evaluate_handle("el => el.closest('section, main, body') || el")
        try:
            await header_el.as_element().screenshot(path=str(header_shot))
        except Exception:
            await page.screenshot(path=str(header_shot))
    else:
        await page.screenshot(path=str(header_shot))

    await page.close()

    # Provisional folder fields use the DOM values. run.py overwrites them
    # via finalize_folder() once the LLM produces corrected values.
    provisional_name = build_folder_name(
        data["city"], data["state"], data["title"], data["quantity"],
    )
    return ListingMetadata(
        url=url,
        title=data["title"],
        location=data["location"],
        city=data["city"],
        state=data["state"],
        zip_code=data.get("zip_code", "") or "",
        contact_email=data.get("contact_email", "") or "",
        contact_phone=data.get("contact_phone", "") or "",
        quantity=str(data["quantity"]),
        lot_id=lot_id,
        seller_id=seller_id,
        image_urls=data["urls"],
        description_text=data["description"],
        folder_name=provisional_name,
        folder_path=DOWNLOAD_ROOT / provisional_name,  # provisional; finalize_folder overwrites
        screenshots={"header": header_shot, "full": full_shot},
        scratch_dir=scratch_dir,
    )
