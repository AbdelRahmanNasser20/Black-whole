"""Canonical-category classification for a lot.

Transport (provider choice, pacing, circuit breaker) lives in
`deals/llm_provider.py` and is shared with the analyze pass. What's left here is
the one thing specific to classification: the prompt, and the rule that a
failure is not an answer.

**The failure this file was rewritten to prevent.** The original swallowed every
exception and returned `("other", 0.0)`, which is indistinguishable from a
genuine "I read it and it's other". The table ended up holding **39,758 lots
classified, every one `other` at confidence 0.0, zero real answers**, and
nothing anywhere said so. A classifier that can't reach its model must leave
`llm_category` NULL — an empty column is a question you can still answer later;
a fake label is a wrong answer nobody will ever go back and check.
"""
import json

from deals.llm_provider import (LlmUnavailable, active_provider, breaker_state,  # noqa: F401
                                chat, reset_breaker)
from deals.models import Lot

CANONICAL_LABELS = ["seating_furniture", "general_merchandise", "vehicles",
                    "collectibles_jewelry", "computers_electronics", "other"]

_INSTRUCTIONS = (
    "Classify this liquidation lot into exactly one label from this list: "
    + ", ".join(CANONICAL_LABELS) +
    ". Respond ONLY as compact JSON: {\"label\": <one label>, \"confidence\": <0..1>}."
)


class ClassificationUnavailable(LlmUnavailable):
    """Kept as a distinct name because callers catch it by meaning, not by
    transport. Subclasses `LlmUnavailable` so a bare provider failure is caught
    by either name."""


def build_prompt(title: str, description: str) -> str:
    """Assemble the prompt by concatenation, NOT `str.format`.

    The original called `_PROMPT.format(title=…, desc=…)` on a template that
    contains the literal JSON example `{"label": …}`. `str.format` reads that as
    a replacement field named `"label"` and raises `KeyError('"label"')` — on
    the first statement inside the try, before any network call. Wrapped in a
    bare `except`, that produced 39,758 lots labelled `other`/0.0 without a
    single request ever leaving the machine. Escaping the braces would also
    work, and would break again the next time someone edits the example; not
    running `.format` over a string that contains JSON is the fix that stays
    fixed.
    """
    return (f"{_INSTRUCTIONS}\n"
            f"Title: {title[:200]}\n"
            f"Description: {(description or '')[:800]}")


def parse_response(text: str) -> tuple[str, float]:
    """Pull (label, confidence) out of a model's reply.

    Models fence JSON in markdown about as often as not, so the fences are
    stripped rather than trusted. An unrecognised label is coerced to `other`
    *keeping the model's own confidence* — that's a real answer we merely can't
    map, which is not the same as an unreachable model.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
        cleaned = cleaned.strip()
    cleaned = cleaned.removeprefix("json").strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as e:
        raise ClassificationUnavailable(
            f"unparseable model reply: {(text or '')[:120]!r}") from e
    if not isinstance(data, dict):
        raise ClassificationUnavailable(
            f"model reply was not an object: {(text or '')[:120]!r}")
    label = data.get("label", "other")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return (label if label in CANONICAL_LABELS else "other", confidence)


def classify_category(title: str, description: str) -> tuple[str, float]:
    """Place a lot in our taxonomy. Raises `ClassificationUnavailable` on any
    provider problem — the caller decides what an absent answer means."""
    try:
        text = chat(build_prompt(title, description), max_tokens=64)
    except LlmUnavailable as e:
        raise ClassificationUnavailable(str(e)) from e
    return parse_response(text)


def apply_classification(lot: Lot, classifier=classify_category) -> Lot:
    """Attach the LLM's read to the lot, or leave the columns NULL when we
    couldn't get one. Never raises: a lot we can't categorise is still a lot we
    want stored."""
    try:
        label, conf = classifier(lot.title, lot.description)
    except LlmUnavailable:
        lot.llm_category = None
        lot.llm_category_confidence = None
        lot.category_agreement = None
        return lot
    lot.llm_category = label
    lot.llm_category_confidence = conf
    lot.category_agreement = (label == lot.canonical_category)
    return lot
