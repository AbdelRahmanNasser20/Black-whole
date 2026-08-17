"""The quantity request must fit inside the provider's per-minute window.

Groq's free gpt-oss tier allows 8,000 tokens/minute and charges `max_tokens` as
requested the instant the call is made, so an oversized request is rejected 413
"Request too large" before the model sees it. That rejection does NOT clear by
waiting — the request alone exceeds the whole window — so the retry layer
correctly fails it over, and with Gemini's balance depleted there is nowhere to
fail over to. Every row in the chunk lands `llm_failed`, which is invisible to
`trusted_quantity` and therefore invisible to the operator.

Observed live on 2026-08-17: chunk size 12 x 3,000 chars + 4,000 reserved asked
for ~13,000 tokens, and four of ten chunks died that way.

Chunk size, description cap, and max_tokens are three knobs on one budget. This
test is what stops someone raising one without cutting another.

Run standalone (no pytest needed):
    python auction_extractors/tests/test_quantity_request_size.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quantity_llm as q


def _worst_case_tokens() -> int:
    """Upper bound on tokens a single chunk can request.

    Prompt = the per-row payload at its caps, plus the fixed instruction block
    (measured generously at 600 tokens), plus the output reservation.
    """
    per_row_chars = q.LLM_QUANTITY_TITLE_MAX + q.LLM_QUANTITY_DESC_MAX
    prompt_tokens = (q._CHUNK * per_row_chars) // q._CHARS_PER_TOKEN
    return prompt_tokens + 600 + q._QUANTITY_MAX_TOKENS


def test_quantity_request_fits_the_tpm_ceiling() -> None:
    worst = _worst_case_tokens()
    assert worst <= q._TPM_CEILING, (
        f"a full chunk can request ~{worst} tokens against a {q._TPM_CEILING} "
        f"tokens/minute ceiling — it will 413 and the whole chunk becomes "
        f"llm_failed. Cut _CHUNK ({q._CHUNK}), LLM_QUANTITY_DESC_MAX "
        f"({q.LLM_QUANTITY_DESC_MAX}), or _QUANTITY_MAX_TOKENS "
        f"({q._QUANTITY_MAX_TOKENS})."
    )


def test_the_reservation_still_covers_a_full_answer() -> None:
    """Trimming max_tokens too far truncates the JSON and fails the parse —
    the same llm_failed outcome by a different route. One row's answer is
    ~25 tokens; leave room for that plus the model's reasoning."""
    answer_tokens = q._CHUNK * 25
    assert q._QUANTITY_MAX_TOKENS >= answer_tokens * 4, (
        f"_QUANTITY_MAX_TOKENS={q._QUANTITY_MAX_TOKENS} leaves too little room "
        f"for {q._CHUNK} rows of JSON plus reasoning"
    )


def test_the_groq_model_is_not_a_retired_one() -> None:
    """Groq retired the Llama 3.x family on this account on 2026-08-17 and every
    call returned 404 model_not_found. A hardcoded model id is a dependency on
    someone else's deprecation schedule."""
    assert "llama-3" not in q._GROQ_QUANTITY_MODEL, (
        f"{q._GROQ_QUANTITY_MODEL} is retired on Groq — verify any replacement "
        f"with scripts/check_llm_provider.py before relying on it"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print(f"all request-size tests passed (worst case ~{_worst_case_tokens()} "
          f"of {q._TPM_CEILING} tokens)")
