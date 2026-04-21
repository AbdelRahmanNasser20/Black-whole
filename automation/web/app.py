"""FastAPI dashboard.

Routes:
  GET  /                  → single page with all three tabs
  GET  /api/runs/state    → snapshot of current/last run
  POST /api/runs/start    → kick off run.py with a GovDeals URL
  GET  /api/runs/stream   → SSE: progress + raw stdout lines
  GET  /api/drafts        → JSON list of listing folders + metadata
  GET  /api/compare       → JSON list of llm_compare_*.json logs
  POST /api/compare/{ts}/rate → save a star rating (matched / wrong)
  GET  /image/{folder}/{name} → serve image from a listing folder
  GET  /screenshot/{folder}/{name} → serve a Playwright screenshot

Streams stdout from run.py as Server-Sent Events. Parses
`<<<EVENT>>>{json}` lines emitted by automation.progress.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from ..config import DOWNLOAD_ROOT, LOG_DIR, STATE_ROOT
from ..progress import EVENT_PREFIX, parse as parse_event
from .. import inventory

try:
    from auction_extractors import get_top_chairs
except Exception:  # pragma: no cover
    get_top_chairs = None  # auction_extractors unavailable; /api/auctions will 503

PKG_DIR = Path(__file__).parent
TEMPLATE_DIR = PKG_DIR / "templates"
STATIC_DIR = PKG_DIR / "static"
PROJECT_ROOT = PKG_DIR.parents[1]
AUCTION_EXTRACTORS_DIR = PROJECT_ROOT / "auction_extractors"
RATINGS_FILE = STATE_ROOT / "compare_ratings.json"

PHASES = ["scrape", "llm", "download", "dewatermark", "facebook", "ebay"]

app = FastAPI(title="listing_automation dashboard")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ───────────────────────────── run state ─────────────────────────────

class RunState:
    """In-memory state for the most recent run.

    A single run at a time is the design contract — the user kicks one off
    from the launcher and watches it stream. We append every stdout line and
    every parsed event to ring buffers so a late-joining SSE client can catch
    up with replay before subscribing to live updates.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.return_code: int | None = None
        self.lines: list[dict] = []          # {"t": ts, "stream": "stdout|stderr|event", "data": ...}
        self.phases: dict[str, dict] = {p: {"status": "pending"} for p in PHASES}
        self.run_status: str = "idle"        # idle | running | finished | error
        self.subscribers: list[asyncio.Queue] = []
        self.suggested_price: int | None = None
        self.confirmed_price: int | None = None
        # FIFO of pending runs queued by the user from the Auctions tab or
        # Launcher while another run is active. Each entry: {"url": str,
        # "extra_args": list[str]}. Drained by _start_next() when a run ends.
        self.pending: list[dict] = []

    def reset(self) -> None:
        self.proc = None
        self.url = None
        self.started_at = None
        self.finished_at = None
        self.return_code = None
        self.lines.clear()
        self.phases = {p: {"status": "pending"} for p in PHASES}
        self.run_status = "idle"
        self.suggested_price = None
        self.confirmed_price = None

    async def broadcast(self, msg: dict) -> None:
        self.lines.append(msg)
        # Cap memory — keep last 4k events
        if len(self.lines) > 4000:
            del self.lines[:1000]
        dead = []
        for q in self.subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.subscribers.remove(q)

    def snapshot(self) -> dict:
        return {
            "url": self.url,
            "status": self.run_status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "return_code": self.return_code,
            "phases": self.phases,
            "suggested_price": self.suggested_price,
            "confirmed_price": self.confirmed_price,
            "line_count": len(self.lines),
            "queue": [
                {"position": i + 1, "url": item["url"]}
                for i, item in enumerate(self.pending)
            ],
            "queue_length": len(self.pending),
        }


state = RunState()


# ───────────────────────────── subprocess ─────────────────────────────

async def _pump_stream(stream: asyncio.StreamReader, kind: str) -> None:
    """Read run.py's stdout/stderr line by line, parse <<<EVENT>>> markers."""
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        ev = parse_event(line)
        if ev is not None:
            await _apply_event(ev)
            await state.broadcast({"t": time.time(), "stream": "event", "data": ev})
        else:
            await state.broadcast({"t": time.time(), "stream": kind, "data": line})


async def _apply_event(ev: dict) -> None:
    kind = ev.get("kind")
    if kind == "phase":
        phase = ev.get("phase")
        if phase in state.phases:
            entry = {k: v for k, v in ev.items() if k not in ("kind", "phase", "ts")}
            state.phases[phase] = entry
    elif kind == "run":
        st = ev.get("status")
        if st == "started":
            state.run_status = "running"
        elif st == "finished":
            state.run_status = "finished"
    elif kind == "price":
        state.suggested_price = ev.get("suggested")
        state.confirmed_price = ev.get("confirmed")


async def _run_subprocess(url: str, extra_args: list[str]) -> None:
    """Spawn `python run.py <url>` and pump its output into the event stream."""
    project_root = Path(__file__).resolve().parents[2]
    run_py = project_root / "run.py"
    cmd = [sys.executable, "-u", str(run_py), url] + extra_args
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Give the dashboard up to 2 minutes to POST a confirmed price before run.py
    # auto-accepts the LLM suggestion. (run.py:_wait_for_price_confirmation)
    env.setdefault("LISTING_PRICE_CONFIRM_TIMEOUT", "120")

    state.proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(project_root),
    )
    state.started_at = time.time()
    state.run_status = "running"

    await state.broadcast({
        "t": time.time(), "stream": "system",
        "data": f"$ {' '.join(cmd)}",
    })

    try:
        await asyncio.gather(
            _pump_stream(state.proc.stdout, "stdout"),
            _pump_stream(state.proc.stderr, "stderr"),
        )
        rc = await state.proc.wait()
    except Exception as e:
        await state.broadcast({"t": time.time(), "stream": "system",
                              "data": f"[runner error] {e!r}"})
        rc = -1

    state.return_code = rc
    state.finished_at = time.time()
    state.run_status = "finished" if rc == 0 else "error"
    await state.broadcast({"t": time.time(), "stream": "system",
                          "data": f"[exit {rc}]"})
    # Drain the next queued URL if any.
    await _start_next()


async def _broadcast_queue() -> None:
    """Push a snapshot-ish event when the pending queue changes."""
    await state.broadcast({
        "t": time.time(), "stream": "queue",
        "data": {"queue": [
            {"position": i + 1, "url": item["url"]}
            for i, item in enumerate(state.pending)
        ], "queue_length": len(state.pending)},
    })


async def _start_next() -> None:
    """If idle and something's pending, pop the head and start it."""
    if state.run_status == "running":
        return
    if not state.pending:
        return
    item = state.pending.pop(0)
    state.reset()
    state.url = item["url"]
    asyncio.create_task(_run_subprocess(item["url"], item.get("extra_args") or []))
    await _broadcast_queue()


# ───────────────────────────── HTTP routes ─────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"phases": PHASES, "now": int(time.time())},
    )


# ───────────────────────────── public pages ─────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def public_landing(request: Request):
    try:
        counts = inventory.stats()
        featured = inventory.list_public()[:4]
    except Exception:
        counts = {"lots": 0, "chairs": 0, "cities": 0}
        featured = []
    return templates.TemplateResponse(
        request, "landing.html",
        {"stats": counts, "featured": featured, "now": int(time.time())},
    )


@app.get("/listings", response_class=HTMLResponse)
async def public_listings(request: Request):
    items = inventory.list_public()
    cities = sorted({(r.get("city") or "").strip() for r in items if r.get("city")})
    chair_types = sorted({(r.get("chair_type") or "").strip()
                          for r in items if r.get("chair_type")})
    return templates.TemplateResponse(
        request, "listings.html",
        {"items": items, "cities": cities, "chair_types": chair_types,
         "now": int(time.time())},
    )


@app.get("/listings/{lot_id}", response_class=HTMLResponse)
async def public_listing_detail(request: Request, lot_id: str):
    row = inventory.get(lot_id)
    if not row or row.get("status") in ("hidden",):
        raise HTTPException(404, "listing not found")
    imgs: list[str] = []
    folder_name = row.get("folder_name")
    if folder_name:
        folder = DOWNLOAD_ROOT / folder_name
        imgs = _folder_images(folder)
    return templates.TemplateResponse(
        request, "listing_detail.html",
        {"item": row, "images": imgs, "now": int(time.time())},
    )


@app.get("/sell", response_class=HTMLResponse)
async def public_sell(request: Request):
    return templates.TemplateResponse(
        request, "sell.html", {"now": int(time.time())},
    )


@app.post("/contact")
async def public_contact(payload: dict):
    payload = payload or {}
    try:
        row = inventory.create_inquiry(
            kind=(payload.get("kind") or "buy").strip(),
            name=(payload.get("name") or "").strip(),
            email=(payload.get("email") or "").strip() or None,
            phone=(payload.get("phone") or "").strip() or None,
            message=(payload.get("message") or "").strip() or None,
            lot_id=(payload.get("lot_id") or "").strip() or None,
            quantity_interested=(
                int(payload["quantity_interested"])
                if payload.get("quantity_interested")
                else None
            ),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": row["id"]}


@app.get("/api/runs/state")
async def get_run_state():
    return JSONResponse(state.snapshot())


def _extra_args_from_payload(payload: dict) -> list[str]:
    extra: list[str] = []
    for flag in ("skip_dewatermark", "skip_fb", "skip_ebay", "force_republish"):
        if payload.get(flag):
            extra.append("--" + flag.replace("_", "-"))
    if payload.get("price"):
        extra += ["--price", str(int(payload["price"]))]
    return extra


def _collect_urls(payload: dict) -> list[str]:
    """Pull one or many URLs out of a request payload.

    Accepts `url: str` (legacy Launcher) or `urls: list[str]` (Auctions tab,
    Queue-all). Validates govdeals.com membership; drops anything else.
    """
    raw: list[str] = []
    single = (payload.get("url") or "").strip()
    if single:
        raw.append(single)
    for u in payload.get("urls") or []:
        if isinstance(u, str) and u.strip():
            raw.append(u.strip())
    return [u for u in raw if "govdeals.com" in u]


@app.post("/api/runs/start")
@app.post("/api/runs/queue")
async def start_run(payload: dict):
    payload = payload or {}
    urls = _collect_urls(payload)
    if not urls:
        raise HTTPException(400, "Provide at least one govdeals.com URL")

    extra = _extra_args_from_payload(payload)

    async with state.lock:
        if state.run_status == "running":
            for u in urls:
                state.pending.append({"url": u, "extra_args": extra})
            await _broadcast_queue()
            return {"ok": True, "queued": len(urls), "running": state.url,
                    "queue_length": len(state.pending)}

        head, *rest = urls
        for u in rest:
            state.pending.append({"url": u, "extra_args": extra})
        state.reset()
        state.url = head
        asyncio.create_task(_run_subprocess(head, extra))
        await _broadcast_queue()
        return {"ok": True, "running": head, "queued": len(rest),
                "queue_length": len(state.pending)}


@app.post("/api/runs/queue/clear")
async def clear_queue():
    async with state.lock:
        n = len(state.pending)
        state.pending.clear()
        await _broadcast_queue()
    return {"ok": True, "cleared": n}


@app.post("/api/runs/cancel")
async def cancel_run():
    if state.proc and state.proc.returncode is None:
        try:
            state.proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        return {"ok": True}
    return {"ok": False, "reason": "no active run"}


@app.post("/api/runs/stdin")
async def write_stdin(payload: dict):
    """Forward a line to the subprocess stdin (used to confirm price prompt)."""
    line = (payload or {}).get("line", "")
    if not state.proc or state.proc.stdin is None or state.proc.returncode is not None:
        raise HTTPException(409, "no active run")
    state.proc.stdin.write((line + "\n").encode())
    await state.proc.stdin.drain()
    return {"ok": True}


@app.get("/api/runs/stream")
async def stream_run(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=2048)

    async def event_gen():
        # Replay
        for msg in list(state.lines):
            yield {"event": msg["stream"], "data": json.dumps(msg)}
        # Subscribe
        state.subscribers.append(queue)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {"event": msg["stream"], "data": json.dumps(msg)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            if queue in state.subscribers:
                state.subscribers.remove(queue)

    return EventSourceResponse(event_gen())


# ───────────────────────────── drafts ─────────────────────────────

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _list_listing_folders() -> list[Path]:
    if not DOWNLOAD_ROOT.exists():
        return []
    folders = []
    for p in DOWNLOAD_ROOT.iterdir():
        if not p.is_dir():
            continue
        # skip non-listing buckets
        if p.name.startswith(".") or p.name in {"General listing", "Lost biddings",
                                                 "Listing Automations", "Listing_automation_html"}:
            continue
        folders.append(p)
    folders.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return folders


def _folder_images(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    files = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            files.append(p.name)
    files.sort()
    return files


def _latest_compare_for_folder(folder_name: str) -> dict | None:
    """Best-effort match: pick the most recent llm log whose primary title slug
    matches the folder name (folders are slugified titles)."""
    candidates = sorted(LOG_DIR.glob("llm_compare_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    folder_slug = re.sub(r"[^a-z0-9]", "", folder_name.lower())
    for log in candidates:
        try:
            data = json.loads(log.read_text())
        except Exception:
            continue
        primary = (data.get("primary") or {})
        title = primary.get("title") or ""
        title_slug = re.sub(r"[^a-z0-9]", "", title.lower())
        if title_slug and (title_slug in folder_slug or folder_slug in title_slug):
            return data
    return None


@app.get("/api/drafts")
async def list_drafts():
    out = []
    for folder in _list_listing_folders():
        imgs = _folder_images(folder)
        meta = _latest_compare_for_folder(folder.name)
        primary = (meta or {}).get("primary") or {}
        out.append({
            "folder": folder.name,
            "path": str(folder),
            "modified": folder.stat().st_mtime,
            "image_count": len(imgs),
            "images": imgs[:24],
            "title": primary.get("title"),
            "location": primary.get("location"),
            "quantity": primary.get("quantity"),
            "chair_type": primary.get("chair_type"),
            "dimensions": primary.get("dimensions"),
            "suggested_price": primary.get("suggested_price_per_chair"),
        })
    return {"drafts": out}


@app.get("/image/{folder}/{name}")
async def serve_image(folder: str, name: str):
    folder_path = DOWNLOAD_ROOT / folder
    target = (folder_path / name).resolve()
    if not str(target).startswith(str(folder_path.resolve())):
        raise HTTPException(403, "path traversal")
    if not target.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(target))


@app.get("/screenshot/{folder}/{name}")
async def serve_screenshot(folder: str, name: str):
    target = (DOWNLOAD_ROOT / folder / "_screenshots" / name).resolve()
    base = (DOWNLOAD_ROOT / folder / "_screenshots").resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(403, "path traversal")
    if not target.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(target))


# ───────────────────────────── compare ─────────────────────────────

def _load_ratings() -> dict[str, str]:
    if not RATINGS_FILE.exists():
        return {}
    try:
        return json.loads(RATINGS_FILE.read_text())
    except Exception:
        return {}


def _save_ratings(d: dict[str, str]) -> None:
    RATINGS_FILE.write_text(json.dumps(d, indent=2))


@app.get("/api/compare")
async def list_compare():
    ratings = _load_ratings()
    logs = sorted(LOG_DIR.glob("llm_compare_*.json"),
                 key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for log in logs:
        try:
            data = json.loads(log.read_text())
        except Exception as e:
            data = {"error": str(e)}
        ts_match = re.search(r"llm_compare_(\d+)\.json", log.name)
        ts = int(ts_match.group(1)) if ts_match else int(log.stat().st_mtime)
        out.append({
            "id": str(ts),
            "filename": log.name,
            "timestamp": ts,
            "modified": log.stat().st_mtime,
            "dom_hint": data.get("dom_hint"),
            "primary": data.get("primary"),
            "secondary": data.get("secondary"),
            "rating": ratings.get(str(ts)),
        })
    return {"entries": out}


@app.post("/api/compare/{cid}/rate")
async def rate_compare(cid: str, payload: dict):
    rating = (payload or {}).get("rating")
    if rating not in (None, "", "match", "wrong"):
        raise HTTPException(400, "rating must be 'match', 'wrong', or null")
    ratings = _load_ratings()
    if rating in (None, ""):
        ratings.pop(cid, None)
    else:
        ratings[cid] = rating
    _save_ratings(ratings)
    return {"ok": True, "ratings": ratings}


# ───────────────────────────── scraper ─────────────────────────────

# Maps logical source names to the script file inside auction_extractors/.
# Each script is invoked with cwd=auction_extractors/ so its sibling
# imports (`from paths import STATE_DIR`, etc.) resolve.
_SCRAPE_SCRIPTS = {
    "gd": "govdeals_chairs_extraction.py",
    "ps": "public_surplus_automation.py",
}
_SCRAPE_LABELS = {"gd": "GovDeals", "ps": "Public Surplus"}

# Maps the `[n]` / `[nX]` prefixes the scrapers emit to human labels that
# render in the SCRAPE strip. Prefix order mirrors the scrapers' pipeline:
# preflight → scrape → describe → regex refine → llm refine → rank → alert.
_SCRAPE_STAGES = {
    "[0]":  "preflight",
    "[1]":  "scraping listings",
    "[1b]": "fetching descriptions",
    "[1c]": "regex refine",
    "[1d]": "llm refine",
    "[2]":  "ranking",
    "[3a]": "telegram alert",
}
_SCRAPE_STAGE_RE = re.compile(r"^\s*(\[[0-9a-z]+\])")
_PAGE_DETAIL_RE = re.compile(r"\bPage\s+(\d+):")
_DESC_DETAIL_RE = re.compile(r"…\s+(\d+)\s*/\s*(\d+)")
_FILTER_DETAIL_RE = re.compile(r"Filter:\s+'([^']+)'")


def _parse_stage(line: str) -> tuple[str | None, str | None]:
    """Return (stage_label, stage_detail) for a stdout line, or (None, None)."""
    m = _SCRAPE_STAGE_RE.match(line)
    label = _SCRAPE_STAGES.get(m.group(1)) if m else None
    detail = None
    if (mp := _PAGE_DETAIL_RE.search(line)):
        detail = f"page {mp.group(1)}"
    elif (md := _DESC_DETAIL_RE.search(line)):
        detail = f"{md.group(1)}/{md.group(2)} descriptions"
    elif (mf := _FILTER_DETAIL_RE.search(line)):
        detail = f"filter: {mf.group(1)}"
    return label, detail


class ScrapeState:
    """Separate from RunState so scrapes and pipeline runs coexist.

    A single scrape job at a time (same design as pipeline: it's a 5-30min
    Playwright job, no point in running two concurrently against the same
    DB row set).
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.proc: asyncio.subprocess.Process | None = None
        self.source: str | None = None        # "gd" | "ps" | "both"
        self.current_step: str | None = None  # "gd" | "ps" — the running sub-job
        self.current_stage: str | None = None  # human label from _SCRAPE_STAGES
        self.stage_detail: str | None = None   # e.g. "page 3" / "12/40 descriptions"
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.return_code: int | None = None
        self.test_mode: bool = False
        self.status: str = "idle"             # idle | running | finished | error | cancelled
        self.last_line: str = ""
        self.lines: list[dict] = []
        self.subscribers: list[asyncio.Queue] = []

    def reset(self, source: str, test_mode: bool) -> None:
        self.source = source
        self.test_mode = test_mode
        self.current_step = None
        self.current_stage = None
        self.stage_detail = None
        self.started_at = time.time()
        self.finished_at = None
        self.return_code = None
        self.status = "running"
        self.last_line = ""
        self.lines.clear()

    async def broadcast(self, msg: dict) -> None:
        self.lines.append(msg)
        if len(self.lines) > 2000:
            del self.lines[:500]
        dead = []
        for q in self.subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.subscribers.remove(q)

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "source": self.source,
            "current_step": self.current_step,
            "current_stage": self.current_stage,
            "stage_detail": self.stage_detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "return_code": self.return_code,
            "test_mode": self.test_mode,
            "last_line": self.last_line,
            "line_count": len(self.lines),
        }


scrape_state = ScrapeState()


async def _pump_scrape_stream(stream: asyncio.StreamReader, kind: str) -> None:
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        scrape_state.last_line = line[-300:]

        if kind == "stdout":
            label, detail = _parse_stage(line)
            changed = False
            if label and label != scrape_state.current_stage:
                scrape_state.current_stage = label
                scrape_state.stage_detail = None  # reset detail when stage flips
                changed = True
            if detail and detail != scrape_state.stage_detail:
                scrape_state.stage_detail = detail
                changed = True
            if changed:
                await scrape_state.broadcast({
                    "t": time.time(), "stream": "event",
                    "data": {
                        "kind": "scrape_stage",
                        "stage": scrape_state.current_stage,
                        "detail": scrape_state.stage_detail,
                        "step": scrape_state.current_step,
                    },
                })

        await scrape_state.broadcast({"t": time.time(), "stream": kind, "data": line})


async def _run_scraper(source: str, test_mode: bool) -> None:
    """Run one or both scrapers sequentially (gd → ps when source='both')."""
    steps: list[str] = ["gd", "ps"] if source == "both" else [source]
    overall_rc = 0

    for step in steps:
        scrape_state.current_step = step
        # Reset stage between steps so the prior sub-job's final stage doesn't
        # bleed into the next sub-job's "starting" window.
        scrape_state.current_stage = "starting"
        scrape_state.stage_detail = None
        await scrape_state.broadcast({
            "t": time.time(), "stream": "event",
            "data": {
                "kind": "scrape_stage",
                "stage": "starting", "detail": None, "step": step,
            },
        })
        script = AUCTION_EXTRACTORS_DIR / _SCRAPE_SCRIPTS[step]
        cmd = [sys.executable, "-u", str(script)]
        if step == "ps" and test_mode:
            cmd.append("--test")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        await scrape_state.broadcast({
            "t": time.time(), "stream": "system",
            "data": f"$ cd auction_extractors && {' '.join(cmd[-2:])}",
        })

        try:
            scrape_state.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(AUCTION_EXTRACTORS_DIR),
                env=env,
            )
        except Exception as e:
            await scrape_state.broadcast({
                "t": time.time(), "stream": "system",
                "data": f"[spawn failed for {step}] {e!r}",
            })
            overall_rc = -1
            break

        try:
            await asyncio.gather(
                _pump_scrape_stream(scrape_state.proc.stdout, "stdout"),
                _pump_scrape_stream(scrape_state.proc.stderr, "stderr"),
            )
            rc = await scrape_state.proc.wait()
        except Exception as e:
            await scrape_state.broadcast({
                "t": time.time(), "stream": "system",
                "data": f"[runner error] {e!r}",
            })
            rc = -1

        await scrape_state.broadcast({
            "t": time.time(), "stream": "system",
            "data": f"[{_SCRAPE_LABELS.get(step, step)} exit {rc}]",
        })
        if rc != 0:
            overall_rc = rc
            # Stop the chain — don't run ps if gd failed.
            break

    scrape_state.proc = None
    scrape_state.return_code = overall_rc
    scrape_state.finished_at = time.time()
    # Treat cancelled run as cancelled, not error.
    if scrape_state.status != "cancelled":
        scrape_state.status = "finished" if overall_rc == 0 else "error"

    # Fresh rows are in the DB now — bust the read-side cache so the next
    # /api/auctions call returns the updated set.
    _AUCTIONS_CACHE.clear()

    await scrape_state.broadcast({
        "t": time.time(), "stream": "event",
        "data": {"kind": "scrape", "status": scrape_state.status,
                 "return_code": overall_rc},
    })


@app.post("/api/scrape/start")
async def scrape_start(payload: dict):
    payload = payload or {}
    source = (payload.get("source") or "gd").strip()
    if source not in ("gd", "ps", "both"):
        raise HTTPException(400, "source must be 'gd', 'ps', or 'both'")
    test_mode = bool(payload.get("test"))

    async with scrape_state.lock:
        if scrape_state.status == "running":
            raise HTTPException(409, "a scrape is already running")
        scrape_state.reset(source, test_mode)
        asyncio.create_task(_run_scraper(source, test_mode))

    return {"ok": True, "source": source, "test_mode": test_mode}


@app.post("/api/scrape/cancel")
async def scrape_cancel():
    if scrape_state.proc and scrape_state.proc.returncode is None:
        scrape_state.status = "cancelled"
        try:
            scrape_state.proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        return {"ok": True}
    return {"ok": False, "reason": "no active scrape"}


@app.get("/api/scrape/state")
async def scrape_state_snapshot():
    return JSONResponse(scrape_state.snapshot())


@app.get("/api/scrape/stream")
async def scrape_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=2048)

    async def event_gen():
        for msg in list(scrape_state.lines):
            yield {"event": msg["stream"], "data": json.dumps(msg)}
        scrape_state.subscribers.append(queue)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {"event": msg["stream"], "data": json.dumps(msg)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            if queue in scrape_state.subscribers:
                scrape_state.subscribers.remove(queue)

    return EventSourceResponse(event_gen())


# ───────────────────────────── auctions ─────────────────────────────

# In-memory cache for /api/auctions responses. Condition-scoring LLM calls
# are 3-10s per request; cache by query-string for ~10 min so toggling
# filters in the UI doesn't re-run the LLM on every click.
_AUCTIONS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_AUCTIONS_TTL = 600.0  # seconds


@app.get("/api/auctions")
async def list_auctions(
    source: str = "gd",
    n: int = 15,
    min_qty: int = 50,
    condition: int = 0,
    active_only: int = 1,
    max_stale_days: int = 2,
):
    if get_top_chairs is None:
        raise HTTPException(503, "auction_extractors package not available")
    if source not in ("gd", "ps"):
        raise HTTPException(400, "source must be 'gd' or 'ps'")
    n = max(1, min(int(n), 100))
    min_qty = max(1, int(min_qty))
    include_condition = bool(int(condition))
    active_flag = bool(int(active_only))
    stale = max(1, int(max_stale_days))

    key = f"{source}|{n}|{min_qty}|{int(include_condition)}|{int(active_flag)}|{stale}"
    now = time.time()
    cached = _AUCTIONS_CACHE.get(key)
    if cached and (now - cached[0]) < _AUCTIONS_TTL:
        return {"items": cached[1], "cached": True, "age": int(now - cached[0])}

    try:
        items = await asyncio.to_thread(
            get_top_chairs,
            source=source,
            n=n,
            min_quantity=min_qty,
            include_condition=include_condition,
            active_only=active_flag,
            max_stale_days=stale,
        )
    except Exception as e:
        raise HTTPException(500, f"get_top_chairs failed: {e!r}")

    _AUCTIONS_CACHE[key] = (now, items)
    return {"items": items, "cached": False, "age": 0}


@app.post("/api/auctions/refresh")
async def refresh_auctions_cache():
    _AUCTIONS_CACHE.clear()
    return {"ok": True}


@app.get("/api/health", response_class=PlainTextResponse)
async def health():
    return "ok"


# ───────────────────────────── inventory API ─────────────────────────────

def _inventory_to_public(row: dict) -> dict:
    """Enrich an inventory row with the image URL the UI can render directly."""
    out = dict(row)
    folder = row.get("folder_name")
    hero = row.get("hero_image")
    if folder and hero:
        out["hero_image_url"] = f"/image/{folder}/{hero}"
    else:
        out["hero_image_url"] = None
    return out


@app.get("/api/inventory")
async def inv_list(status: str | None = None):
    rows = inventory.list_all(status=status)
    return {"items": [_inventory_to_public(r) for r in rows]}


@app.get("/api/inventory/{lot_id}")
async def inv_get(lot_id: str):
    row = inventory.get(lot_id)
    if not row:
        raise HTTPException(404, "not found")
    return _inventory_to_public(row)


@app.patch("/api/inventory/{lot_id}")
async def inv_update(lot_id: str, payload: dict):
    try:
        row = inventory.set_fields(lot_id, **(payload or {}))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not row:
        raise HTTPException(404, "not found")
    return _inventory_to_public(row)


@app.post("/api/inventory")
async def inv_create(payload: dict):
    payload = payload or {}
    try:
        row = inventory.insert_manual(
            lot_id=str(payload["lot_id"]).strip(),
            title=str(payload["title"]).strip(),
            quantity=int(payload["quantity"]),
            price_per_chair=(float(payload["price_per_chair"])
                             if payload.get("price_per_chair") else None),
            city=payload.get("city") or None,
            state=payload.get("state") or None,
            chair_type=payload.get("chair_type") or None,
            dimensions=payload.get("dimensions") or None,
            description=payload.get("description") or None,
            folder_name=payload.get("folder_name") or None,
            hero_image=payload.get("hero_image") or None,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return _inventory_to_public(row)


@app.delete("/api/inventory/{lot_id}")
async def inv_delete(lot_id: str):
    ok = inventory.delete(lot_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


@app.post("/api/inventory/{lot_id}/platform")
async def inv_set_platform(lot_id: str, payload: dict):
    """Backfill a FB or eBay URL for a lot that was listed manually."""
    payload = payload or {}
    platform = payload.get("platform")
    url = (payload.get("url") or "").strip() or None
    if platform not in ("facebook", "ebay"):
        raise HTTPException(400, "platform must be 'facebook' or 'ebay'")
    row = inventory.set_platform_url(
        lot_id, platform, url, clear_timestamp=(url is None),
    )
    if not row:
        raise HTTPException(404, "not found")
    return _inventory_to_public(row)


@app.get("/api/inventory-stats")
async def inv_stats():
    return inventory.stats()


@app.post("/api/inventory/backfill")
async def inv_backfill():
    """Walk DOWNLOAD_ROOT, add a draft row for any folder not in the table.

    Best-effort: pulls title/qty/city/chair_type from the same
    llm_compare_*.json lookup the Drafts tab uses. FB/eBay URLs stay NULL;
    admin must paste them manually for pre-tracking listings.
    """
    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    for folder in _list_listing_folders():
        meta = _latest_compare_for_folder(folder.name) or {}
        primary = meta.get("primary") or {}
        dom_hint = meta.get("dom_hint") or {}
        # Can we derive a stable lot_id? The folder name doesn't carry one, but
        # if the llm log does, use it. Otherwise synthesize "folder:<slug>" so
        # at least the admin can see + edit the row. Real lot_id can be pasted
        # in later.
        lot_id = (primary.get("lot_id") or dom_hint.get("lot_id")
                  or f"folder:{folder.name}")
        existing = inventory.get(lot_id)
        imgs = _folder_images(folder)
        hero = imgs[0] if imgs else None
        title = primary.get("title") or dom_hint.get("title") or folder.name
        city = primary.get("city") or dom_hint.get("city")
        state = primary.get("state") or dom_hint.get("state")
        qty_raw = primary.get("quantity") or dom_hint.get("quantity")
        try:
            qty_int = int(qty_raw) if qty_raw and str(qty_raw).isdigit() else None
        except Exception:
            qty_int = None
        if existing is None:
            try:
                inventory.insert_manual(
                    lot_id=lot_id,
                    title=title,
                    quantity=qty_int or 0,
                    city=city, state=state,
                    chair_type=primary.get("chair_type"),
                    dimensions=primary.get("dimensions"),
                    price_per_chair=(
                        float(primary["suggested_price_per_chair"])
                        if primary.get("suggested_price_per_chair") else None
                    ),
                    folder_name=folder.name,
                    hero_image=hero,
                )
                added.append(lot_id)
            except Exception:
                skipped.append(folder.name)
        else:
            # Keep user edits, just refresh folder binding + hero if missing.
            patch = {}
            if not existing.get("hero_image") and hero:
                patch["hero_image"] = hero
            if patch:
                inventory.set_fields(lot_id, **patch)
                updated.append(lot_id)
            else:
                skipped.append(lot_id)
    return {"added": added, "updated": updated, "skipped": skipped,
            "counts": {"added": len(added), "updated": len(updated),
                       "skipped": len(skipped)}}


# ───────────────────────────── inquiries API ─────────────────────────────

@app.get("/api/inquiries")
async def inq_list(status: str | None = None):
    return {"items": inventory.list_inquiries(status=status)}


@app.patch("/api/inquiries/{inquiry_id}")
async def inq_update(inquiry_id: int, payload: dict):
    payload = payload or {}
    row: dict | None = None
    if "status" in payload:
        try:
            row = inventory.set_inquiry_status(inquiry_id, payload["status"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "lot_id" in payload:
        row = inventory.link_inquiry(inquiry_id, payload["lot_id"] or None)
    if row is None:
        row = inventory.get_inquiry(inquiry_id)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@app.delete("/api/inquiries/{inquiry_id}")
async def inq_delete(inquiry_id: int):
    ok = inventory.delete_inquiry(inquiry_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


# ───────────────────────────── entrypoint ─────────────────────────────

def main() -> None:
    import uvicorn
    host = os.getenv("LISTING_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("LISTING_WEB_PORT", "8765"))
    uvicorn.run(
        "automation.web.app:app",
        host=host, port=port,
        reload=False, log_level="info",
    )


if __name__ == "__main__":
    main()
