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

Even in that "live" mode the browser flow stops at Craigslist's preview/review
step and NEVER clicks the final publish button — a human confirms every post.
Tests exercise the pure logic only (city resolution, copy variation, draft
building, dry-run orchestration) and never open a browser or hit the network.
"""

from __future__ import annotations

import inspect
import os
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
}

# Craigslist post-flow selectors used only on the LIVE path. "for sale by owner"
# main category, then "furniture - by owner". Kept as constants so a live
# operator can tweak them without hunting through the flow code.
CL_MAINCAT_FOR_SALE_BY_OWNER = "for sale by owner"
CL_CATEGORY_FURNITURE_BY_OWNER = "furniture - by owner"


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
      prepared -> live browser filled the form and stopped at review (no publish)
      posted   -> operator published (only ever set by external confirmation)
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
    requires BOTH ``dry_run=False`` AND ``CRAIGSLIST_LIVE=1``; even then it
    fills the form and stops at Craigslist's review step (never auto-publishes).
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
                await _prepare_post(ctx, listing, draft)
                draft.status = "prepared"
            except Exception as e:  # pragma: no cover - live browser path
                draft.status = "error"
                draft.error = f"{type(e).__name__}: {str(e)[:200]}"
                print(f"[craigslist] {draft.subdomain} prepare failed: {draft.error}")
    return drafts


async def _prepare_post(ctx, listing: CraigslistListing, draft: CityDraft) -> None:  # pragma: no cover
    """Best-effort live fill of the Craigslist post form — STOPS before publish.

    Not exercised by the test suite (no live posting in CI). Selectors are
    Craigslist's current post-flow labels; a live operator should verify them
    before the first real run. This function intentionally advances only as far
    as the preview/review page and never clicks the final "publish" control.
    """
    page = await ctx.new_page()
    await page.goto(draft.post_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    async def _click_text(text: str, timeout: int = 5000) -> None:
        await page.get_by_text(text, exact=False).first.click(timeout=timeout)

    # 1) create posting -> for sale by owner -> furniture by owner
    try:
        await _click_text("create a posting")
        await page.wait_for_timeout(1000)
        await _click_text(CL_MAINCAT_FOR_SALE_BY_OWNER)
        await page.wait_for_timeout(1000)
        await _click_text(CL_CATEGORY_FURNITURE_BY_OWNER)
        await page.wait_for_timeout(1500)
    except Exception as e:
        print(f"[craigslist] category nav fallback ({draft.subdomain}): {e}")

    # 2) core fields
    async def _fill(label: str, value: str) -> None:
        el = page.get_by_label(label, exact=False).first
        await el.click()
        await el.fill(value)

    for label in ("posting title", "Posting Title", "PostingTitle"):
        try:
            await _fill(label, draft.title)
            break
        except Exception:
            continue
    try:
        await _fill("price", str(listing.price))
    except Exception:
        pass
    if listing.zip_code:
        for label in ("postal code", "zip code", "postal"):
            try:
                await _fill(label, listing.zip_code)
                break
            except Exception:
                continue
    for label in ("posting body", "Posting Body", "PostingBody"):
        try:
            await _fill(label, draft.body)
            break
        except Exception:
            continue
    if listing.contact_email:
        for label in ("email", "reply email"):
            try:
                await _fill(label, listing.contact_email)
                break
            except Exception:
                continue

    # 3) advance to image upload + preview — but STOP. No publish click here.
    try:
        await page.get_by_role("button", name="continue").first.click()
        await page.wait_for_timeout(1500)
    except Exception:
        pass

    file_input = await page.query_selector("input[type=file]")
    if file_input and listing.images:
        try:
            await file_input.set_input_files([str(p) for p in listing.images])
            await page.wait_for_timeout(2000)
        except Exception as e:
            print(f"[craigslist] image upload fallback ({draft.subdomain}): {e}")

    draft.detail_url = page.url
    print(f"[craigslist] {draft.subdomain}: form prepared, awaiting human review "
          f"at {page.url} — NOT auto-published.")
