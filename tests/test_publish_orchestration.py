"""Tests for the multi-platform publish orchestration (BLACKWHOLE-21).

No live publishing: every test is dry-run or monkeypatches the underlying
`create_draft` calls. A fake Craigslist adapter stands in for BLACKWHOLE-20 to
prove the registration point works.
"""
from pathlib import Path

import pytest

from automation.publish import orchestrator, registry
from automation.publish.models import (
    ListingData,
    PublishRequest,
    PublishResult,
)


@pytest.fixture
def reg():
    """Ensure built-ins are loaded; remove any test-added adapters after."""
    registry.load_builtin()
    before = set(registry.available())
    yield registry
    for platform in set(registry.available()) - before:
        registry.unregister(platform)


@pytest.fixture
def sample_data():
    return ListingData(
        title="Stackable Banquet Chairs",
        chair_type="Banquet Chairs",
        location="Phoenix, AZ",
        city="Phoenix",
        state="AZ",
        zip_code="85001",
        quantity="120",
        dimensions='18"x18"x32"',
        style_suffix="Bulk Lot",
        price_per_chair=15,
        lot_id="LOT-999",
        images=[Path("/tmp/a.jpg")],
        description_text="Great condition.",
    )


# ── registry ────────────────────────────────────────────────────────────────

def test_load_builtin_registers_reference_adapters(reg):
    assert "fb" in reg.available()
    assert "ebay" in reg.available()


def test_aliases_resolve_to_canonical_adapter(reg):
    assert reg.get("facebook").platform == "fb"
    assert reg.get("fb") is reg.get("facebook")


def test_get_unknown_platform_returns_none(reg):
    assert reg.get("myspace") is None


def test_reference_adapter_per_city_flags(reg):
    assert reg.get("fb").per_city is True       # FB is city-scoped
    assert reg.get("ebay").per_city is False     # eBay is national


def test_register_requires_platform(reg):
    class Bad:
        platform = ""
    with pytest.raises(ValueError):
        reg.register(Bad())


def test_live_publishing_disabled_by_default(reg, monkeypatch):
    monkeypatch.delenv("LISTING_PUBLISH_LIVE", raising=False)
    assert reg.live_publishing_enabled() is False


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False),
])
def test_live_publishing_env_gate(reg, monkeypatch, val, expected):
    monkeypatch.setenv("LISTING_PUBLISH_LIVE", val)
    assert reg.live_publishing_enabled() is expected


def test_load_builtin_tolerates_broken_extra_adapter(reg, monkeypatch, capsys):
    monkeypatch.setenv("LISTING_PUBLISH_ADAPTERS", "automation.publish._does_not_exist")
    # A missing/broken adapter must not raise or drop the reference adapters.
    names = reg.load_builtin(force=True)
    assert "fb" in names and "ebay" in names
    assert "skipped adapter" in capsys.readouterr().out


# ── orchestrator fan-out ──────────────────────────────────────────────────────

async def test_dry_run_by_default_creates_no_real_drafts(reg, sample_data, monkeypatch):
    # If any adapter reached the live path, these would fire and fail the test.
    from automation import ebay, facebook

    async def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("live create_draft called during dry-run")

    monkeypatch.setattr(facebook, "create_draft", _boom)
    monkeypatch.setattr(ebay, "create_draft", _boom)
    monkeypatch.delenv("LISTING_PUBLISH_LIVE", raising=False)

    results = await orchestrator.publish_all(
        ctx=None, data=sample_data, platforms=["fb", "ebay"], cities=["phx"],
    )
    assert {r.status for r in results} == {"dry_run"}
    assert all(r.url is None for r in results)


async def test_fanout_city_scoped_vs_national(reg, sample_data):
    results = await orchestrator.publish_all(
        ctx=None, data=sample_data,
        platforms=["fb", "ebay"], cities=["phx", "atl", "la"], dry_run=True,
    )
    fb = [r for r in results if r.platform == "fb"]
    ebay = [r for r in results if r.platform == "ebay"]
    # FB (per_city) -> one per city; eBay (national) -> exactly one.
    assert sorted(r.city for r in fb) == ["atl", "la", "phx"]
    assert len(ebay) == 1


async def test_unknown_platform_degrades_to_no_adapter(reg, sample_data):
    results = await orchestrator.publish_all(
        ctx=None, data=sample_data, platforms=["cl"], cities=["phx"], dry_run=True,
    )
    assert len(results) == 1
    assert results[0].status == "no_adapter"
    assert results[0].ok is False


async def test_platforms_are_deduped_and_normalized(reg, sample_data):
    results = await orchestrator.publish_all(
        ctx=None, data=sample_data, platforms=["FB", "fb", " ebay "], cities=["phx"],
        dry_run=True,
    )
    platforms = {r.platform for r in results}
    assert platforms == {"fb", "ebay"}


async def test_no_cities_falls_back_to_lot_city(reg, sample_data):
    results = await orchestrator.publish_all(
        ctx=None, data=sample_data, platforms=["fb"], cities=None, dry_run=True,
    )
    assert [r.city for r in results] == ["Phoenix"]


async def test_duplicate_is_skipped_unless_forced(reg, sample_data):
    published = {"fb": "https://facebook.com/marketplace/item/existing"}
    results = await orchestrator.publish_all(
        ctx=None, data=sample_data, platforms=["fb"], cities=["phx", "atl"],
        dry_run=True, published=published,
    )
    assert len(results) == 1
    assert results[0].status == "skipped_duplicate"
    assert results[0].url.endswith("existing")

    forced = await orchestrator.publish_all(
        ctx=None, data=sample_data, platforms=["fb"], cities=["phx", "atl"],
        dry_run=True, published=published, force_republish=True,
    )
    assert {r.status for r in forced} == {"dry_run"}
    assert len(forced) == 2


async def test_adapter_exception_becomes_error_result(reg, sample_data):
    class Exploding:
        platform = "boom"
        per_city = True
        async def publish(self, ctx, request):
            raise RuntimeError("kaboom")

    reg.register(Exploding())
    results = await orchestrator.publish_all(
        ctx=None, data=sample_data, platforms=["boom", "ebay"], cities=["phx"],
        dry_run=True,
    )
    boom = next(r for r in results if r.platform == "boom")
    ebay = next(r for r in results if r.platform == "ebay")
    assert boom.status == "error" and "kaboom" in boom.error
    assert ebay.status == "dry_run"  # sibling still ran


# ── registration point for BLACKWHOLE-20 (Craigslist) ─────────────────────────

async def test_craigslist_registration_point(reg, sample_data):
    """Simulate BLACKWHOLE-20 dropping in its adapter; orchestrator finds it."""
    class FakeCraigslist:
        platform = "craigslist"
        aliases = ("cl",)
        per_city = True
        def __init__(self):
            self.cities_seen = []
        async def publish(self, ctx, request):
            self.cities_seen.append(request.city)
            return PublishResult(
                platform="craigslist", city=request.city, status="dry_run",
            )

    cl = FakeCraigslist()
    reg.register(cl)

    results = await orchestrator.publish_all(
        ctx=None, data=sample_data,
        platforms=["fb", "ebay", "cl"], cities=["phx", "atl"], dry_run=True,
    )
    by_platform = {}
    for r in results:
        by_platform.setdefault(r.platform, []).append(r.city)
    assert sorted(by_platform["fb"]) == ["atl", "phx"]
    assert len(by_platform["ebay"]) == 1
    assert sorted(by_platform["craigslist"]) == ["atl", "phx"]
    assert sorted(cl.cities_seen) == ["atl", "phx"]  # alias "cl" routed here


# ── live-mode adapter mapping (monkeypatched, still no real network) ──────────

async def test_facebook_adapter_maps_fields_in_live_mode(reg, sample_data, monkeypatch):
    from automation import facebook

    captured = {}
    async def fake_create_draft(**kwargs):
        captured.update(kwargs)
        return ("https://facebook.com/marketplace/item/123", "rendered body")

    monkeypatch.setattr(facebook, "create_draft", fake_create_draft)
    adapter = reg.get("fb")
    req = PublishRequest(data=sample_data, city="Atlanta", dry_run=False)
    result = await adapter.publish(ctx=object(), request=req)

    assert result.status == "draft"
    assert result.url.endswith("123")
    # city comes from the fan-out request, not the lot's source city
    assert captured["city"] == "Atlanta"
    assert captured["chair_type"] == "Banquet Chairs"
    assert captured["price_per_chair"] == 15
    assert captured["sku"] == "LOT-999"


async def test_ebay_adapter_maps_fields_in_live_mode(reg, sample_data, monkeypatch):
    from automation import ebay

    captured = {}
    async def fake_create_draft(**kwargs):
        captured.update(kwargs)
        return "https://ebay.com/itm/456"

    monkeypatch.setattr(ebay, "create_draft", fake_create_draft)
    adapter = reg.get("ebay")
    req = PublishRequest(data=sample_data, city="Phoenix", dry_run=False)
    result = await adapter.publish(ctx=object(), request=req)

    assert result.status == "draft"
    assert result.url.endswith("456")
    assert captured["price_each"] == 15
    assert captured["lot_id"] == "LOT-999"
    assert captured["quantity"] == "120"


# ── models ────────────────────────────────────────────────────────────────────

def test_build_listing_data_maps_extraction_and_meta():
    class P:
        title = "T"; chair_type = "Chairs"; location = "Phoenix, AZ"
        city = "Phoenix"; state = "AZ"; zip_code = "85001"
        quantity = "50"; dimensions = "18x18"; style_suffix = "Bulk"
        description_text = "desc"
    class M:
        city = "ignored"; state = "ignored"; zip_code = "00000"; lot_id = "LOT-1"
    data = orchestrator.build_listing_data(P(), M(), price_per_chair=20, images=[Path("/x.jpg")])
    assert data.city == "Phoenix"          # primary wins over meta
    assert data.lot_id == "LOT-1"           # meta supplies lot id
    assert data.price_per_chair == 20
    assert data.images == [Path("/x.jpg")]


def test_build_listing_data_falls_back_to_meta_when_primary_blank():
    class P:
        title = "T"; chair_type = "Chairs"; location = ""
        city = ""; state = ""; zip_code = ""
        quantity = "50"; dimensions = ""; style_suffix = ""
        description_text = ""
    class M:
        city = "Atlanta"; state = "GA"; zip_code = "30301"; lot_id = "LOT-2"
    data = orchestrator.build_listing_data(P(), M(), price_per_chair=10, images=[])
    assert data.city == "Atlanta" and data.state == "GA" and data.zip_code == "30301"


def test_publish_result_ok_semantics():
    assert PublishResult("fb", "phx", "draft").ok is True
    assert PublishResult("fb", "phx", "dry_run").ok is True
    assert PublishResult("fb", "phx", "skipped_duplicate").ok is True
    assert PublishResult("fb", "phx", "error", error="x").ok is False
    assert PublishResult("cl", "*", "no_adapter").ok is False
