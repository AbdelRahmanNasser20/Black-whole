"""GET /admin/ui — the design-review page for the shared UI layer (plan §5 step 9).

Renders every primitive in `static/ui/` against fixture rows so the operator can
eyeball all seven states at 1280 and 390 before B–G build on them. Auth-gated by
the `/admin` prefix (see `auth.path_requires_auth`). No DB access, ever.

Fixture rows mirror a `/deals/api/lots` row exactly — the public shape. They must
never carry `storage_note`, photos, verdicts, or `distance_mi`
(tests/web/test_ui_primitives.py::test_preview_page_renders_every_state).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def fixture_rows() -> list[dict]:
    """Eight lots covering: live · urgent (< 1 h) · no bids · unit price · closed with
    a final · closed with no bid · long title · missing city."""
    base = [
        dict(asset_id=101, account_id=7, auction_id=1, title="Lot of (30) Dell Latitude 5420 laptops, i5, 16 GB, no chargers",
             canonical_category="computers_electronics", city="Houston", state="TX", bid_count=14, current_bid=625,
             end_utc=_iso(timedelta(hours=3, minutes=47)), quantity=30, quantity_source="title", unit_bid=20.83, landed_cost=703.13),
        dict(asset_id=102, account_id=7, auction_id=1, title="2016 Ford F-250 Super Duty 4x4, 148k mi, runs",
             canonical_category="vehicles", city="Mesa", state="AZ", bid_count=27, current_bid=9250,
             end_utc=_iso(timedelta(minutes=41)), quantity=1, quantity_source="default", unit_bid=None, landed_cost=10406.25),
        dict(asset_id=103, account_id=9, auction_id=2, title="Pallet of assorted office chairs and desk parts",
             canonical_category="general", city="Tulsa", state="OK", bid_count=0, current_bid=25,
             end_utc=_iso(timedelta(days=2, hours=5)), quantity=1, quantity_source="default", unit_bid=None, landed_cost=28.13),
        dict(asset_id=104, account_id=3, auction_id=5, title="(12) Zebra ZT410 industrial label printers",
             canonical_category="computers_electronics", city="Sacramento", state="CA", bid_count=3, current_bid=410,
             end_utc=_iso(timedelta(days=1, hours=2)), quantity=12, quantity_source="llm", unit_bid=34.17, landed_cost=461.25),
        dict(asset_id=105, account_id=3, auction_id=5, title="Collection of 200+ silver coins, mixed dates",
             canonical_category="collectibles_jewelry", city="Reno", state="NV", bid_count=41, current_bid=3120,
             end_utc=_iso(timedelta(hours=-6)), outcome="sold", outcome_complete=True, final_bid=3405, final_bid_count=46,
             quantity=200, quantity_source="title", unit_bid=15.6, landed_cost=3830.63),
        dict(asset_id=106, account_id=11, auction_id=8, title="Surplus filing cabinets, 4-drawer, qty 18",
             canonical_category="other", city=None, state="GA", bid_count=0, current_bid=10,
             end_utc=_iso(timedelta(hours=-30)), outcome="no_bid", outcome_complete=True, final_bid=None, final_bid_count=0,
             quantity=18, quantity_source="title", unit_bid=0.56, landed_cost=11.25),
        dict(asset_id=107, account_id=11, auction_id=8,
             title="Miscellaneous lot of network switches, patch panels, rack rails, cable management arms, and one partial spool of Cat6 — sold as-is where-is, buyer loads",
             canonical_category="computers_electronics", city="Columbus", state="OH", bid_count=6, current_bid=140,
             end_utc=_iso(timedelta(hours=19, minutes=12)), quantity=1, quantity_source="default", unit_bid=None, landed_cost=157.5),
        dict(asset_id=108, account_id=5, auction_id=9, title="1998 John Deere 5210 tractor with loader",
             canonical_category="vehicles", city="Lincoln", state="NE", bid_count=9, current_bid=6100,
             end_utc=_iso(timedelta(days=4, hours=1)), quantity=1, quantity_source="default", unit_bid=None, landed_cost=6862.5),
    ]
    for r in base:
        r.setdefault("outcome", None)
        r.setdefault("outcome_complete", False)
        r.setdefault("final_bid", None)
        r.setdefault("final_bid_count", None)
        r["govdeals_url"] = f"https://www.govdeals.com/en/asset/{r['asset_id']}/{r['account_id']}"
        r["viewer_url"] = f"/deals/{r['asset_id']}/{r['account_id']}/{r['auction_id']}"
    return base


@router.get("/admin/ui", response_class=HTMLResponse)
async def ui_preview(request: Request):
    # Imported at call time: app.py imports this module, so a top-level import would be circular.
    from automation.web.app import templates  # noqa: WPS433

    return templates.TemplateResponse(request, "ui_preview.html", {
        "rows": fixture_rows(),
        "nav_active": "admin",
        "categories": [("All", 9791, True), ("General", 3120, False), ("Vehicles", 2044, False),
                       ("Collectibles & jewelry", 611, False), ("Computers & electronics", 1980, False), ("Other", 2036, False)],
    })
