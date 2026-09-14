"""No route handler may run blocking psycopg / disk work on the asyncio event
loop. When one does, the whole server freezes for the duration — every other
tab's fetch and the SSE streams queue behind it, which is why the admin felt
slow even for endpoints that were themselves cheap (2026-09-11 diagnosis).

Rule: an `async def` route may only touch the DB layer via
`asyncio.to_thread(...)`. Handlers with no `await` at all should simply be
`def` — FastAPI runs those in its threadpool."""
from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "automation" / "web" / "app.py"

# Names whose attribute-calls do synchronous I/O (psycopg or filesystem).
_SYNC_IO = re.compile(
    r"^(inventory|db|tracking_store|favorites|profiles|lot_channels|catalog_feed|"
    r"public_deals|_auctions_cache_stats)\.\w+\($|"
    r"^(_all_compare_rows|_list_listing_folders|_folder_images|_tracking_pass|"
    r"_alerts_collect_due|_deals_facets_and_stats|get_top_lots|_folder_meta)\($"
)
# Pure helpers that live on those modules and never touch I/O.
_PURE = {
    "inventory.location_labels", "inventory.is_sold", "inventory.parse_locations",
    "inventory.buyer_cert_abs_path", "inventory.generate_unsubscribe_token",
    "profiles.from_row", "profiles.validate_slug", "public_deals.is_excluded",
    "lot_channels.unit_word", "catalog_feed.rows_to_csv", "favorites.ALERT_INTERVALS",
}


def _is_route(fn: ast.AST) -> bool:
    for d in getattr(fn, "decorator_list", []):
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
            if getattr(d.func.value, "id", None) == "app":
                return True
    return False


def _call_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f"{f.value.id}.{f.attr}"
    if isinstance(f, ast.Name):
        return f.id
    return None


def _offenders(tree: ast.Module) -> list[str]:
    out = []
    for node in tree.body:
        if not (isinstance(node, ast.AsyncFunctionDef) and _is_route(node)):
            continue
        # Calls inside a nested def / lambda are (by convention here) the body
        # handed to asyncio.to_thread — they run off-loop.
        nested: set[int] = set()
        for x in ast.walk(node):
            if isinstance(x, (ast.FunctionDef, ast.Lambda, ast.AsyncFunctionDef)) and x is not node:
                nested.update(id(y) for y in ast.walk(x))
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or id(call) in nested:
                continue
            name = _call_name(call)
            if not name or name in _PURE:
                continue
            if _SYNC_IO.match(name + "("):
                out.append(f"{node.name}:{call.lineno} {name}()")
    return out


def test_no_sync_db_or_disk_calls_on_event_loop():
    tree = ast.parse(APP.read_text())
    assert _offenders(tree) == []


def test_detector_catches_a_direct_call():
    src = (
        "@app.get('/x')\n"
        "async def x():\n"
        "    return inventory.stats()\n"
    )
    assert _offenders(ast.parse(src)) == ["x:3 inventory.stats()"]


def test_detector_allows_to_thread_lambda():
    src = (
        "@app.get('/x')\n"
        "async def x():\n"
        "    return await asyncio.to_thread(lambda: inventory.stats())\n"
    )
    assert _offenders(ast.parse(src)) == []
