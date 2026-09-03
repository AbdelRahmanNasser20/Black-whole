<!-- Moved verbatim from ../../CLAUDE.md on 2026-08-28 (trim to <=8 KB). Original kept as ../../CLAUDE.md.pre-trim-2026-08-28 -->

## Dewatermark behavior — READ BEFORE TOUCHING

**dewatermark.ai API only.** No IOPaint, no local inpainting, no heuristic watermark detection — the API is the watermark remover. Every image not already in the global response cache is sent to `https://platform.dewatermark.ai/api/object_removal/v2/erase_watermark` with `X-API-KEY: $DEWATERMARK_API_KEY`.

- `automation/dewatermark_cache.py` owns three layers consulted before any API call:
  1. Per-folder sidecar `<folder>/.dewatermark_state.json` keyed by sha256 of the original. Records method (`api_cache` / `api`), status (`clean` / `failed`), api_calls counter.
  2. Global response cache at `~/.listing_automation/api_cache/<sha>.bin`. Survives lot deletions and machine restarts. Once a hash is here, **no future run anywhere on this machine ever calls the API for that hash again.**
  3. `RunBudget` (per-run counter + 24h rolling window) gates each call. Default caps: `MAX_API_CALLS_PER_RUN=50`, `MAX_API_CALLS_PER_DAY=250`. Override in `.env`.
- **`DEWATERMARK_OFFLINE` defaults OFF.** The real API runs on every un-cached image. Set `DEWATERMARK_OFFLINE=1` in `.env` to freeze spend while developing.
- **No fallback when the API fails.** If the API errors or returns byte-identical bytes to the input, the sidecar marks the hash `failed`, the original stays in `_originals/`, and a `dewatermark:degraded` event is emitted. There is nothing else to try.
- **Sanity check on API output.** `quality.watermark_likely_present()` does one thing: reject only if the cleaned bytes are missing, empty, or equal the original. Don't reintroduce histogram/heuristic checks — the GovDeals watermark is semi-transparent and tiled full-image, so pixel-delta thresholds can't distinguish clean from dirty (see Gotcha #9).
- Audit tools (no API calls):
  - `python -m automation.dewatermark stats` — today/month/all-time call counts + global cache size
  - `python -m automation.dewatermark verify <path-to-cleaned-file>` — byte-compares against the matching `_originals/<file>` and prints `clean` or `watermarked`
