# Listing Automation

End-to-end automation for turning a GovDeals URL into Facebook Marketplace +
eBay drafts. Implements the pipeline described in
`GovDeals_Automation_Blueprint.md`.

## Install

```bash
cd /Users/abdelnasser/Desktop/Black_whole_projects/listing_automation
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
```

`iopaint` (local watermark removal) is an optional extra and pulls a large
dependency tree — only install if you want the local pass to run before the
dewatermark.ai API fallback:

```bash
pip install -e '.[iopaint]'
```

## .env

Create `.env` in the project root (already gitignored):

```
DEWATERMARK_API_KEY=your_dewatermark_api_key
GEMINI_API_KEY=your_google_ai_studio_key   # optional, for secondary LLM A/B
```

- `DEWATERMARK_API_KEY` — get from your paid dewatermark.ai account. Used by the
  REST fallback (the script first tries IOPaint locally if installed, then
  escalates to this API if the bottom-right histogram still looks watermarked).
- `GEMINI_API_KEY` — free tier at https://aistudio.google.com/apikey. When set,
  the script auto-promotes Gemini to the **primary** extractor (it works
  standalone, unlike the Claude-Code one which needs a TTY).

## First-run login

Playwright uses its own persistent profile at
`~/.listing_automation/chrome_profile`. Log in once:

```bash
python run.py --login-only
```

A browser opens with two tabs (Facebook + eBay — dewatermark.ai is no longer
needed thanks to the API integration). Log into each tab, then **close the
browser window**. The script's close-event listener fires on window close and
saves the session. Subsequent runs are automatically logged in.

`browser.persistent_context()` auto-cleans stale `SingletonLock` files and
kills any leftover Chromium before each launch — if a previous run crashed or
was backgrounded, you don't need to do any manual cleanup.

## Normal usage

```bash
python run.py https://www.govdeals.com/en/asset/1234/5678
```

Flow:
1. Scrapes title, location, quantity, images
2. Runs primary LLM (Claude Code, if driving) and secondary (Gemini) in parallel
3. Prompts you to confirm the LLM-suggested per-chair price
4. Downloads images to `~/Desktop/Banquet chiars Pictures/{folder}/`
5. Dewatermarks locally (IOPaint/LaMa); falls back to dewatermark.ai if quality check fails
6. Opens Facebook Marketplace draft with all fields pre-filled
7. Opens eBay bulk-sell draft with all fields pre-filled
8. Leaves both browser tabs open — you review and click Publish manually

Flags:
- `--price 25` — override suggested price
- `--skip-dewatermark`, `--skip-fb`, `--skip-ebay` — partial runs for debugging

## LLM A/B comparison

Every run writes `~/.listing_automation/logs/llm_compare_*.json` with the
primary and secondary extractions side by side. After a handful of listings,
inspect to decide whether Gemini is good enough to replace whichever extractor
is currently primary.

## How the LLM is picked

`automation.llm.default_extractors()` chooses based on environment:

| `LISTING_LLM_MODE` | Primary used |
|---|---|
| (unset, "auto") | `gemini` if `GEMINI_API_KEY` set; else `claude_code` if stdin is a TTY; else `dom_fallback` |
| `gemini` | Gemini (or `dom_fallback` if no key) |
| `claude_code` | Claude-Code stdin/stdout sentinel protocol |
| `dom` | Pure DOM heuristic, no external calls |

The dashboard runs the script as a subprocess (no TTY), so `dom_fallback` is
the safe default there. Set `GEMINI_API_KEY` to upgrade the dashboard's
extraction quality without changing any code.

## Running when driven by Claude Code

If you paste `python run.py <url>` into a Claude Code chat with a TTY, the
script prints `<<<LLM_EXTRACT_REQUEST>>>` blocks referencing screenshot paths.
Claude Code reads those screenshots and responds with a
`<<<LLM_EXTRACT_RESPONSE>>> … <<<END_LLM_EXTRACT_RESPONSE>>>` JSON block —
no API key needed, bundled in your subscription. Won't work from the dashboard
(no TTY).

## Dashboard

A local web console at `http://127.0.0.1:8765` for kicking off runs, reviewing
generated drafts, and inspecting the LLM A/B logs.

```bash
source .venv/bin/activate
pip install -e '.[dev]'   # picks up fastapi / uvicorn / sse-starlette
python -m automation.web
```

Tabs:

- **01 Launcher** — paste a GovDeals URL, optionally toggle skip flags or
  override the per-chair price, then hit Run. The five pipeline phases
  (`scrape → llm → download → dewatermark → facebook → ebay`) light up live as
  Server-Sent Events stream stdout from `run.py`. When the LLM proposes a
  price, a confirm prompt appears in the UI so you don't need the terminal.
- **02 Drafts** — one card per folder under
  `~/Desktop/Banquet chiars Pictures/`. Thumbnail grid of cleaned images,
  extracted metadata (title / location / qty / chair_type / dimensions /
  price). Drafts surface clickable FB and eBay URLs once a run finishes them.
- **03 A/B** — every `~/.listing_automation/logs/llm_compare_*.json` rendered
  as a side-by-side diff. Mismatched cells light up red. Star **Match** /
  **Wrong** per entry; ratings persist to
  `~/.listing_automation/compare_ratings.json` so you can decide later whether
  Gemini is good enough to promote to primary.

The dashboard never modifies pipeline behavior — it spawns the same
`python run.py <url>` you would run by hand and tails its output.

## Tests

```bash
pytest
```

Runs the offline fixture-based extraction test. Does not hit the network or
require login.

## Known limits

- Chair-focused templates and eBay category hardcoded per blueprint
- Stops at draft on both platforms — never auto-publishes
- Selectors for FB/eBay UIs are best-effort and may need updating if those
  sites change; see logs in each phase for fallback warnings
- GovDeals **must** be scraped non-headless (Akamai 403s headless Chromium).
  `persistent_context()` defaults to `headless=False` — don't change that.
- Quantity parsing uses `\((\d{1,5})\)` on the page text — brittle. The
  `dom_fallback` extractor compensates by also scanning the description for
  `<N> chairs`; tweak `automation/llm/dom_fallback.py` if you keep getting
  wrong numbers.

## Starting a fresh Claude Code session in this project

If you want a clean session with all permissions pre-allowed for this project:

```bash
cd /Users/abdelnasser/Desktop/Black_whole_projects/listing_automation
claude --permission-mode bypassPermissions
```

`bypassPermissions` skips every tool prompt for the session. Use
`--permission-mode acceptEdits` if you'd rather Claude still confirm risky
actions (git pushes, destructive shell commands) but auto-accept file edits.

`CLAUDE.md` in the project root captures the architecture, gotchas, and TODOs
— Claude loads it automatically on startup so a new session has full context.
