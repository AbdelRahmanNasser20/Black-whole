"""
Optional LLM pass to infer chair *quantity* from title + (optional) long description.

Designed for GovDeals where counts often appear only in the asset description.

Env (same providers as ranking): LLM_PROVIDER, OLLAMA_*, GROQ_API_KEY, OPENAI_API_KEY, OLLAMA_TIMEOUT.

Sustainability:
  - One batched JSON request per chunk of listings (default 12), not one HTTP call per row.
  - Groq/OpenAI: small cost per daily run; Ollama: no API cost, local GPU/CPU time.
  - Descriptions add N page loads if FETCH_GOVDEALS_DESCRIPTION=1 — main latency/courtesy concern.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests

# Chunk size keeps prompts small for free-tier limits and fast failures.
#
# 8, not the old 12: Groq's gpt-oss tier allows 8,000 tokens per MINUTE and
# counts `max_tokens` as requested up front, so an oversized single request is
# rejected 413 "Request too large" — a limit that genuinely does not clear by
# waiting, because the request alone exceeds the whole window. At 12 rows the
# worst case (12 x 3,000 chars of title+description ≈ 9,000 tokens, plus 4,000
# reserved) asked for ~13,000 against a ceiling of 8,000 and four of ten chunks
# died. See `_TPM_CEILING` below for the arithmetic that keeps this honest.
_CHUNK = int(os.getenv("LLM_QUANTITY_CHUNK", "8"))

# Per-chunk reliability knobs. No single free LLM is reliable enough to carry a
# full daily scrape on its own: Gemini suffers transient 503 outages and Groq's
# free tier caps at 100k tokens/day. So each chunk retries its primary provider
# a couple of times, then falls back to the next configured provider before
# giving up and marking the chunk llm_failed.
_RETRIES = int(os.getenv("QUANTITY_LLM_RETRIES", "2"))
# Backoff = _BACKOFF_BASE * 2**attempt seconds. 0 disables sleeping (tests).
_BACKOFF_BASE = float(os.getenv("QUANTITY_LLM_BACKOFF", "1.5"))

# Rate limits (429) get their OWN, larger budget, because a 429 usually tells
# us exactly how long to wait — unlike a 503, which may never recover and so
# keeps the small generic budget above.
#
# Groq's free tier enforces BOTH a per-minute token cap (8k-12k TPM, depending
# on model) and a per-DAY one (100k TPD). A full GovDeals run is ~56 chunks of
# 2.5k-7.6k tokens = 140k-400k tokens, which clears neither. The two failures
# need opposite responses, which is what the helpers below sort out:
#   - TPM: a leaky bucket that refills. Honor the stated wait; that pacing is
#     what lets the run finish. The generic 2 retries (1.5s + 3.0s) gave up
#     ~55s early, which is why chunks kept dying.
#   - TPD (and a depleted balance): will not clear inside this run. Waiting is
#     pure waste — fail over to the next provider immediately.
# Either way the old code marked the chunk llm_failed, BLACKWHOLE-4 NULLed its
# quantity, and the read path (`WHERE quantity >= 50`) dropped the row: on
# 2026-07-20 that hid 625 of 661 rows and emptied the admin Auctions tab.
_RATE_LIMIT_RETRIES = int(os.getenv("QUANTITY_LLM_RATE_LIMIT_RETRIES", "6"))
# Longest single wait worth taking, as a backstop for hints with no stated
# limit type. Per-minute limits are a leaky bucket: when a run is deeply over
# budget the stated wait can run well past 60s, and honoring it IS the pacing
# mechanism that lets the run finish — so this is generous, not tight.
_RATE_LIMIT_MAX_WAIT = float(os.getenv("QUANTITY_LLM_RATE_LIMIT_MAX_WAIT", "120"))

# Must match tests/test_quantity_accuracy.py / any tooling that shows "what the LLM sees"
LLM_QUANTITY_TITLE_MAX = 500
LLM_QUANTITY_DESC_MAX = int(os.getenv("LLM_QUANTITY_DESC_MAX", "1200"))

# Groq's free gpt-oss tier: 8,000 tokens/minute, and `max_tokens` is counted as
# requested the moment the call is made. A request whose prompt + reservation
# clears this is rejected 413 outright, so the three knobs above are not
# independent — `test_quantity_request_fits_the_tpm_ceiling` asserts their
# product stays inside it. Raise one and that test tells you which other to cut.
_TPM_CEILING = 8000
_CHARS_PER_TOKEN = 4  # standard rough ratio; deliberately pessimistic here

# Sized to the answer, not to the ceiling. The reply is one small JSON object
# per row (~25 tokens), and gpt-oss's reasoning for a counting task is short —
# but the reservation is charged in full against TPM whether it is used or not,
# so 4,000 was buying nothing and costing half the window.
_QUANTITY_MAX_TOKENS = int(os.getenv("LLM_QUANTITY_MAX_TOKENS", "2000"))


def _ollama_chat(base_url: str, model: str, prompt: str, timeout: int) -> str:
    url = f"{base_url.rstrip('/')}/api/chat"
    r = requests.post(
        url,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content") or ""


def _openai_chat(api_key: str, prompt: str, model: str = "gpt-4o-mini") -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=4000,
    )
    return resp.choices[0].message.content or ""


# Groq retired the whole Llama 3.x family on this account. Both models this
# repo had hardcoded — `llama-3.3-70b-versatile` here and `llama-3.1-8b-instant`
# in deals/llm_provider.py — now answer 404 model_not_found, which the quantity
# pass records as `llm_failed` for every row in the chunk. That is the immediate
# reason lot 420/9312 ("LOT: (199) BANQUET CHAIRS") never reached an alert.
#
# `openai/gpt-oss-120b` is the same family the upstream quantity_eval benchmark
# picked (auction_extractors/.env pins OLLAMA_MODEL=gpt-oss:120b-cloud), so the
# swap keeps the accuracy the benchmark measured. Watch the budget: gpt-oss on
# Groq's free tier allows 1,000 req/day, not the 14,400 Llama had. The chair
# sweep needs ~60 (BATCH=12 over ~740 rows) and fits; the deals classify crons
# do not, and are tracked separately.
#
# Env-overridable so the next retirement is a config change, not a deploy.
_GROQ_QUANTITY_MODEL = os.getenv("GROQ_QUANTITY_MODEL", "openai/gpt-oss-120b")


def _groq_chat(api_key: str, prompt: str, model: str = "") -> str:
    from openai import OpenAI

    model = model or _GROQ_QUANTITY_MODEL

    client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=_QUANTITY_MAX_TOKENS,
    )
    return resp.choices[0].message.content or ""


def _gemini_chat(api_key: str, prompt: str, model: str = "gemini-2.5-flash") -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    # Force a JSON response — without this, gemini-2.5-flash often "helpfully"
    # returns a Python script that *computes* the answer instead of the answer,
    # which fails our JSON parse and marks every row in the chunk llm_failed.
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
        ),
    )
    return resp.text or ""


def _claude_max_chat(prompt: str) -> str:
    """Run a prompt through the user's Claude Max subscription via the official
    ``claude-agent-sdk``. Strips all tools (we want a pure text completion),
    forces JSON-only output via the system prompt, and concatenates every
    assistant TextBlock into a single string the existing parser can read.
    """
    import asyncio
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    # Alias, not a pinned ID: the CLI resolves "sonnet" to its current Sonnet,
    # and the alias returns raw JSON where the pinned ID wraps it in ```json
    # fences (the parser strips them, but clean output is one less thing to
    # break). Sonnet is the right tier here — quantity extraction is short-text
    # structured output, but it IS load-bearing (a wheelchair misread as
    # qty=110 is the whole reason BLACKWHOLE-4 exists), so don't drop to Haiku.
    model = os.getenv("CLAUDE_MAX_MODEL", "sonnet")

    async def _run() -> str:
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=(
                "You output ONLY raw JSON. No prose, no markdown, no code fences."
            ),
            allowed_tools=[],
            permission_mode="default",
            max_turns=1,
        )
        chunks: list[str] = []
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
        return "".join(chunks)

    return asyncio.run(_run())


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    try:
        data = json.loads(t)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    start, end = t.find("["), t.rfind("]")
    if start != -1 and end != -1 and end > start:
        return json.loads(t[start : end + 1])
    raise ValueError("No JSON array in LLM response")


def _dispatch_provider(prov: str, prompt: str, cfg: dict[str, Any]) -> str:
    """Single text completion from one provider. Raises on missing key /
    unknown provider so the caller can fall through to the next provider."""
    if prov == "openai":
        if not cfg.get("openai_api_key"):
            raise ValueError("OPENAI_API_KEY not set")
        return _openai_chat(cfg["openai_api_key"], prompt)
    if prov == "groq":
        if not cfg.get("groq_api_key"):
            raise ValueError("GROQ_API_KEY not set")
        return _groq_chat(cfg["groq_api_key"], prompt)
    if prov == "gemini":
        key = cfg.get("gemini_api_key") or os.getenv("GEMINI_API_KEY")
        if not key:
            raise ValueError("GEMINI_API_KEY not set")
        return _gemini_chat(key, prompt, os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    if prov == "ollama":
        return _ollama_chat(
            cfg["ollama_base_url"], cfg["ollama_model"], prompt, cfg["ollama_timeout"]
        )
    if prov == "claude_max":
        return _claude_max_chat(prompt)
    raise ValueError(f"Unknown LLM provider: {prov}")


# "Please try again in 4.065s" / "... in 1m2s" / "... in 23h59m59s". Groq and
# OpenAI both phrase the wait this way; larger units are absent for short waits.
_RETRY_HINT_RE = re.compile(
    r"try again in\s+(?:([\d.]+)h)?\s*(?:([\d.]+)m)?\s*(?:([\d.]+)s)?", re.I
)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Is this a 429 / quota error (recoverable by waiting) rather than a
    generic transient failure like a 503 (which may never recover)?"""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("rate limit", "429", "tokens per minute", "tpm", "too many requests"))


def _retry_after_seconds(exc: Exception) -> float | None:
    """How long the server asked us to wait, or None if it didn't say."""
    hdrs = getattr(getattr(exc, "response", None), "headers", None) or {}
    for key in ("retry-after", "Retry-After"):
        raw = hdrs.get(key) if hasattr(hdrs, "get") else None
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    m = _RETRY_HINT_RE.search(str(exc))
    if not m or not any(m.groups()):
        return None
    h, mi, s = m.groups()
    return float(h or 0) * 3600.0 + float(mi or 0) * 60.0 + float(s or 0)


# A 429 that will NOT clear by waiting: a per-day cap, or an account that is
# simply out of credit. Retrying these is pure waste — a depleted Gemini key
# retried 6x with exponential backoff burns ~95s per chunk (~88min over a full
# 56-chunk run) and still fails. Fail over to the next provider immediately.
_UNRECOVERABLE_LIMIT_MARKERS = (
    "per day", "(tpd)", "(rpd)", "daily",
    "credits are depleted", "credits depleted", "prepayment",
    "billing", "insufficient_quota", "insufficient quota",
)


def _is_unrecoverable_limit(exc: Exception) -> bool:
    """Is this a 429 that waiting cannot fix (daily cap / no credit) rather
    than the per-minute window that clears on its own?

    Groq and OpenAI name the limit in the message ("tokens per day (TPD)");
    Gemini reports a depleted balance as a generic RESOURCE_EXHAUSTED 429, so
    we key on the billing wording rather than the status code.
    """
    msg = str(exc).lower()
    return any(s in msg for s in _UNRECOVERABLE_LIMIT_MARKERS)


def _rate_limit_wait(exc: Exception, attempt: int) -> float | None:
    """Seconds to sleep before retrying a rate-limited call, or None when
    waiting isn't worth it.

    Prefer the server's own hint, plus a small buffer so we don't wake a hair
    too early — honoring it is what paces the run to the allowed rate.

    Never sleep a *truncated* amount: retrying before the window clears just
    429s again and burns the retry budget (observed live: 6 x 75s wasted on one
    chunk, which is why the cap is a fail-over signal rather than a clamp).

    A limit that waiting cannot fix (daily cap, depleted credit), or a wait
    past the backstop, returns None = "don't wait, fail over now".
    """
    if _is_unrecoverable_limit(exc):
        return None
    hint = _retry_after_seconds(exc)
    if hint is None:
        return min(_BACKOFF_BASE * (2 ** attempt), _RATE_LIMIT_MAX_WAIT)
    if hint > _RATE_LIMIT_MAX_WAIT:
        return None
    return hint + 0.5


def _call_with_retries(prov: str, prompt: str, cfg: dict[str, Any]) -> str:
    """Call one provider, retrying transient failures with exponential backoff.
    Re-raises the last exception once retries are exhausted.

    Rate limits (429) draw on their own, larger budget and honor the server's
    stated wait — a saturated per-minute token window needs ~60s to roll over,
    which is far longer than the generic backoff allows. Generic failures keep
    the small budget so a hard-down provider fails fast to the next one.
    """
    last: Exception | None = None
    generic_used = 0
    rate_used = 0
    while True:
        try:
            return _dispatch_provider(prov, prompt, cfg)
        except Exception as e:  # noqa: BLE001 — any failure should fall through
            last = e
            if _is_rate_limit_error(e):
                if rate_used >= _RATE_LIMIT_RETRIES:
                    break
                wait = _rate_limit_wait(e, rate_used)
                if wait is None:
                    # Daily cap / no credit — waiting won't clear it this run.
                    print(f" ! quantity_llm {prov} limit will not clear by waiting; failing over")
                    break
                rate_used += 1
                if _BACKOFF_BASE > 0:
                    print(f"   ↳ quantity_llm {prov} rate-limited; waiting {wait:.1f}s (retry {rate_used}/{_RATE_LIMIT_RETRIES})")
                    time.sleep(wait)
            else:
                if generic_used >= _RETRIES:
                    break
                if _BACKOFF_BASE > 0:
                    time.sleep(_BACKOFF_BASE * (2 ** generic_used))
                generic_used += 1
    raise last  # type: ignore[misc]


def _provider_chain(primary: str, cfg: dict[str, Any]) -> list[str]:
    """Ordered list of providers to try for each chunk: primary first, then
    fallbacks. ``QUANTITY_LLM_FALLBACK`` (comma-separated) overrides the
    auto-selected fallbacks, which are simply whichever other providers have a
    key configured (groq → gemini → openai)."""
    chain = [primary]
    env_fb = (os.getenv("QUANTITY_LLM_FALLBACK") or "").strip().lower()
    if env_fb:
        candidates = [x.strip() for x in env_fb.split(",") if x.strip()]
    else:
        candidates = []
        if cfg.get("groq_api_key"):
            candidates.append("groq")
        if cfg.get("gemini_api_key") or os.getenv("GEMINI_API_KEY"):
            candidates.append("gemini")
        if cfg.get("openai_api_key"):
            candidates.append("openai")
    for p in candidates:
        if p and p != primary and p not in chain:
            chain.append(p)
    return chain


def refine_quantities_with_llm(
    listings: list[dict[str, Any]],
    *,
    provider: str,
    ollama_base_url: str,
    ollama_model: str,
    ollama_timeout: int,
    groq_api_key: str | None,
    openai_api_key: str | None,
    gemini_api_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Set quantity, quantity_confidence, quantity_source on each listing.
    Expects keys: title, optional description.
    """
    if not listings:
        return listings

    out = [dict(x) for x in listings]
    prov = (provider or "ollama").strip().lower()
    cfg = {
        "ollama_base_url": ollama_base_url,
        "ollama_model": ollama_model,
        "ollama_timeout": ollama_timeout,
        "groq_api_key": groq_api_key,
        "openai_api_key": openai_api_key,
        "gemini_api_key": gemini_api_key,
    }
    chain = _provider_chain(prov, cfg)
    if len(chain) > 1:
        print(f"   quantity_llm providers: {' → '.join(chain)} (retries={_RETRIES})")

    for start in range(0, len(out), _CHUNK):
        chunk = out[start : start + _CHUNK]
        rows = []
        for j, item in enumerate(chunk):
            title = (item.get("title") or "")[:LLM_QUANTITY_TITLE_MAX]
            desc = (item.get("description") or "")[:LLM_QUANTITY_DESC_MAX]
            rows.append({"i": start + j, "title": title, "description": desc})

        payload = json.dumps(rows, ensure_ascii=False)
        prompt = f"""You estimate how many CHAIRS (individual chair units) are in each auction lot.

Input (JSON array; each row has i, title, description). Description may be empty — use title only if so.

Return ONLY a JSON array of objects, ONE per input row, IN THE SAME ORDER as the input:
[{{"i": <same as input>, "quantity": <positive integer>, "confidence": "high"|"medium"|"low"|"unknown"}}]

Rules:
- Prefer explicit phrases: "lot of (10)", "approx 50 chairs", "Qty 200", etc.
- If the lot is clearly not a chair count (e.g. only model numbers), pick best-effort or 1 with confidence low/unknown.
- Numbers describing ROTATION, ANGLE, DIMENSIONS, WEIGHT, or PERCENTAGES are NOT chair counts. Examples:
  • "rotates 360 degrees" / "swivels 360°" → NOT 360 chairs
  • "45 inches tall" / "18\\" to 22\\" height" → NOT 45 / 22 chairs
  • "Weighs 25 lbs" / "holds 300 lbs" → NOT 25 / 300 chairs
  • "50% polyester" → NOT 50 chairs
  If the only numbers nearby are dimensions/angles/weights, return 1 with confidence low.
- A single retail product (e.g. "Herman Miller Caper Chair", "UMF Medical 8678 Phlebotomy Chair") is a lot of 1, even if its description mentions dimensions or rotation.
- Count ONLY the chairs included in THIS listing. Sellers often split one batch across
  several auctions and repeat the grand total in every description. Examples:
  • "105 chairs total across our listings, sold in groups of 1, 4, and 12" → NOT 105;
    use this listing's own count (title or per-lot statement). If unclear, return 1 with confidence low.
  • "Lot of (6) chairs. (2) other sets of 8 chairs are also being posted." → 6, NOT 22.
    Never add chairs from other listings mentioned in the description.
  • "There are an additional 2,100 chairs available in a separate auction" → ignore the 2,100.
- Never return 0; minimum 1.

INPUT:
{payload}
"""

        # ── chunk-level failure: tag every row in this chunk so the failure is
        # visible downstream (alerts, reports) instead of silently leaving the
        # regex fallback in place. The previous behavior masked exactly the
        # bug we hit when llama3.2 returned Python code instead of JSON.
        def _mark_chunk_failed(reason: str, raw_excerpt: str = "") -> None:
            short = reason[:200]
            for it in chunk:
                # Drop the regex seed rather than shipping it as a trusted count.
                # An unverified regex quantity (often a lot/model number) must
                # not survive an LLM failure — leave it None and let the next
                # run retry (BLACKWHOLE-4 AC4).
                it["quantity"] = None
                it["quantity_source"] = "llm_failed"
                it["quantity_confidence"] = "unknown"
                it["quantity_error"] = short
                if raw_excerpt:
                    it["quantity_llm_raw"] = raw_excerpt[:500]

        # Walk the provider chain: primary (with retries) first, then each
        # fallback. A provider "fails" if the call errors OR its output won't
        # parse as a JSON array; only when every provider fails do we mark the
        # chunk llm_failed. This is what stops a Gemini 503 outage or a Groq
        # daily-cap 429 from silently collapsing two thirds of the scrape.
        parsed = None
        raw = ""
        last_err = ""
        span = f"{start}-{start + len(chunk)}"
        for p in chain:
            try:
                raw = _call_with_retries(p, prompt, cfg)
            except Exception as e:  # noqa: BLE001
                last_err = f"call[{p}]: {e}"
                print(f" ! quantity_llm chunk {span} provider {p} call failed: {str(e)[:160]}")
                continue
            try:
                parsed = _parse_json_array(raw)
                if p != chain[0]:
                    print(f"   ↳ quantity_llm chunk {span} recovered via fallback {p}")
                break
            except Exception as e:
                last_err = f"parse[{p}]: {e}"
                print(f" ! quantity_llm chunk {span} provider {p} JSON parse failed: {str(e)[:160]}")
                continue
        if parsed is None:
            _mark_chunk_failed(last_err, raw_excerpt=raw)
            continue

        # Map by index i
        by_i: dict[int, dict] = {}
        for obj in parsed:
            if not isinstance(obj, dict):
                continue
            idx = obj.get("i")
            if idx is None:
                continue
            try:
                by_i[int(idx)] = obj
            except (TypeError, ValueError):
                continue

        for j, item in enumerate(chunk):
            global_idx = start + j
            row = by_i.get(global_idx)
            if not row:
                # Parsed JSON but this row is missing — mark it so it is
                # visible rather than blending in with successful predictions.
                # Null the regex seed so it isn't trusted (BLACKWHOLE-4 AC4).
                item["quantity"] = None
                item["quantity_source"] = "llm_missing"
                item["quantity_confidence"] = "unknown"
                item["quantity_error"] = "row absent from LLM response"
                continue
            try:
                q = int(row.get("quantity", 1))
                if q < 1:
                    q = 1
            except (TypeError, ValueError):
                q = 1
            conf = str(row.get("confidence") or "unknown").lower()
            if conf not in ("high", "medium", "low", "unknown"):
                conf = "unknown"
            item["quantity"] = q
            item["quantity_confidence"] = conf
            item["quantity_source"] = "llm"

    return out
