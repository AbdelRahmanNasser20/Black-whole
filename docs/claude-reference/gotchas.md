<!-- Moved verbatim from ../../CLAUDE.md on 2026-08-28 (trim to <=8 KB). Original kept as ../../CLAUDE.md.pre-trim-2026-08-28 -->

## Gotchas (all fixed — don't reintroduce)

| # | Symptom | Cause | Fix landed in |
|---|---------|-------|---------------|
| 1 | Dashboard `/` returns 500 | starlette 1.0 changed `TemplateResponse` signature | `automation/web/app.py` — `TemplateResponse(request, name, ctx)` |
| 2 | `--login-only` exits instantly under `!` prefix | no TTY → `sys.stdin.readline()` returns EOF immediately | `run.py::_login_only` — wait for `ctx.on('close')` event instead of stdin |
| 3 | `Failed to create ProcessSingleton` on launch | prior Chromium still alive holding the profile lock | `automation/browser.py::_clear_stale_profile_lock` — kills PID from `SingletonLock` symlink target + unlinks all `Singleton*` files |
| 4 | LLM phase hangs forever in dashboard | `ClaudeCodeExtractor` waits on stdin nobody feeds | `automation/llm/dom_fallback.py` + `default_extractors()` env-aware picker (auto: gemini → claude_code-if-TTY → dom) |
| 5 | 0 images downloaded; folder has only `_screenshots/` | GovDeals modernized to lightGallery v2 (`.lg-object`); some images render via `srcset` / CSS bg / inline JSON | `govdeals.py` `EXTRACT_JS` — scans `<img src/srcset/data-src>`, `<source srcset>`, CSS `background-image`, AND raw-HTML regex for `webassets.lqdt1.com/.../photos/...` |
| 6 | Image fetch returns 403 from CDN | no `Referer` / no browser UA | `automation/downloader.py::DOWNLOAD_HEADERS` |
| 7 | Related-listing thumbnails leak into the upload | sidebar uses same `lqdt1.com/photos/<other_lot>/` URLs | `govdeals.py` post-filter: `f"/photos/{lot_id}/" in u` |
| 8 | `click.prompt` aborts under dashboard subprocess (`Aborted!`) | Even with `--price` flag passed, `click.prompt` still tries to read stdin; no TTY → EOF → abort | `run.py` — check `price_override is not None` first, else check `sys.stdin.isatty()` before prompting, else auto-accept suggestion. Same pattern for the "Press Enter to close" at end of run — replaced with a sleep loop under no-TTY. |
| 9 | Dewatermark silently shipped watermarked images | The old bottom-right histogram quality check, written for a small corner stamp, failed open against GovDeals' new full-image tiled `www.govdeals.com` watermark and rejected every cleaned API output | `automation/quality.py` — replaced histogram heuristic with pure byte-identity check (cleaned != original); trust dewatermark.ai's HTTP 200 |

**Other things worth knowing:**
- Headless mode hits Akamai 403 on GovDeals. Always non-headless. `persistent_context(headless=False)` is the default — don't flip it.
- `ClaudeCodeExtractor` only works when Claude Code is the orchestrator with a real TTY. From the dashboard subprocess it'll hang. The `default_extractors()` picker handles this automatically.
- Dewatermark is **dewatermark.ai API only** (`DEWATERMARK_API_KEY` in `.env`). No local image processing, no heuristic watermark detection. Every image not in the global response cache goes to the API; failures leave originals in `_originals/` and emit `dewatermark:degraded`.
