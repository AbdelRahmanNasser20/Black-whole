"""Wave 4 storefront UI: does the markup actually render, in both states?

Same offline shape as ``tests/web/test_reserve.py`` — ``TestClient`` built
WITHOUT a ``with`` block so the lifespan never runs, and every DB call site
monkeypatched. These are Jinja smoke tests: they prove each template compiles
and that the two feature gates (``freight.enabled``, ``reserve_enabled``)
actually change what a buyer sees, rather than re-testing the endpoints those
templates call (covered in test_freight_endpoint.py / test_reserve.py).
"""
import sys

import pytest
from fastapi.testclient import TestClient

from automation import config as config_mod
from automation import deposits, inventory, site_settings, stripe_gateway
from automation.web.app import app

app_module = sys.modules["automation.web.app"]


@pytest.fixture(autouse=True)
def _dark_by_default(monkeypatch):
    monkeypatch.setattr(config_mod, "STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(config_mod, "STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        site_settings, "get_all",
        lambda: {"deposit_pct": 0.15, "deposit_min_usd": 200},
    )
    # get_public() goes straight to the DB once the slug column exists; keep
    # it on the patched inventory.get path so no test reads prod.
    monkeypatch.setattr(inventory, "has_slug_column", lambda: False)


def _lit(monkeypatch):
    monkeypatch.setattr(config_mod, "STRIPE_SECRET_KEY", "sk_test_unit")


def _client():
    return TestClient(app, base_url="https://testserver")


def make_lot(**over) -> dict:
    lot = {
        "lot_id": "31225",
        "title": "Mity-Lite folding chairs",
        "status": "listed",
        "price_per_chair": 12,
        "quantity_remaining": 400,
        "quantity_original": 945,
        "city": "Boise",
        "state": "ID",
        "zip_code": "83702",
        "hero_image_url": "https://cdn.example/31225.jpg",
        "image_urls": ["https://cdn.example/31225/00.jpg"],
    }
    lot.update(over)
    return lot


@pytest.fixture
def lot(monkeypatch):
    row = make_lot()
    monkeypatch.setattr(
        inventory, "get",
        lambda lot_id: dict(row) if lot_id == row["lot_id"] else None,
    )
    return row


# ───────────────────────── lot detail — freight widget ──────────────────────

def test_freight_widget_renders_on_a_locatable_lot(lot):
    r = _client().get("/listings/31225")
    assert r.status_code == 200
    assert "FREIGHT TO YOUR ZIP" in r.text
    assert 'id="freight-widget"' in r.text
    assert 'data-lot-id="31225"' in r.text
    assert 'data-default-qty="400"' in r.text
    assert "ESTIMATE — FINALIZED AT SHIP TIME" in r.text
    assert "LOCAL PICKUP ALWAYS FREE" in r.text
    # The plain fallback row must NOT also be there.
    assert "freight quoted on request" not in r.text
    # One step: ZIP, chairs, email and phone are all asked BEFORE the price.
    widget = r.text.split('id="freight-widget"')[1].split("fw-framing")[0]
    for field in ('name="dest_zip"', 'name="quantity"', 'name="email"', 'name="phone"'):
        assert field in widget, field
    assert widget.count("<form") == 1          # no second "email me this" step
    assert 'type="tel"' in widget and 'type="email"' in widget


@pytest.mark.parametrize(
    "patch", [{"zip_code": None, "state": None}, {"status": "sold_out"}]
)
def test_unlocatable_or_sold_lots_lose_the_widget(lot, patch):
    lot.update(patch)
    r = _client().get("/listings/31225")
    assert r.status_code == 200
    assert 'id="freight-widget"' not in r.text
    # A sold lot drops the pickup row entirely; an unlocatable live one keeps it.
    if patch.get("status") != "sold_out":
        assert "Local pickup free · freight quoted on request" in r.text


# ───────────────────────── lot detail — reserve CTA ─────────────────────────

def test_reserve_cta_replaces_the_lead_form_button_when_lit(monkeypatch, lot):
    _lit(monkeypatch)
    r = _client().get("/listings/31225")
    assert "RESERVE WITH DEPOSIT →" in r.text
    assert 'href="/reserve/31225"' in r.text
    assert "ASK A QUESTION" in r.text
    assert "I WANT THIS LOT" not in r.text


def test_dark_mode_keeps_the_original_cta(lot):
    r = _client().get("/listings/31225")
    assert "RESERVE WITH DEPOSIT" not in r.text
    assert "I WANT THIS LOT →" in r.text


@pytest.mark.parametrize(
    "patch",
    [{"price_per_chair": None}, {"quantity_remaining": 0}, {"status": "sold_out"}],
)
def test_unreservable_lots_keep_the_original_cta_even_when_lit(
    monkeypatch, lot, patch
):
    _lit(monkeypatch)
    lot.update(patch)
    r = _client().get("/listings/31225")
    assert "RESERVE WITH DEPOSIT" not in r.text
    assert ("FIND ME ONE LIKE THIS →" in r.text) or ("I WANT THIS LOT →" in r.text)


# ──────────────────────────── reserve page ──────────────────────────────────

def test_reserve_page_carries_the_form_contract(monkeypatch, lot):
    _lit(monkeypatch)
    r = _client().get("/reserve/31225")
    assert r.status_code == 200
    assert 'id="reserve-form"' in r.text
    assert 'data-endpoint="/reserve/31225/checkout"' in r.text
    assert 'data-pct="0.15"' in r.text
    assert 'data-min-cents="20000"' in r.text
    assert 'data-max-qty="400"' in r.text
    for el in ("quote-subtotal", "quote-due-now", "quote-balance"):
        assert f'id="{el}"' in r.text
    assert "CONTINUE TO SECURE PAYMENT →" in r.text
    assert 'href="/terms#deposits"' in r.text
    assert 'href="/listings/31225"' in r.text          # back-link to the lot


def test_reserve_page_shows_the_cancel_hook(monkeypatch, lot):
    """`canceled=1` is what Stripe's cancel_url sends back."""
    _lit(monkeypatch)
    assert 'data-canceled="1"' in _client().get("/reserve/31225?canceled=1").text
    assert 'data-canceled="0"' in _client().get("/reserve/31225").text


# ──────────────────────────── success page ──────────────────────────────────

def _deposit(**over) -> dict:
    row = {
        "id": 7, "lot_id": "31225", "kind": "deposit", "quantity": 30,
        "subtotal_cents": 300000, "amount_cents": 45000, "status": "paid",
    }
    row.update(over)
    return row


@pytest.mark.parametrize(
    "status,expected,absent",
    [
        ("paid", "DEPOSIT RECEIVED — LOT LOCKED", "BANK TRANSFER INITIATED"),
        ("processing", "BANK TRANSFER INITIATED", "LOT LOCKED"),
    ],
)
def test_success_page_states(monkeypatch, status, expected, absent):
    _lit(monkeypatch)
    monkeypatch.setattr(
        deposits, "get_by_session", lambda sid: _deposit(status=status)
    )
    r = _client().get("/reserve/success?session_id=cs_1")
    assert r.status_code == 200
    assert expected in r.text
    assert absent not in r.text
    assert "#7" in r.text                       # the reference
    assert "$450.00" in r.text                  # amount, in dollars
    assert "$2,550.00" in r.text                # balance at delivery
    assert 'href="/listings/31225"' in r.text


# ──────────────────────────────── terms ─────────────────────────────────────

def test_terms_page_anchors_and_policy():
    r = _client().get("/terms")
    assert r.status_code == 200
    assert 'id="deposits"' in r.text
    assert 'id="freight"' in r.text
    assert stripe_gateway.REFUND_POLICY_SHORT in r.text
    assert "Fully refundable" in r.text


def test_footer_links_the_terms_everywhere():
    assert 'href="/terms#deposits"' in _client().get("/terms").text


# ─────────────────────────────── privacy ────────────────────────────────────

def test_privacy_page_renders_with_contact_details(monkeypatch):
    monkeypatch.setattr(config_mod, "ALERTS_POSTAL_ADDRESS", "1 Test St, Phoenix, AZ 85054")
    monkeypatch.setattr(config_mod, "PUBLIC_CONTACT_PHONE", "(555) 010-0000")
    r = _client().get("/privacy")
    assert r.status_code == 200
    for anchor in ('id="collect"', 'id="use"', 'id="choices"'):
        assert anchor in r.text
    assert "We do not sell your information" in r.text
    assert "(555) 010-0000" in r.text
    assert "1 Test St, Phoenix, AZ 85054" in r.text


def test_privacy_page_hides_missing_contact_details(monkeypatch):
    monkeypatch.setattr(config_mod, "ALERTS_POSTAL_ADDRESS", "")
    monkeypatch.setattr(config_mod, "PUBLIC_CONTACT_PHONE", "")
    r = _client().get("/privacy")
    assert r.status_code == 200
    assert "By mail" not in r.text
    assert "call or" not in r.text


def test_footer_links_the_privacy_policy():
    assert 'href="/privacy"' in _client().get("/terms").text


def test_sitemap_lists_the_policy_pages(monkeypatch):
    monkeypatch.setattr(inventory, "list_public", lambda: [])
    monkeypatch.setattr(inventory, "list_sold_showcase", lambda: [])
    body = _client().get("/sitemap.xml").text
    assert "/privacy</loc>" in body and "/terms</loc>" in body


# ─────────────────────────────── landing copy ───────────────────────────────

@pytest.fixture
def landing(monkeypatch):
    monkeypatch.setattr(
        inventory, "stats",
        lambda: {"lots": 3, "chairs": 900, "cities": 2, "moved": 4900},
    )
    monkeypatch.setattr(inventory, "list_public", lambda: [])


def test_landing_copy_follows_the_gate(monkeypatch, landing):
    dark = _client().get("/")
    assert "Pay on collection — no deposit games." in dark.text
    _lit(monkeypatch)
    lit = _client().get("/")
    assert "Lock your lot with a refundable deposit" in lit.text
    assert "no deposit games" not in lit.text


def test_terms_returns_policy_section():
    t = _client().get("/terms").text
    assert 'id="returns"' in t
    for head in ("ALL SALES FINAL", "SEE THEM FIRST", "NOT AS DESCRIBED", "FREIGHT DAMAGE",
                 "BUYING WITHOUT SEEING", "HOLD PERIOD", "PAYMENT RETURNS", "ALREADY COVERED"):
        assert head in t
    assert "14 days" in t and "2 business days" in t
    assert 'href="/terms#returns"' in t  # footer


def test_returns_short_link_redirects_to_terms_anchor():
    r = _client().get("/returns", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/terms#returns"


# ───────────────────────── lot detail — new-retail anchor ───────────────────

def test_detail_shows_new_retail_anchor_with_sources(lot):
    r = _client().get("/listings/31225")
    assert r.status_code == 200
    assert "NEW, THESE RUN $24 (IMPORT GRADE) TO $749 (HOTEL GRADE)" in r.text
    assert "uline.com" in r.text


def test_sold_lot_has_no_retail_anchor(monkeypatch):
    row = make_lot(status="sold_out", quantity_remaining=0)
    monkeypatch.setattr(inventory, "get", lambda lot_id: dict(row) if lot_id == row["lot_id"] else None)
    r = _client().get("/listings/31225")
    assert "NEW, THESE RUN" not in r.text
