"""Craigslist multi-city cross-posting (BLACKWHOLE-20).

Post ONE source-of-truth listing object to many Craigslist cities from a single
invocation, with per-city copy variation so near-identical posts don't get
"ghosted" by Craigslist's duplicate detection.

This is a self-contained adapter (mirrors ``automation/facebook.py`` /
``automation/ebay.py`` conventions). It deliberately does NOT touch ``run.py``
or the shared pipeline so it composes cleanly with the multi-platform publish
orchestrator being built in parallel (BLACKWHOLE-21).

SAFETY — no accidental live posting
-----------------------------------
Everything defaults to a DRY RUN. A real Craigslist submission is only ever
attempted when BOTH:

  * the caller passes ``dry_run=False``, AND
  * the environment sets ``CRAIGSLIST_LIVE=1``.

The live flow (`post_listing`) is the exact step sequence recorded on
2026-10-03 against a logged-in account; it posts "for sale by owner →
furniture" (free — never by-dealer, which is paid) and DOES click publish.
The channel sync loop gates it behind `channel_craigslist_enabled` (ships 0)
plus the browser pacing caps; the standalone CLIs behind `CRAIGSLIST_LIVE=1`.
Tests exercise the pure logic only (city resolution, copy, draft building,
dry-run orchestration) and never open a browser or hit the network.
"""

from __future__ import annotations

import inspect
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Union

from .templates import listing_title, state_abbr


# ── City registry ───────────────────────────────────────────────────────────
# Craigslist is sharded by city subdomain (``phoenix.craigslist.org``). We map
# the metros the epic (BLACKWHOLE-5) cares about — PHX, GA, LA, Midwest, DC —
# plus a handful of common aliases. Anything not in the table is assumed to be
# a valid subdomain already and passed through normalized.
CITY_SUBDOMAINS: dict[str, str] = {
    # Phoenix
    "phoenix": "phoenix", "phx": "phoenix", "az": "phoenix", "arizona": "phoenix",
    # Atlanta / Georgia
    "atlanta": "atlanta", " atl": "atlanta", "atl": "atlanta",
    "ga": "atlanta", "georgia": "atlanta",
    # Los Angeles
    "losangeles": "losangeles", "los angeles": "losangeles", "la": "losangeles",
    # Chicago / Midwest
    "chicago": "chicago", "chi": "chicago", "midwest": "chicago",
    # Washington DC
    "washingtondc": "washingtondc", "washington dc": "washingtondc",
    "washington": "washingtondc", "dc": "washingtondc",
    # A few more large metros for convenience
    "dallas": "dallas", "houston": "houston", "newyork": "newyork",
    "new york": "newyork", "nyc": "newyork", "sfbay": "sfbay",
    "sanfrancisco": "sfbay", "seattle": "seattle", "denver": "denver",
    "miami": "miami", "boston": "boston",
    # Cities our inventory actually sits in (2026-10)
    "pittsburgh": "pittsburgh", "nashville": "nashville", "boise": "boise",
    "las vegas": "lasvegas", "lasvegas": "lasvegas", "vegas": "lasvegas",
    "tampa": "tampa", "orlando": "orlando",
    "orange county": "orangecounty", "orangecounty": "orangecounty",
    "stanton": "orangecounty", "cypress": "orangecounty", "anaheim": "orangecounty",
}

# Pretty labels for the copy. Anything missing falls back to a title-cased slug.
SUBDOMAIN_LABELS: dict[str, str] = {
    "phoenix": "Phoenix",
    "atlanta": "Atlanta",
    "losangeles": "Los Angeles",
    "chicago": "Chicago",
    "washingtondc": "Washington, DC",
    "dallas": "Dallas",
    "houston": "Houston",
    "newyork": "New York",
    "sfbay": "SF Bay Area",
    "seattle": "Seattle",
    "denver": "Denver",
    "miami": "Miami",
    "boston": "Boston",
    "pittsburgh": "Pittsburgh",
    "nashville": "Nashville",
    "boise": "Boise",
    "lasvegas": "Las Vegas",
    "tampa": "Tampa",
    "orlando": "Orlando",
    "orangecounty": "Orange County",
}

# Sub-area to pick when a city asks "choose the location that fits best"
# (substring, case-insensitive). Cities not listed take the first option.
SUBAREA_PREF: dict[str, str] = {
    "phoenix": "phx north",
    "atlanta": "city of atlanta",
}

# `post.craigslist.org/c/<code>` entry codes verified live 2026-10-03. A city
# not listed is tried as `/c/<subdomain>` and falls back to the home page's
# "create a posting" link.
CL_POST_CODES: dict[str, str] = {
    "phoenix": "phx", "atlanta": "atl", "pittsburgh": "pit", "nashville": "nsh",
    "boise": "boi", "lasvegas": "lvg", "orangecounty": "orc", "tampa": "tpa", "orlando": "orl",
}

# City-centre ZIP used only when the inventory row has none. Never invent a ZIP
# for a city that is not here — `build_lot_post` raises instead.
CITY_ZIP: dict[str, str] = {
    "phoenix": "85054", "atlanta": "30303", "pittsburgh": "15222", "nashville": "37203",
    "orangecounty": "90680", "boise": "83702", "lasvegas": "89101", "tampa": "33602",
    "orlando": "32801",
}

# Craigslist post-flow constants (recorded 2026-10-03). Radios are matched by
# label text, never by numeric value — category ids differ per city.
CL_POST_BASE = "https://post.craigslist.org"
CL_TYPE_FOR_SALE_BY_OWNER = "fso"      # radio value on ?s=type — by-dealer is paid, never used
CL_CATEGORY_LABEL = "furniture"        # radio label on ?s=cat
CL_CONDITION_GOOD = "40"               # <select name=condition>
CL_LANGUAGE_ENGLISH = "5"              # <select name=language>
CL_TITLE_MAX = 70
CL_MAX_PHOTOS = 8
_POST_URL_RE = re.compile(r"^https://([a-z]+)\.craigslist\.org/.+/(\d+)\.html$")

def _norm(city: str) -> str:
    return (city or "").strip().lower()


def resolve_subdomain(city: str) -> str:
    """Map a human city name / alias to its Craigslist subdomain.

    Falls back to the normalized token (spaces stripped) on the assumption the
    caller already passed a valid subdomain (e.g. ``"sacramento"``). Raises on
    an empty value so a blank ``--cities`` entry fails loudly rather than
    silently targeting ``.craigslist.org``.
    """
    key = _norm(city)
    if not key:
        raise ValueError("empty city — cannot resolve a Craigslist subdomain")
    if key in CITY_SUBDOMAINS:
        return CITY_SUBDOMAINS[key]
    # Passthrough: assume it's already a subdomain; drop spaces/dots.
    return key.replace(" ", "").replace(".", "")


def city_label(subdomain: str) -> str:
    """Human label for a subdomain, e.g. ``losangeles`` -> ``Los Angeles``."""
    return SUBDOMAIN_LABELS.get(subdomain, subdomain.replace("_", " ").title())


def post_url_for(subdomain: str) -> str:
    """Craigslist "create a posting" entry point for a city subdomain."""
    return f"https://{subdomain}.craigslist.org/"


# ── Data model ──────────────────────────────────────────────────────────────
@dataclass
class CraigslistListing:
    """One source-of-truth listing, cross-posted to every requested city.

    Field names mirror ``automation.llm.base.Extraction`` so callers can build
    one straight from a pipeline extraction without a translation layer.
    ``price`` is the per-chair asking price (USD).
    """

    chair_type: str
    quantity: str
    price: int
    description_text: str = ""
    dimensions: str = ""
    location: str = ""          # raw source location (fallback for the body)
    state: str = ""             # home state of the inventory, if any
    zip_code: str = ""
    images: list[Path] = field(default_factory=list)
    contact_email: str = ""
    contact_phone: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["images"] = [str(p) for p in self.images]
        return d


@dataclass
class CityDraft:
    """The per-city result of a cross-post run.

    ``status`` transitions:
      pending  -> built but not yet acted on
      dry_run  -> prepared copy only; nothing submitted (default outcome)
      posted   -> live browser published it; `detail_url` is the post URL
      skipped  -> city could not be resolved / was blank
      error    -> the live flow raised
    """

    city_slug: str
    city_label: str
    subdomain: str
    post_url: str
    title: str
    body: str
    price: int
    status: str = "pending"
    detail_url: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# A varier turns (listing, subdomain, city_label) into a (title, body) pair.
# It may be sync or async so tests can inject either flavor.
Varier = Callable[
    [CraigslistListing, str, str],
    Union[tuple[str, str], Awaitable[tuple[str, str]]],
]


# ── Per-city copy variation ─────────────────────────────────────────────────
# Craigslist ghosts posts whose title+body are byte-identical across cities.
# The deterministic varier rotates the intro and call-to-action by a stable
# hash of the subdomain and always stamps the local metro into the copy, so
# every city's post reads differently without an LLM in the loop. The LLM
# varier (Gemini, approved in the ticket) is an opt-in upgrade that falls back
# to the deterministic one on any failure.

_INTROS = [
    "Clearing out a bulk lot of {chair_type} — priced to move for {label} buyers.",
    "{label} pickup: bulk {chair_type} available now, perfect for events and venues.",
    "Bulk {chair_type} for sale, local {label} pickup. Great for churches and halls.",
    "We have a large quantity of {chair_type} ready for pickup in the {label} area.",
    "Selling {chair_type} in bulk near {label} — ideal for schools and event spaces.",
]

_CTAS = [
    "Message with the quantity you need and whether you want pickup or delivery.",
    "Reply with how many you need plus your ZIP for a delivery quote.",
    "Text or email the quantity you're after — bulk discounts on larger orders.",
    "Let us know your quantity and city and we'll send pricing right over.",
    "Serious buyers: send quantity + pickup/delivery and we'll get you a quote.",
]


def _rotate(options: list[str], subdomain: str) -> str:
    idx = sum(ord(c) for c in subdomain) % len(options)
    return options[idx]


def _body_lines(listing: CraigslistListing, label: str) -> list[str]:
    lines: list[str] = []
    desc = (listing.description_text or "").strip()
    if desc:
        lines.append(desc)
    lines.append(f"Quantity available: {listing.quantity}")
    if listing.dimensions.strip():
        lines.append(f"Dimensions: {listing.dimensions.strip()}")
    lines.append(f"Asking ${listing.price} per chair — bulk discounts on the full lot.")
    lines.append(f"Local pickup in the {label} area (delivery quotes on request).")
    lines.append("Ideal for churches, banquet halls, schools, and event venues.")
    return lines


def deterministic_varier(
    listing: CraigslistListing, subdomain: str, label: str
) -> tuple[str, str]:
    """No-network per-city copy. Stable, unique-per-city, safe for tests."""
    title = listing_title(listing.chair_type, city=label, state=listing.state,
                          fallback=f"{listing.quantity} {listing.chair_type}".strip())
    intro = _rotate(_INTROS, subdomain).format(
        chair_type=listing.chair_type or "chairs", label=label,
    )
    cta = _rotate(_CTAS, subdomain)
    body = "\n\n".join([intro, "\n".join(_body_lines(listing, label)), cta])
    return title, body


async def llm_varier(
    listing: CraigslistListing, subdomain: str, label: str,
    *, api_key: str | None = None, model: str | None = None,
) -> tuple[str, str]:
    """Gemini-backed per-city copy. Falls back to deterministic on any issue.

    Kept dependency-light: imports google-genai lazily so importing this module
    (and running the dry-run tests) never requires an API key or the SDK.
    """
    from .config import GEMINI_API_KEY, GEMINI_MODEL

    key = api_key or GEMINI_API_KEY
    if not key:
        return deterministic_varier(listing, subdomain, label)

    base_title, base_body = deterministic_varier(listing, subdomain, label)
    prompt = (
        "Rewrite this Craigslist for-sale listing so it reads naturally for "
        f"buyers in {label}. Keep every fact identical (quantity, price, "
        "dimensions, pickup logistics) but vary the wording so it is clearly "
        "distinct from the same listing posted in other cities — Craigslist "
        "removes near-duplicate posts. No emojis. No auction/bidding language. "
        "Return two lines exactly:\nTITLE: <one-line title>\nBODY: <body>\n\n"
        f"Reference title: {base_title}\nReference body:\n{base_body}"
    )
    try:
        from google import genai  # type: ignore

        client = genai.Client(api_key=key)
        resp = await client.aio.models.generate_content(
            model=model or GEMINI_MODEL, contents=[prompt],
        )
        text = (resp.text or "").strip()
        title, body = _parse_llm_copy(text)
        if not title or not body:
            return base_title, base_body
        return title, body
    except Exception as e:  # pragma: no cover - network/SDK path
        print(f"[craigslist] LLM copy variation failed for {subdomain} "
              f"({type(e).__name__}: {str(e)[:120]}); using deterministic copy")
        return base_title, base_body


def _parse_llm_copy(text: str) -> tuple[str, str]:
    """Pull TITLE:/BODY: out of the LLM response. Best-effort."""
    title, body_parts, in_body = "", [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("TITLE:"):
            title = stripped[len("TITLE:"):].strip()
            in_body = False
        elif stripped.upper().startswith("BODY:"):
            body_parts.append(stripped[len("BODY:"):].strip())
            in_body = True
        elif in_body:
            body_parts.append(line)
    return title, "\n".join(body_parts).strip()


# ── Draft building ──────────────────────────────────────────────────────────
async def build_city_draft(
    listing: CraigslistListing, city: str, *, varier: Varier | None = None,
) -> CityDraft:
    """Build the per-city draft (copy + target URL). No network, no browser.

    ``varier`` may be sync or async; ``None`` uses the deterministic varier.
    """
    try:
        subdomain = resolve_subdomain(city)
    except ValueError as e:
        return CityDraft(
            city_slug=_norm(city), city_label=city.strip(), subdomain="",
            post_url="", title="", body="", price=listing.price,
            status="skipped", error=str(e),
        )

    label = city_label(subdomain)
    fn = varier or deterministic_varier
    result = fn(listing, subdomain, label)
    if inspect.isawaitable(result):
        result = await result
    title, body = result

    return CityDraft(
        city_slug=subdomain,
        city_label=label,
        subdomain=subdomain,
        post_url=post_url_for(subdomain),
        title=title,
        body=body,
        price=listing.price,
        status="pending",
    )


def _live_enabled() -> bool:
    return (os.getenv("CRAIGSLIST_LIVE") or "").strip().lower() in ("1", "true", "yes", "on")


async def cross_post(
    listing: CraigslistListing,
    cities: list[str],
    *,
    dry_run: bool = True,
    use_llm: bool = False,
    varier: Varier | None = None,
    headless: bool = False,
) -> list[CityDraft]:
    """Cross-post one listing to many cities in a single invocation.

    Returns one :class:`CityDraft` per requested city. Defaults to a dry run:
    copy is prepared for every city but nothing is submitted. A live run
    requires BOTH ``dry_run=False`` AND ``CRAIGSLIST_LIVE=1``; it then runs
    `post_listing` per city (which publishes) and records the post URL.
    """
    if varier is None and use_llm:
        varier = llm_varier

    drafts = [await build_city_draft(listing, c, varier=varier) for c in cities]

    live = (not dry_run) and _live_enabled()
    if not live:
        for d in drafts:
            if d.status == "pending":
                d.status = "dry_run"
        return drafts

    # ── LIVE path (double-gated) ────────────────────────────────────────────
    # Lazily import the browser so the dry-run/test path never needs Playwright
    # loaded via this module. One shared browser context for the whole batch.
    from . import browser  # noqa: WPS433 (intentional lazy import)

    async with browser.persistent_context(headless=headless) as ctx:
        for draft in drafts:
            if draft.status != "pending":
                continue
            try:
                await _post_draft(ctx, listing, draft)
                draft.status = "posted"
            except Exception as e:  # pragma: no cover - live browser path
                draft.status = "error"
                draft.error = f"{type(e).__name__}: {str(e)[:200]}"
                print(f"[craigslist] {draft.subdomain} post failed: {draft.error}")
    return drafts


async def _post_draft(ctx, listing: CraigslistListing, draft: CityDraft) -> None:  # pragma: no cover
    """Live-publish one city draft of a `CraigslistListing` (CLI fan-out path)."""
    postal = listing.zip_code or CITY_ZIP.get(draft.subdomain)
    if not postal:
        raise ValueError(f"no ZIP for {draft.subdomain} — pass --zip-code or add it to CITY_ZIP")
    url, _id = await post_listing(
        ctx, subdomain=draft.subdomain, title=draft.title, body=draft.body,
        price=draft.price, postal=postal, city=draft.city_label,
        images=list(listing.images)[:CL_MAX_PHOTOS],
    )
    draft.detail_url = url


# ── Per-lot copy (inventory row → one post in the lot's own city) ───────────
@dataclass
class LotPost:
    """What one inventory row becomes on Craigslist. Pure data, no browser."""

    lot_id: str
    subdomain: str
    city: str
    title: str
    body: str
    price: int
    postal: str
    subarea_pref: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_lot_subdomain(city: str, state: str = "") -> str:
    """Strict city→subdomain for inventory rows: unknown city raises so the
    sync loop records a visible `error` row instead of posting to a guessed
    subdomain (`resolve_subdomain`'s passthrough is for the CLI only)."""
    key = _norm(city)
    if not key:
        raise ValueError("inventory row has no city — cannot pick a Craigslist site")
    if key in CITY_SUBDOMAINS:
        return CITY_SUBDOMAINS[key]
    key_state = f"{key} {_norm(state)}".strip()
    if key_state in CITY_SUBDOMAINS:
        return CITY_SUBDOMAINS[key_state]
    raise ValueError(f"no Craigslist subdomain for city {city!r} — add it to CITY_SUBDOMAINS")


_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def lot_title(row: dict, city: str, st: str) -> str:
    """`<title> — <qty> Available, Stackable (<City>, <ST>)`, fitted to 70 chars.

    A trailing "(City, ST)" already on the inventory title is replaced, never
    doubled. When the full suffix doesn't fit, it shortens in steps (drop
    "Stackable", then the quantity) before trimming the base at a word break —
    the location is the one part a Craigslist reader needs in the title.
    """
    qty = int(row.get("quantity_remaining") or 0)
    base = _TRAILING_PAREN_RE.sub("", (row.get("title") or "").strip())
    if not base:
        base = f"{qty} {row.get('chair_type') or 'Chairs'}".strip()
    loc = ", ".join(x for x in (city, st) if x)
    suffixes = [f" — {qty} Available, Stackable ({loc})", f" — {qty} Available ({loc})", f" ({loc})"]
    for suffix in suffixes:
        if len(base) + len(suffix) <= CL_TITLE_MAX:
            return base + suffix
    room = CL_TITLE_MAX - len(suffixes[-1])
    cut = base[:room]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" —-,") + suffixes[-1]


def lot_body(row: dict, city: str) -> str:
    qty = int(row.get("quantity_remaining") or 0)
    price = int(float(row.get("price_per_chair") or 0))
    desc = (row.get("description") or row.get("subtitle") or "").strip()
    lines = []
    if desc:
        lines += [desc, ""]
    lines += [
        f"Quantity available: {qty}",
        f"${price} per chair. Bulk discounts on the full lot.",
        f"Local pickup in {city}. Delivery quotes available for larger orders.",
        "Ideal for churches, banquet halls, schools, and event venues.",
        "",
        "Photos, specs and all our lots: black-whole.com",
        "",
        "Reply with how many you need and whether you want pickup or delivery.",
    ]
    return "\n".join(lines)


def build_lot_post(row: dict, *, city: str | None = None) -> LotPost:
    """Inventory row → `LotPost`. `city` overrides the lot's own city (per-city
    fan-out); the ZIP then comes from `CITY_ZIP`, never from the row.

    Raises `ValueError` on an unknown city or a missing ZIP — never invents one.
    Never reads `storage_note` (the sync engine strips it anyway).
    """
    own_city = (row.get("city") or "").strip()
    target_city = (city or own_city).strip()
    st = state_abbr(row.get("state") or "") if _norm(target_city) == _norm(own_city) else ""
    subdomain = resolve_lot_subdomain(target_city, row.get("state") or "")
    label = city_label(subdomain) if not target_city else target_city
    postal = (row.get("zip_code") or "").strip() if _norm(target_city) == _norm(own_city) else ""
    postal = postal or CITY_ZIP.get(subdomain) or ""
    if not postal:
        raise ValueError(f"no ZIP for lot {row.get('lot_id')!r} in {target_city!r} — "
                         "set inventory.zip_code or add the city to CITY_ZIP")
    price = int(float(row.get("price_per_chair") or 0))
    if price <= 0:
        raise ValueError(f"lot {row.get('lot_id')!r} has no price_per_chair")
    return LotPost(
        lot_id=str(row.get("lot_id") or ""), subdomain=subdomain, city=label,
        title=lot_title(row, label, st), body=lot_body(row, label), price=price,
        postal=postal, subarea_pref=SUBAREA_PREF.get(subdomain),
    )


def lot_photo_urls(row: dict) -> list[str]:
    """Hero first, then the gallery, deduped, capped at `CL_MAX_PHOTOS`."""
    hero = row.get("hero_image_url")
    urls = ([hero] if hero else []) + [u for u in (row.get("image_urls") or []) if u and u != hero]
    return urls[:CL_MAX_PHOTOS]


def download_lot_photos(row: dict, *, log=print) -> list[Path]:
    """Local copies of the lot's public (already disguised) R2 photos under
    `SCRATCH_DIR/craigslist/<key>/NN.jpg`. A file already on disk is reused."""
    import httpx

    from . import config, listing_images
    from .lot_channels import DOWNLOAD_HEADERS

    key = listing_images.key_base(row.get("lot_id")) or "lot"
    folder = Path(config.SCRATCH_DIR) / "craigslist" / key
    folder.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    urls = lot_photo_urls(row)
    with httpx.Client(timeout=60.0, follow_redirects=True, headers=DOWNLOAD_HEADERS) as client:
        for i, url in enumerate(urls):
            target = folder / f"{i:02d}.jpg"
            if target.is_file() and target.stat().st_size > 0:
                files.append(target)
                continue
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001 — one bad photo isn't fatal
                log(f"[craigslist] photo download failed ({type(exc).__name__}): {url[:90]}")
                continue
            if resp.content:
                target.write_bytes(resp.content)
                files.append(target)
    return files


# ── The live flow (recorded 2026-10-03) ─────────────────────────────────────
# Steps are keyed by the `?s=` query param so a city without sub-areas (which
# skips ?s=subarea) walks the same loop. Every step submits and then waits for
# the URL to change; an unknown step raises so nothing is guessed.

_LOGIN_MARKERS = ("accounts.craigslist.org/login", "/login/home?rp=")
NOT_LOGGED_IN = ("Craigslist profile not logged in — run run.py --login-only and sign in at "
                 "accounts.craigslist.org")
# Seen live after the 5th post of the day across 5 cities: CL parks the draft
# at `?s=pn` and wants a one-time SMS/voice code. That is the operator's phone —
# never automated. The draft survives in the account; re-run after verifying.
PHONE_VERIFY_NEEDED = ("Craigslist asks for phone verification (?s=pn) — verify once in the "
                       "profile's Chrome (accounts.craigslist.org → drafts), then re-run")


def _step_of(url: str) -> str | None:
    m = re.search(r"[?&]s=([a-z]+)", url)
    return m.group(1) if m else None


def _assert_logged_in(page) -> None:  # pragma: no cover - live browser path
    if any(mark in page.url for mark in _LOGIN_MARKERS):
        raise RuntimeError(NOT_LOGGED_IN)


async def _submit_and_wait(page, submit_selector: str, timeout: int = 20000) -> None:  # pragma: no cover
    before = page.url
    await page.click(submit_selector, timeout=timeout)
    try:
        await page.wait_for_url(lambda u: u != before, timeout=timeout)
    except Exception:
        # Extension-style clicks sometimes don't register on CL's radios; a DOM
        # submit from inside the page always did.
        await page.evaluate("sel => document.querySelector(sel).click()", submit_selector)
        await page.wait_for_url(lambda u: u != before, timeout=timeout)
    await page.wait_for_load_state("domcontentloaded")


async def _check_radio_and_submit(page, radio_selector: str, submit_selector: str = "button[type=submit]") -> None:  # pragma: no cover
    radio = page.locator(radio_selector).first
    await radio.check(timeout=10000)
    if not await radio.is_checked():
        await page.evaluate("sel => { document.querySelector(sel).checked = true }", radio_selector)
    await _submit_and_wait(page, submit_selector)


async def _pick_radio_by_label(page, wanted: str | None) -> str:  # pragma: no cover
    """Radio selector for the option whose label contains `wanted` (first option when None/no match)."""
    options = await page.evaluate(
        "() => [...document.querySelectorAll('input[type=radio]')]"
        ".map(r => [r.value, (r.parentElement?.innerText || '').trim().toLowerCase()])"
    )
    if not options:
        raise RuntimeError(f"no radio options on {page.url}")
    value = options[0][0]
    if wanted:
        w = wanted.lower()
        for val, label in options:
            if w in label:
                value = val
                break
    return f'input[type=radio][value="{value}"]'


async def _radio_by_exact_label(page, label: str) -> str:  # pragma: no cover
    options = await page.evaluate(
        "() => [...document.querySelectorAll('input[type=radio]')]"
        ".map(r => [r.value, (r.parentElement?.innerText || '').trim().toLowerCase()])"
    )
    for val, text in options:
        if text == label.lower():
            return f'input[type=radio][value="{val}"]'
    raise RuntimeError(f"category {label!r} not offered on {page.url}")


async def _fill_details(page, *, title: str, body: str, price: int, postal: str, city: str) -> None:  # pragma: no cover
    await page.fill('[name="PostingTitle"]', title[:CL_TITLE_MAX])
    await page.fill('[name="price"]', str(int(price)))
    await page.fill('[name="geographic_area"]', city)
    await page.fill('[name="postal"]', postal)
    await page.fill('[name="PostingBody"]', body)
    for name, value in (("condition", CL_CONDITION_GOOD), ("language", CL_LANGUAGE_ENGLISH)):
        try:
            await page.select_option(f'[name="{name}"]', value)
        except Exception:
            pass
    for name in ("delivery_available", "see_my_other"):
        try:
            await page.check(f'[name="{name}"]', timeout=3000)
        except Exception:
            pass
    # Never: show_address_ok, contact_phone — the pickup address stays private.
    await _submit_and_wait(page, 'button[name="go"]')


async def _upload_images(page, images: list[Path], timeout_s: int = 90) -> None:  # pragma: no cover
    import asyncio

    paths = [str(p) for p in images if Path(p).is_file()]
    if paths:
        await page.set_input_files("input[type=file]", paths, timeout=15000)
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            text = await page.inner_text("body")
            m = re.search(r"this posting has (\d+) images", text)
            if m and int(m.group(1)) >= len(paths) and "cancel uploads" not in text:
                break
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(f"image upload did not finish in {timeout_s}s "
                                   f"({m.group(1) if m else 0}/{len(paths)})")
            await asyncio.sleep(1.0)
    await _submit_and_wait(page, 'button:has-text("done with images")')


async def _confirmation_url(page, subdomain: str) -> tuple[str, str]:  # pragma: no cover
    hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    for href in hrefs:
        m = _POST_URL_RE.match(href)
        if m and m.group(1) == subdomain:
            return href, m.group(2)
    for href in hrefs:  # a city whose post host differs from the entry code
        m = _POST_URL_RE.match(href)
        if m:
            return href, m.group(2)
    raise RuntimeError(f"published but no post URL on the confirmation page ({page.url})")


async def post_listing(
    ctx, *, subdomain: str, title: str, body: str, price: int, postal: str, city: str,
    images: list[Path], subarea_pref: str | None = None, max_steps: int = 14,
) -> tuple[str, str]:  # pragma: no cover - live browser path
    """Publish one furniture-by-owner post. Returns `(post_url, external_id)`.

    Steps (all recorded live): /c/<code> → [copyfromanother: skip] → [area] →
    [subarea] → type=fso → cat "furniture" → details → map (city + ZIP only,
    no street) → images → preview → publish → confirmation. Raises on a login
    page, the phone-verification step, an unexpected step, or a missing
    confirmation link — never guesses.
    """
    subarea_pref = subarea_pref if subarea_pref is not None else SUBAREA_PREF.get(subdomain)
    page = await ctx.new_page()
    try:
        code = CL_POST_CODES.get(subdomain, subdomain)
        await page.goto(f"{CL_POST_BASE}/c/{code}", wait_until="domcontentloaded")
        _assert_logged_in(page)
        if _step_of(page.url) is None:
            # Not a post step — go in through the city home page's "create a posting".
            await page.goto(post_url_for(subdomain), wait_until="domcontentloaded")
            await page.get_by_text("create a posting", exact=False).first.click(timeout=10000)
            await page.wait_for_load_state("domcontentloaded")
            _assert_logged_in(page)

        for _ in range(max_steps):
            step = _step_of(page.url)
            if step == "copyfromanother":
                # "Re-use selected data from your previous posting?" — never; each
                # lot gets its own copy. `brand_new_post` = the "skip" button.
                await _submit_and_wait(page, 'button[name="brand_new_post"]')
            elif step == "area":
                # Single <select name=n> already on the right city — just continue.
                await _submit_and_wait(page, 'button[name="go"]')
            elif step == "pn":
                raise RuntimeError(PHONE_VERIFY_NEEDED)
            elif step == "subarea":
                await _check_radio_and_submit(page, await _pick_radio_by_label(page, subarea_pref))
            elif step == "type":
                await _check_radio_and_submit(page, f'input[type=radio][value="{CL_TYPE_FOR_SALE_BY_OWNER}"]')
            elif step == "cat":
                await _check_radio_and_submit(page, await _radio_by_exact_label(page, CL_CATEGORY_LABEL))
            elif step == "edit":
                await _fill_details(page, title=title, body=body, price=price, postal=postal, city=city)
            elif step == "geoverify":
                await page.fill('[name="city"]', city)
                await page.fill('[name="postal"]', postal)
                await _submit_and_wait(page, 'button:has-text("continue")')
            elif step == "editimage":
                await _upload_images(page, images)
            elif step == "preview":
                await _submit_and_wait(page, 'button[name="go"]')
            elif step is None:
                _assert_logged_in(page)
                return await _confirmation_url(page, subdomain)
            else:
                raise RuntimeError(f"unexpected Craigslist step {step!r} at {page.url}")
        raise RuntimeError(f"Craigslist flow did not finish in {max_steps} steps (at {page.url})")
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _manage_action(ctx, external_id: str, action: str) -> bool:  # pragma: no cover - live browser path
    """UNVERIFIED SELECTORS — check on the first live run. The manage page
    (`/manage/<id>`) carries delete / renew / edit controls; this clicks the one
    named `action` and, if a confirmation control of the same name appears,
    clicks that too. Returns True when a control was clicked."""
    page = await ctx.new_page()
    try:
        await page.goto(f"{CL_POST_BASE}/manage/{external_id}", wait_until="domcontentloaded")
        _assert_logged_in(page)
        selector = (f'button:has-text("{action}"), input[type=submit][value*="{action}" i], '
                    f'a:has-text("{action}")')
        clicked = False
        for _ in range(2):
            loc = page.locator(selector).first
            if await loc.count() == 0:
                break
            before = page.url
            await loc.click(timeout=10000)
            clicked = True
            try:
                await page.wait_for_url(lambda u: u != before, timeout=10000)
            except Exception:
                await page.wait_for_timeout(1500)
        return clicked
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def delete_posting(ctx, external_id: str) -> bool:  # pragma: no cover
    """Delete a live post from its manage page. UNVERIFIED — see `_manage_action`."""
    return await _manage_action(ctx, external_id, "delete")


async def renew_posting(ctx, external_id: str) -> bool:  # pragma: no cover
    """Renew (bump) a live post. CL allows it 48 h after posting, within the
    45-day window. UNVERIFIED — see `_manage_action`."""
    return await _manage_action(ctx, external_id, "renew")
