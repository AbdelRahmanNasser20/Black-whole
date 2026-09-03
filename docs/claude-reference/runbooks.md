<!-- Moved verbatim from ../../CLAUDE.md on 2026-08-28 (trim to <=8 KB). Original kept as ../../CLAUDE.md.pre-trim-2026-08-28 -->

## How to run end-to-end
1. First time only: `python run.py --login-only` (browser opens with FB + eBay tabs; log into both, close window).
2. `python -m automation.web` — public site at http://127.0.0.1:8765/, admin dashboard at http://127.0.0.1:8765/admin.
3. Paste GovDeals URL on the Launcher tab → Run. Confirm price when prompted. Drafts appear under the Drafts tab. Inventory ledger picks up the row automatically (see next section).

## Current status

**End-to-end pipeline is working.** First full successful run finished 2026-04-17 on `https://www.govdeals.com/en/asset/305/10340`:

- scrape ✓ 1 image (only lot the filter kept)
- llm ✓ `dom_fallback` (no Gemini key set yet)
- download ✓ 1 file with proper `Referer` + UA
- dewatermark ✓ 1 cleaned via `dewatermark.ai` API
- facebook ✓ draft created, URL has `listing_id=3...`
- ebay ✓ but URL landed on `ebay.com/sh/lst/active?sku=...` (the "active listings search" page), **NOT a real draft URL** — eBay selectors need verification

Dashboard lives at `http://127.0.0.1:8765`. Run triggered via:

```bash
curl -X POST http://127.0.0.1:8765/api/runs/start \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.govdeals.com/en/asset/<seller>/<lot>"}'
```

Price confirmation (when interactive prompt is expected) can be sent programmatically:

```bash
curl -X POST http://127.0.0.1:8765/api/runs/stdin \
  -H "Content-Type: application/json" \
  -d '{"line":"12"}'
```

## Skill / settings notes
- `.claude/settings.json` allowlists the project's common Bash commands (venv, pip, pytest, playwright, python run.py). Re-pickup needs `/hooks` open or session restart since Claude only watches files that existed at session start.
- For a fresh session with everything pre-allowed: `cd .../listing_automation && claude --permission-mode bypassPermissions`. CLAUDE.md (this file) auto-loads on start.
