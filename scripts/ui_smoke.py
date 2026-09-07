"""Playwright smoke: for each URL, wait until no [data-state="loading"] remains, assert no console errors,
no aria-busy left behind, screenshot to docs/superpowers/screenshots/<branch>/. Usage:
  .venv/bin/python scripts/ui_smoke.py /deals "/deals?view=map" /sources
Env: UI_BASE (default http://127.0.0.1:8765), UI_WIDTHS (default 1280,390)."""
import os, subprocess, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = os.getenv("UI_BASE", "http://127.0.0.1:8765")
WIDTHS = [int(w) for w in os.getenv("UI_WIDTHS", "1280,390").split(",")]
branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip() or "wip"
out = Path("docs/superpowers/screenshots") / branch; out.mkdir(parents=True, exist_ok=True)
failures = []
with sync_playwright() as p:
    b = p.chromium.launch()
    for url in sys.argv[1:]:
        for w in WIDTHS:
            pg = b.new_page(viewport={"width": w, "height": 900}); errors = []
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(BASE + url, wait_until="domcontentloaded")
            try:
                pg.wait_for_function("!document.querySelector('[data-state=\"loading\"]')", timeout=20000)
            except Exception:
                failures.append(f"{url}@{w}: still loading after 20 s")
            if pg.locator("[aria-busy='true']").count():
                failures.append(f"{url}@{w}: aria-busy left behind")
            if errors:
                failures.append(f"{url}@{w}: console errors: {errors[:3]}")
            pg.screenshot(path=str(out / (url.strip('/').replace('/', '_').replace('?', '_') or 'root') + f"_{w}.png"), full_page=True)
            pg.close()
    b.close()
print("\n".join(failures) or f"smoke ok → {out}"); sys.exit(1 if failures else 0)
