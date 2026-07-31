"""Rate-limit-aware backoff for the LLM quantity pass.

Root cause this locks down (2026-07-20 outage — the admin Auctions tab went
empty): Groq's free tier caps at 8,000 tokens per MINUTE. A full GovDeals run
is ~56 chunks x ~3k tokens, so the TPM budget is spent after ~3 chunks. The
generic retry (`_RETRIES=2`, backoff 1.5s then 3.0s) gives up after ~4.5s —
far short of the ~60s a saturated TPM window needs to roll over. So chunks
4..56 all failed, BLACKWHOLE-4 NULLed their quantity, and the read path
(`WHERE quantity >= 50`) dropped every one of them: 625/661 rows invisible.

Observed prod shape: 36 rows `llm` (exactly 3 chunks) + 625 `llm_failed`.

A 429 is different from a 503: it is *guaranteed recoverable* and the server
tells us exactly how long to wait ("Please try again in 4.065s"). So rate
limits get their own, larger retry budget and honor the server's hint, instead
of sharing the generic transient-error budget.

Run standalone: ``python tests/test_quantity_rate_limit.py`` or via pytest.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quantity_llm


class _FakeRateLimit(Exception):
    """Mimics openai.RateLimitError: has .status_code and a TPM message."""

    status_code = 429

    def __init__(self, wait: str = "4.065s"):
        super().__init__(
            "Rate limit reached for model `openai/gpt-oss-120b` in organization "
            "`org_x` service tier `on_demand` on tokens per minute (TPM): "
            f"Limit 8000, Used 6829, Requested 1713. Please try again in {wait}."
        )


def _items():
    return [{"title": "Lot of (120) banquet chairs", "description": "120 stackable chairs"}]


def _refine(**kw):
    base = dict(
        provider="groq", ollama_base_url="", ollama_model="",
        ollama_timeout=1, groq_api_key="gk", openai_api_key=None,
        gemini_api_key=None,
    )
    base.update(kw)
    return quantity_llm.refine_quantities_with_llm(_items(), **base)


# ── the helpers ──────────────────────────────────────────────────────────────

def test_detects_rate_limit_errors():
    assert quantity_llm._is_rate_limit_error(_FakeRateLimit()) is True
    assert quantity_llm._is_rate_limit_error(RuntimeError("429 Too Many Requests")) is True
    assert quantity_llm._is_rate_limit_error(RuntimeError("tokens per minute (TPM)")) is True
    # A transient outage is NOT a rate limit — it keeps the generic budget.
    assert quantity_llm._is_rate_limit_error(RuntimeError("503 model overloaded")) is False
    assert quantity_llm._is_rate_limit_error(ValueError("GROQ_API_KEY not set")) is False


def test_parses_the_servers_retry_hint():
    # Groq phrasing, seconds.
    assert abs(quantity_llm._retry_after_seconds(_FakeRateLimit("4.065s")) - 4.065) < 1e-6
    # Minutes + seconds.
    assert abs(quantity_llm._retry_after_seconds(_FakeRateLimit("1m2s")) - 62.0) < 1e-6
    # No hint at all → None, caller falls back to exponential backoff.
    assert quantity_llm._retry_after_seconds(RuntimeError("429 slow down")) is None


def test_distinguishes_recoverable_limits_from_ones_waiting_cant_fix():
    """A per-minute limit clears on its own and is worth waiting out — even
    when the bucket is deeply backed up and the stated wait runs past 60s. A
    per-DAY cap or a depleted balance never clears inside the run."""
    tpm = _FakeRateLimit("95s")  # deeply backed-up leaky bucket, still TPM
    assert quantity_llm._is_unrecoverable_limit(tpm) is False
    assert quantity_llm._rate_limit_wait(tpm, attempt=0) is not None

    # Groq's daily cap — the real message from the 2026-07-20 investigation.
    tpd = Exception(
        "Rate limit reached ... on tokens per day (TPD): Limit 100000, Used 96933, "
        "Requested 7340. Please try again in 1h1m31.872s."
    )
    assert quantity_llm._is_unrecoverable_limit(tpd) is True
    assert quantity_llm._rate_limit_wait(tpd, attempt=0) is None
    # Hours are parsed, not silently dropped.
    assert abs(quantity_llm._retry_after_seconds(tpd) - (3600 + 60 + 31.872)) < 1e-6

    # Gemini reports a dead balance as a generic RESOURCE_EXHAUSTED 429, so the
    # status code alone can't tell us — the billing wording has to.
    broke = Exception(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Your prepayment "
        "credits are depleted. Please go to AI Studio to manage your plan'}}"
    )
    assert quantity_llm._is_rate_limit_error(broke) is True
    assert quantity_llm._is_unrecoverable_limit(broke) is True
    assert quantity_llm._rate_limit_wait(broke, attempt=0) is None


def test_depleted_provider_is_not_retried(monkeypatch):
    """Retrying a dead balance 6x with exponential backoff burns ~95s per chunk
    (~88min over a full run) and still fails. It must be tried exactly once."""
    monkeypatch.setattr(quantity_llm.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def depleted(*a, **k):
        calls["n"] += 1
        raise Exception("429 RESOURCE_EXHAUSTED: Your prepayment credits are depleted.")

    monkeypatch.setattr(quantity_llm, "_groq_chat", depleted)
    _refine()
    assert calls["n"] == 1


def test_long_cap_fails_over_instead_of_waiting():
    """A hint longer than the wait budget means a daily cap, not the
    per-minute window. Sleeping a *truncated* amount would retry too early and
    burn the whole budget (observed live: 6 x 75s wasted on one chunk), so the
    helper returns None = "don't wait, fail over to the next provider"."""
    exc = _FakeRateLimit("3600s")
    assert quantity_llm._retry_after_seconds(exc) == 3600.0
    assert quantity_llm._rate_limit_wait(exc, attempt=0) is None

    # A per-minute window IS worth waiting out, and is never truncated.
    short = _FakeRateLimit("4.065s")
    waited = quantity_llm._rate_limit_wait(short, attempt=0)
    assert waited is not None and 4.0 <= waited <= quantity_llm._RATE_LIMIT_MAX_WAIT


def test_long_cap_reaches_the_fallback_provider(monkeypatch):
    """End of the chain: a daily-capped primary must hand the chunk to the
    fallback rather than marking it llm_failed."""
    monkeypatch.setattr(quantity_llm.time, "sleep", lambda s: None)
    calls = {"groq": 0, "gemini": 0}

    def capped_groq(api_key, prompt, model="llama-3.3-70b-versatile"):
        calls["groq"] += 1
        raise _FakeRateLimit("3600s")

    def ok_gemini(api_key, prompt, model="gemini-2.5-flash"):
        calls["gemini"] += 1
        return '[{"i":0,"quantity":120,"confidence":"high"}]'

    monkeypatch.setattr(quantity_llm, "_groq_chat", capped_groq)
    monkeypatch.setattr(quantity_llm, "_gemini_chat", ok_gemini)

    out = _refine(gemini_api_key="gm")
    assert out[0]["quantity"] == 120
    assert out[0]["quantity_source"] == "llm"
    assert calls["groq"] == 1   # no pointless sleep-and-retry loop
    assert calls["gemini"] == 1


# ── the behaviour that was broken ────────────────────────────────────────────

def test_rate_limited_chunk_waits_out_the_window_and_recovers(monkeypatch):
    """The 2026-07-20 outage: chunk 429s more times than the generic budget
    allows. It must still recover rather than collapse to llm_failed."""
    slept: list[float] = []
    monkeypatch.setattr(quantity_llm.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(quantity_llm, "_RETRIES", 2)  # generic budget, as in prod

    state = {"n": 0}

    def throttled_groq(api_key, prompt, model="llama-3.3-70b-versatile"):
        state["n"] += 1
        if state["n"] <= 4:  # exceeds the generic 2-retry budget
            raise _FakeRateLimit()
        return '[{"i":0,"quantity":120,"confidence":"high"}]'

    monkeypatch.setattr(quantity_llm, "_groq_chat", throttled_groq)

    out = _refine()
    assert out[0]["quantity"] == 120
    assert out[0]["quantity_source"] == "llm"
    # It honored the server's 4.065s hint rather than the 1.5s generic backoff.
    assert slept and all(s >= 4.0 for s in slept), slept


def test_rate_limit_exhausted_still_marks_failed_and_nulls_quantity(monkeypatch):
    """BLACKWHOLE-4 must survive: a never-recovering limit still NULLs the
    regex seed rather than shipping it as trusted."""
    monkeypatch.setattr(quantity_llm.time, "sleep", lambda s: None)

    def always_throttled(*a, **k):
        raise _FakeRateLimit()

    monkeypatch.setattr(quantity_llm, "_groq_chat", always_throttled)
    monkeypatch.setattr(quantity_llm, "_RATE_LIMIT_RETRIES", 2)

    out = quantity_llm.refine_quantities_with_llm(
        [{"title": "Lot of 200 chairs", "quantity": 200, "quantity_source": "regex_title"}],
        provider="groq", ollama_base_url="", ollama_model="", ollama_timeout=1,
        groq_api_key="gk", openai_api_key=None, gemini_api_key=None,
    )
    assert out[0]["quantity_source"] == "llm_failed"
    assert out[0]["quantity"] is None


def test_generic_errors_keep_the_smaller_budget(monkeypatch):
    """A 503 must NOT get the rate-limit budget — it is not guaranteed
    recoverable, and burning 6 long sleeps on it would stall the run."""
    monkeypatch.setattr(quantity_llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(quantity_llm, "_RETRIES", 1)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("503 model overloaded")

    monkeypatch.setattr(quantity_llm, "_groq_chat", boom)
    _refine()
    assert calls["n"] == 2  # 1 initial + 1 generic retry, not the rate-limit budget


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([os.path.abspath(__file__), "-v"]))
