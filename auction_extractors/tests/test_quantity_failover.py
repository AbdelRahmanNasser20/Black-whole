"""Failover + retry for the LLM quantity pass.

Root cause this locks down: a single provider is not reliable enough to carry a
full daily scrape. Gemini suffers transient 503 outages (727/727 chunks failed
2026-06-22); Groq's free tier caps at 100k tokens/day and 429s the back two
thirds of a ~795-listing run. Either way the old single-provider dispatch
marked every affected chunk ``llm_failed``, the quantity collapsed to the regex
fallback, and qty<50 lots were filtered off the site (the Fresno / maroon
chairs vanished).

The fix: per chunk, try the primary provider with a couple of retries, then
fall back to the next configured provider before giving up. Run standalone:
``python tests/test_quantity_failover.py`` or via pytest.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quantity_llm


def _items():
    return [{"title": "Lot of (120) banquet chairs", "description": "120 stackable chairs"}]


def _refine(monkeypatch, **kw):
    monkeypatch.setattr(quantity_llm, "_BACKOFF_BASE", 0.0)  # no real sleeping in tests
    base = dict(
        provider="gemini", ollama_base_url="", ollama_model="",
        ollama_timeout=1, groq_api_key="gk", openai_api_key=None,
        gemini_api_key="gm",
    )
    base.update(kw)
    return quantity_llm.refine_quantities_with_llm(_items(), **base)


def test_falls_back_to_secondary_when_primary_errors(monkeypatch):
    calls = {"gemini": 0, "groq": 0}

    def boom_gemini(api_key, prompt, model="gemini-2.5-flash"):
        calls["gemini"] += 1
        raise RuntimeError("503 model overloaded")

    def ok_groq(api_key, prompt, model="llama-3.3-70b-versatile"):
        calls["groq"] += 1
        return '[{"i":0,"quantity":120,"confidence":"high"}]'

    monkeypatch.setattr(quantity_llm, "_gemini_chat", boom_gemini)
    monkeypatch.setattr(quantity_llm, "_groq_chat", ok_groq)
    monkeypatch.setattr(quantity_llm, "_RETRIES", 1)

    out = _refine(monkeypatch)
    assert out[0]["quantity"] == 120
    assert out[0]["quantity_source"] == "llm"
    assert calls["gemini"] == 2  # 1 initial try + 1 retry
    assert calls["groq"] == 1    # fallback fired exactly once


def test_retry_recovers_primary_without_fallback(monkeypatch):
    state = {"n": 0}

    def flaky_gemini(api_key, prompt, model="gemini-2.5-flash"):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("503 transient")
        return '[{"i":0,"quantity":80,"confidence":"medium"}]'

    monkeypatch.setattr(quantity_llm, "_gemini_chat", flaky_gemini)
    monkeypatch.setattr(quantity_llm, "_RETRIES", 2)

    out = _refine(monkeypatch, groq_api_key=None)  # no fallback available
    assert out[0]["quantity"] == 80
    assert out[0]["quantity_source"] == "llm"
    assert state["n"] == 2  # failed once, recovered on retry


def test_all_providers_failing_marks_llm_failed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(quantity_llm, "_gemini_chat", boom)
    monkeypatch.setattr(quantity_llm, "_groq_chat", boom)
    monkeypatch.setattr(quantity_llm, "_RETRIES", 0)

    out = _refine(monkeypatch)
    assert out[0]["quantity_source"] == "llm_failed"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([os.path.abspath(__file__), "-v"]))
