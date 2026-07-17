"""Craigslist multi-city cross-posting (BLACKWHOLE-20).

Pure-logic + dry-run coverage only: no browser, no network, no DB, no LLM
calls. The live path is double-gated (dry_run=False AND CRAIGSLIST_LIVE=1) and
these tests assert the gate holds — a fake ``persistent_context`` blows up if it
is ever opened when it shouldn't be.
"""

from contextlib import asynccontextmanager

import pytest
from click.testing import CliRunner

from automation import browser, craigslist
from automation.craigslist import (
    CraigslistListing,
    CityDraft,
    build_city_draft,
    city_label,
    cross_post,
    deterministic_varier,
    post_url_for,
    resolve_subdomain,
)
from automation.craigslist_cli import main as cli_main


def _listing(**kw) -> CraigslistListing:
    base = dict(
        chair_type="Tan Metal Folding Chairs",
        quantity="240",
        price=15,
        description_text="Used, good condition with normal wear from prior service.",
        dimensions='18" seat, 32" tall',
        state="Nevada",
        zip_code="89191",
    )
    base.update(kw)
    return CraigslistListing(**base)


# ── city resolution ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("phoenix", "phoenix"),
    ("PHX", "phoenix"),
    ("  Arizona ", "phoenix"),
    ("ga", "atlanta"),
    ("Georgia", "atlanta"),
    ("Los Angeles", "losangeles"),
    ("LA", "losangeles"),
    ("midwest", "chicago"),
    ("DC", "washingtondc"),
    ("Washington DC", "washingtondc"),
    ("sacramento", "sacramento"),          # passthrough
    ("San Diego", "sandiego"),             # passthrough, spaces stripped
])
def test_resolve_subdomain(raw, expected):
    assert resolve_subdomain(raw) == expected


def test_resolve_subdomain_rejects_blank():
    with pytest.raises(ValueError):
        resolve_subdomain("   ")


def test_city_label_and_post_url():
    assert city_label("losangeles") == "Los Angeles"
    assert city_label("sacramento") == "Sacramento"   # title-cased fallback
    assert post_url_for("phoenix") == "https://phoenix.craigslist.org/"


# ── copy variation ──────────────────────────────────────────────────────────
def test_deterministic_varier_stamps_city_and_facts():
    title, body = deterministic_varier(_listing(), "phoenix", "Phoenix")
    assert "Phoenix" in title
    assert "Tan Metal Folding Chairs" in title
    assert "Phoenix" in body
    assert "240" in body            # quantity
    assert "$15" in body            # price
    assert '18" seat, 32" tall' in body


def test_deterministic_varier_avoids_auction_language():
    _, body = deterministic_varier(_listing(), "atlanta", "Atlanta")
    lowered = body.lower()
    for banned in ("bid", "auction", "as-is", "lot #"):
        assert banned not in lowered


def test_copy_differs_across_cities():
    """Craigslist ghosts byte-identical posts — every city must read differently."""
    t1, b1 = deterministic_varier(_listing(), "phoenix", "Phoenix")
    t2, b2 = deterministic_varier(_listing(), "atlanta", "Atlanta")
    t3, b3 = deterministic_varier(_listing(), "losangeles", "Los Angeles")
    assert len({t1, t2, t3}) == 3
    assert len({b1, b2, b3}) == 3


def test_deterministic_varier_is_stable():
    a = deterministic_varier(_listing(), "chicago", "Chicago")
    b = deterministic_varier(_listing(), "chicago", "Chicago")
    assert a == b


# ── draft building ──────────────────────────────────────────────────────────
async def test_build_city_draft():
    draft = await build_city_draft(_listing(), "phx")
    assert isinstance(draft, CityDraft)
    assert draft.subdomain == "phoenix"
    assert draft.post_url == "https://phoenix.craigslist.org/"
    assert draft.status == "pending"
    assert "Phoenix" in draft.title
    assert draft.price == 15


async def test_build_city_draft_skips_blank():
    draft = await build_city_draft(_listing(), "   ")
    assert draft.status == "skipped"
    assert draft.subdomain == ""
    assert draft.error


async def test_build_city_draft_accepts_async_varier():
    async def fake_llm(listing, subdomain, label):
        return (f"CUSTOM {label}", f"custom body for {label}")

    draft = await build_city_draft(_listing(), "atlanta", varier=fake_llm)
    assert draft.title == "CUSTOM Atlanta"
    assert draft.body == "custom body for Atlanta"


# ── dry-run orchestration + live gate ───────────────────────────────────────
@asynccontextmanager
async def _exploding_context(*a, **kw):
    raise AssertionError("browser.persistent_context opened during a dry run!")
    yield  # pragma: no cover


async def test_cross_post_dry_run_default_never_opens_browser(monkeypatch):
    monkeypatch.setattr(browser, "persistent_context", _exploding_context)
    drafts = await cross_post(_listing(), ["phoenix", "atlanta", "losangeles"])
    assert [d.subdomain for d in drafts] == ["phoenix", "atlanta", "losangeles"]
    assert all(d.status == "dry_run" for d in drafts)


async def test_cross_post_live_flag_without_env_gate_stays_dry(monkeypatch):
    """dry_run=False alone is NOT enough — CRAIGSLIST_LIVE must also be set."""
    monkeypatch.delenv("CRAIGSLIST_LIVE", raising=False)
    monkeypatch.setattr(browser, "persistent_context", _exploding_context)
    drafts = await cross_post(_listing(), ["phoenix"], dry_run=False)
    assert all(d.status == "dry_run" for d in drafts)


async def test_cross_post_live_gate_opens_browser_once_and_stops_at_review(monkeypatch):
    monkeypatch.setenv("CRAIGSLIST_LIVE", "1")
    opened = {"count": 0}

    @asynccontextmanager
    async def fake_ctx(*a, **kw):
        opened["count"] += 1
        yield object()  # a stand-in BrowserContext; _prepare_post is stubbed out

    async def fake_prepare(ctx, listing, draft):
        draft.detail_url = f"https://{draft.subdomain}.craigslist.org/preview/123"

    monkeypatch.setattr(browser, "persistent_context", fake_ctx)
    monkeypatch.setattr(craigslist, "_prepare_post", fake_prepare)

    drafts = await cross_post(_listing(), ["phoenix", "atlanta"], dry_run=False)
    assert opened["count"] == 1                       # one shared browser for the batch
    assert all(d.status == "prepared" for d in drafts)   # never auto-published
    assert all(d.detail_url.endswith("/preview/123") for d in drafts)


async def test_cross_post_records_skipped_city(monkeypatch):
    monkeypatch.setattr(browser, "persistent_context", _exploding_context)
    drafts = await cross_post(_listing(), ["phoenix", "  "])
    assert drafts[0].status == "dry_run"
    assert drafts[1].status == "skipped"


async def test_cross_post_use_llm_falls_back_without_key(monkeypatch):
    """use_llm=True with no GEMINI_API_KEY must degrade to deterministic copy, not crash."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("automation.config.GEMINI_API_KEY", None, raising=False)
    monkeypatch.setattr(browser, "persistent_context", _exploding_context)
    drafts = await cross_post(_listing(), ["phoenix"], use_llm=True)
    assert drafts[0].status == "dry_run"
    assert "Phoenix" in drafts[0].title


# ── CLI ─────────────────────────────────────────────────────────────────────
def test_cli_dry_run_lists_all_cities(monkeypatch):
    monkeypatch.setattr(browser, "persistent_context", _exploding_context)
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "--cities", "phoenix,atlanta,losangeles",
        "--chair-type", "Banquet Chairs",
        "--quantity", "300",
        "--price", "20",
    ])
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "Phoenix" in result.output
    assert "Atlanta" in result.output
    assert "Los Angeles" in result.output
    assert "3/3 city drafts ready" in result.output
    assert "No posts submitted" in result.output


def test_cli_requires_cities():
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--chair-type", "x", "--quantity", "1", "--price", "1"])
    assert result.exit_code != 0
