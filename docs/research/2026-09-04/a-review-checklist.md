# Workstream A — design review checklist (`/admin/ui`)

**Result: all §1 checks pass at 1280 and 390.** Verified 2026-09-07 with headless Chromium against a throwaway server on :8766 (no `.env`, no DB — the preview page needs neither). Screenshots: `docs/superpowers/screenshots/ui/a-tokens-primitives/admin_ui_{1280,390}.png`.

- **Radius 0 everywhere** — pass.
  - Computed `border-radius` of every element on the page at both widths: no value other than `0px` (Playwright probe over `document.querySelectorAll('*')`).
  - Only `var(--radius)` is ever written; no literal radius in `components.css` / `skeleton.css`.
- **One bold element = mono price + timer** — pass.
  - `.card-price` is the only 700-weight text in a card (`--mono` 18 px). Timer is 11 px mono in `--text`, flips to `--danger` under 1 h (`⏱ 40m` card in the fixture), `closed · sold` / `closed · no bid` in `--dim` when `outcome_complete`.
  - Everything else is 400/500: title sans 14/500, meta sans 12 muted, bids in `--accent` only when > 0.
  - Hover is `border-color: var(--border-strong)` only — no shadow, no lift.
- **No hard-coded hex outside `tokens.css`** — pass.
  - `tests/web/test_ui_primitives.py::test_no_hardcoded_hex_outside_tokens` is green over `static/{ui,deals,admin,site}/*.css`.
  - `components.css` uses two `rgba()` values (the body dot-grid from `app.css:47-52`, and the drawer scrim) — those are not hex and carry no palette meaning.
- **Empty / error copy in the interface's voice** — pass.
  - Empty: `No matches for "banquet chairs" in Vehicles` · `There are 24 matches without that filter.` · CTA `Search everywhere`.
  - Error: `Something didn't load` · `Couldn't load lots. The database didn't answer in 15 s.` · `Retry`; the live demo produces `The server said 503.` and `The database didn't answer in 2 s.` from real rejections/timeouts.
  - Stale: `stale · 3 min` badge; refetch with `keepOld` shows `refreshing · just now` over 55 %-dimmed content with `aria-busy="true"` (probe confirmed badge + busy mid-flight, both cleared on settle).
- **All seven states render** — pass (`ex-skeleton-card`, `ex-skeleton-tr`, `ex-empty`, `ex-error`, `ex-stale`, `ex-pending`, `ex-card`, plus row/line/pill skeletons).
  - `UI.load` demo: resolve → `ready`, empty → `empty`, reject 503 → `error`, 2 s timeout → `error`, keepOld → `ready`; `aria-busy` never left behind.
  - `UI.pending`: button reads `SAVING…`, `.is-pending`, disabled; restores label + enabled after the promise; toast fires.
- **Layout** — pass.
  - No horizontal overflow at either width (`scrollWidth == clientWidth`). Two bugs found and fixed during review: `1fr` grid columns were sized by the clamped title's min-content (now `minmax(0,1fr)` + `.grid > * { min-width: 0 }`); the closed `.drawer` widened the page (now `visibility: hidden` when closed, same for the mobile rail).
  - 1280: rail 240 px + 4-col grid, sticky topbar with search input, rail collapses to 56 px via `‹` and persists in `localStorage.ui.rail`.
  - 390: rail becomes a left drawer (`Filters` button opens, scrim + `Close` + Esc close; scrim paints `rgba(0,0,0,.6)`), topbar search collapses to `⌕`, brand shows the mark only, 1-col cards.
  - 4-col cards: the band's `GovDeals ↗` drops the word below a 280 px card (container query) so the category stays readable.
- **Hard rules** — pass.
  - No `storage_note` anywhere in the preview HTML (asserted by test). Fixture rows are `/deals/api/lots`-shaped: no photos, verdicts, `distance_mi`, seller, description.
  - `_tracking_loop` untouched; `public_deals.py` untouched; `app.py` diff is the three §5 lines.

## Not verified here
- Real fonts under a real network: the throwaway run had Google Fonts reachable, so JetBrains Mono / Instrument Serif / IBM Plex Sans rendered; fallbacks were not eyeballed.
- The operator's own :8765 process was not restarted (that is the operator's server; the branch has to land first).
