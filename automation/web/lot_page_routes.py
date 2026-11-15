"""Routes for the public auction-lot page and the deals sitemap.

    GET /deals/lot/{title-slug}-{st}-{source}-{key}   one lot, any source
    GET /sitemap-index.xml                            storefront sitemap + deals children
    GET /sitemaps/deals-{n}.xml                       one child (memo only, no DB read)

Read model: `lot_pages`; URL codec: `deal_lot_urls`; sitemap memo: `deal_sitemap`.
Handlers are plain `def` (FastAPI threadpool) because the lot page reads the DB.
"""
from __future__ import annotations

import importlib
import sys

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import auth as auth_svc
from . import deal_lot_urls, deal_sitemap, lot_pages

router = APIRouter()
EDGE_CACHE = "public, max-age=60, s-maxage=300"       # lets Cloudflare absorb a crawl


def _app():
    return importlib.import_module("automation.web.app")   # lazy: app imports this module


def _signed_in(request: Request) -> bool:
    """A platform account or the operator session. Asked only when photos are
    gated to signed-in visitors, so the default page makes no auth lookup."""
    if auth_svc.request_has_session(request):
        return True
    try:
        from . import platform_auth
        return platform_auth.maybe_user(request) is not None
    except Exception:  # noqa: BLE001 - an auth lookup failure reads as anonymous
        return False


@router.get("/deals/lot/{tail}", response_class=HTMLResponse)
def deal_lot_page(request: Request, tail: str):
    parsed = deal_lot_urls.parse(tail)
    if not parsed:
        raise HTTPException(404, "lot not found")
    try:
        lot = lot_pages.resolve(*parsed)
    except Exception as e:  # noqa: BLE001 - never a stack trace on a public page
        print(f"[lot-page] read failed for {parsed}: {e!r}", file=sys.stderr)
        raise HTTPException(503, "lot page temporarily unavailable")
    if lot is None:
        raise HTTPException(404, "lot not found")
    if request.url.path != lot["path"]:
        # Only source + key pick the lot; the text in front must be the canonical one.
        return RedirectResponse(lot["path"], status_code=301)
    webapp = _app()
    gated = lot_pages.photos_mode() == lot_pages.PHOTOS_SIGNED_IN
    ctx = lot_pages.view(lot, webapp.PUBLIC_BASE_URL, signed_in=gated and _signed_in(request))
    resp = webapp.templates.TemplateResponse(request, "deal_lot.html", ctx)
    resp.headers["Cache-Control"] = "private, no-store" if gated else EDGE_CACHE
    return resp


@router.get("/sitemap-index.xml", include_in_schema=False)
def sitemap_index():
    return Response(deal_sitemap.index_xml(_app().PUBLIC_BASE_URL), media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sitemaps/deals-{n}.xml", include_in_schema=False)
def sitemap_deals_child(n: int):
    xml = deal_sitemap.child_xml(_app().PUBLIC_BASE_URL, n)
    if xml is None:
        raise HTTPException(404, "no such sitemap")
    return Response(xml, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})
