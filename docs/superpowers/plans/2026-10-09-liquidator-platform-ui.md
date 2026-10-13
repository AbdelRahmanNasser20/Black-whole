# Liquidator Platform UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three public, read-only pages on top of the `/platform` draft (PR #117): a `/liquidators` landing page, a `/platform/bankruptcies` sample demo, and a `/platform/deals` unified deal feed.

**Architecture:** Plain `def` FastAPI handlers that read nothing from the DB; Jinja templates on `_public_base.html` (light theme); one new stylesheet `static/site/platform-tools.css` (`lq-`/`pt-` prefixes, tokens only, no hex) and two ES modules. Bankruptcies = static sample JSON fixture. Deals = the existing `/platform/api/auctions` + `/platform/api/sites` (policy lives in `platform_api.py` / `public_deals.py`, no photos exist in that contract).

**Tech Stack:** FastAPI, Jinja2, vanilla ES modules (`static/ui/state.js` `api/esc/fmt`), pytest + TestClient.

**Spec:** the task brief (this file is the spec). Branch `feat/liquidator-platform-ui` off `feat/platform-demo`.

## Global Constraints

- No new DB tables, no migrations, no DB read in any new page handler.
- Honest copy: no invented customers or metrics, every sample is labelled "Sample data, invented", AI search is only ever "Coming".
- Never show auction photos (`/platform/api/auctions` has none; the deals template/JS must not render `<img>`).
- Sample debtor names must be obviously fake (`Sample …`, `Example …`); no email/phone/address in the fixture.
- CSS: tokens only (`tests/web/test_ui_primitives.py::test_no_hardcoded_hex_outside_tokens` covers `static/site`); JS: no raw `fetch(` (use `api` from `ui/state.js`).
- New pages are `noindex,nofollow`, not in the storefront nav/footer, not in the sitemap (same stance as `/platform`). Don't touch `platform.html` — its test forbids a `href="/platform` on the page.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Design plan (frontend-design pass)

- **Subject:** liquidation companies. Lead side = court dockets / distress signals. Sell side = a buyer network (churches, venues, event companies).
- **Palette:** storefront light tokens only — `--bg` cream, `--text` ink, `--accent` orange, `--surface-2` warm grey, `--info` blue (for "lead" track), `--success` green (for "sell" track). No new hex.
- **Type:** Archivo Black (`--display`) uppercase headlines as on the storefront; Fraunces (`--sans`) body; JetBrains Mono (`--mono`) for docket numbers / table data.
- **Layout, /liquidators:** hero split: headline left, a tall "sample docket" strip right (5 sample filings, labelled). Then the two tracks side by side (Find / Sell) as ruled lists, a numbered process (it is a sequence: watch → score → buy → list → buyers), a sample lead table, build-status line, contact form. Left-aligned throughout; rules not cards.
- **Layout, tool pages:** a sticky tool bar (search + filters), a ruled table, a right-hand drawer for detail. On phones the table becomes stacked records and the drawer becomes a bottom sheet.
- **The one bold element:** the sample docket strip in the hero. Everything else quiet.
- **Review against the generic default:** dropped the "big number + label + gradient" hero, dropped identical feature cards (ruled two-column lists instead), numbers kept only for the real sequence.

## Review Focus

1. `/platform/api/auctions` answers `ok:false` or zero items → deals page shows a direction ("None open in the feed right now. Show closed auctions.") not a blank table. (Task 4 JS, test checks the empty-state copy exists in the template/JS.)
2. A visitor types into Ask AI → a clear "not built yet" state, no network call. (Task 3 test: no `fetch(`/`api(` in the ask handler path; template carries the coming-soon copy.)
3. Fixture drift → a sample row with a real-looking name or a contact field. (Task 3 test on the JSON.)
4. The contact form on `/liquidators` sends with an empty name/email → client-side message, no POST. (Mirrors `platform.js`.)
5. A page handler reaches the DB → test's `no_db` fixture raises. (Every route test uses it.)

---

### Task 1: Fixture + `/liquidators` landing page
**Files:** create `automation/web/static/site/platform/bankruptcies.sample.json`, `automation/web/templates/liquidators.html`, `automation/web/static/site/platform-tools.css`, `automation/web/static/site/liquidators.js`; modify `automation/web/app.py` (route + sample loader); test `tests/web/test_liquidator_pages.py`.
- [ ] Write failing route/copy tests → run → build → pass → commit.

### Task 2: `/platform/bankruptcies` demo
**Files:** create `templates/platform_bankruptcies.html`, `static/site/platform/bankruptcies.js`; modify `app.py`; tests in the same file.
- [ ] Tests (sample banner, filter ids, ask-ai coming-soon, drawer, fixture hygiene) → build → pass → commit.

### Task 3: `/platform/deals` unified feed
**Files:** create `templates/platform_deals.html`, `static/site/platform/deals.js`; modify `app.py`; tests.
- [ ] Tests (route 200 no DB, source strip incl. planned placeholders, no `<img`, uses `/platform/api/auctions`, empty-state copy) → build → pass → commit.

### Task 4: Docs line + full `tests/web` run + draft PR
- [ ] `docs/claude-reference/repo-layout.md` one line per route; `.venv/bin/python -m pytest tests/web -q`; push; `gh pr create --draft`.
