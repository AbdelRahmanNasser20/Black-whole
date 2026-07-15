"""Trust gate for chair quantities (BLACKWHOLE-4, read-side fix).

Only an LLM-verified count (`quantity_source == 'llm'`) is trusted for
ranking / the MIN_CHAIR_QUANTITY filter / Telegram alerts. Regex-seeded
counts and LLM failures must NOT reach those surfaces, and an LLM failure
must NULL the regex seed rather than shipping it as trusted.

Run standalone (no pytest needed):  python tests/test_quantity_trust.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quantity_llm
from top_chairs import TRUSTED_QUANTITY_SOURCE, trusted_quantity


def test_trusted_quantity_only_trusts_llm_source() -> None:
    # LLM-verified → the count is returned as an int.
    assert trusted_quantity({"quantity": 200, "quantity_source": "llm"}) == 200
    assert trusted_quantity({"quantity": "150", "quantity_source": "llm"}) == 150

    # Every non-LLM source is untrusted → None (never coerced to 0 / kept).
    for src in ("regex_title", "regex_fulltext", "llm_failed", "llm_missing", "", None):
        assert trusted_quantity({"quantity": 110, "quantity_source": src}) is None, src

    # Missing source key, or a trusted source with an unusable quantity → None.
    assert trusted_quantity({"quantity": 110}) is None
    assert trusted_quantity({"quantity": None, "quantity_source": "llm"}) is None
    assert trusted_quantity({"quantity": "n/a", "quantity_source": "llm"}) is None


def test_llm_chunk_failure_nulls_the_regex_seed() -> None:
    # provider=groq with no key → the chunk call raises → _mark_chunk_failed.
    # The regex seed (200) must be dropped, not shipped as trusted.
    listings = [{"title": "Lot of 200 chairs", "quantity": 200, "quantity_source": "regex_title"}]
    out = quantity_llm.refine_quantities_with_llm(
        listings,
        provider="groq",
        ollama_base_url="",
        ollama_model="",
        ollama_timeout=1,
        groq_api_key=None,
        openai_api_key=None,
    )
    assert out[0]["quantity_source"] == "llm_failed"
    assert out[0]["quantity"] is None
    assert trusted_quantity(out[0]) is None


def test_llm_missing_row_nulls_seed_success_row_trusted() -> None:
    # Stub the provider so index 0 comes back but index 1 is omitted.
    def _fake_groq(api_key: str, prompt: str, model: str = "") -> str:
        return '[{"i": 0, "quantity": 42, "confidence": "high"}]'

    orig = quantity_llm._groq_chat
    quantity_llm._groq_chat = _fake_groq
    try:
        listings = [
            {"title": "Lot of 42 stackable chairs", "quantity": 999, "quantity_source": "regex_title"},
            {"title": "Armless Rolling Chairs (217285 NB)", "quantity": 106, "quantity_source": "regex_fulltext"},
        ]
        out = quantity_llm.refine_quantities_with_llm(
            listings,
            provider="groq",
            ollama_base_url="",
            ollama_model="",
            ollama_timeout=1,
            groq_api_key="test-key",
            openai_api_key=None,
        )
    finally:
        quantity_llm._groq_chat = orig

    # Row 0: LLM answered → overwrites the regex seed, source llm, trusted.
    assert out[0]["quantity"] == 42
    assert out[0]["quantity_source"] == "llm"
    assert trusted_quantity(out[0]) == 42

    # Row 1: absent from the LLM response → seed nulled, untrusted.
    assert out[1]["quantity"] is None
    assert out[1]["quantity_source"] == "llm_missing"
    assert trusted_quantity(out[1]) is None


def test_trusted_source_constant() -> None:
    assert TRUSTED_QUANTITY_SOURCE == "llm"


if __name__ == "__main__":
    test_trusted_quantity_only_trusts_llm_source()
    test_llm_chunk_failure_nulls_the_regex_seed()
    test_llm_missing_row_nulls_seed_success_row_trusted()
    test_trusted_source_constant()
    print("ok — quantity trust gate")
