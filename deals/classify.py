"""Canonical-category classification for a lot, via whichever LLM we can afford.

**The failure this file was rewritten to prevent.** The original swallowed every
exception and returned `("other", 0.0)`, which is indistinguishable from a
genuine "I read it and it's other". Gemini's prepay credits ran dry and the
sweep kept calling it ~3,000 times a day for weeks: **37,934 lots classified,
every one of them `other` at confidence 0.0, zero real answers**, and nothing
anywhere said so. A classifier that can't reach its model must leave
`llm_category` NULL — an empty column is a question you can still answer later;
a fake label is a wrong answer nobody will ever go back and check.

Three consequences of that, all deliberate:

1. `classify_category` raises `ClassificationUnavailable` instead of returning a
   default. `apply_classification` catches it and leaves the LLM columns None,
   so the lot is still stored — classification failing must never cost us the row.
2. A run-scoped circuit breaker. Once the provider has failed
   `_BREAKER_THRESHOLD` times in a row it stops calling for the rest of the
   process. The old code spent three thousand doomed HTTPS round-trips per
   sweep rediscovering the same dead key.
3. Provider is env-driven, because "which LLM is free this month" changes far
   more often than what we want to ask it. Cerebras and Groq are both
   OpenAI-compatible, so they share a code path and differ only by base URL.

Set `DEALS_LLM_PROVIDER` to `cerebras` | `groq` | `gemini`; leaving it unset
picks the first provider whose key is present, in that order — most free
capacity first.
"""
import json
import os
import sys

from deals.models import Lot

CANONICAL_LABELS = ["seating_furniture", "general_merchandise", "vehicles",
                    "collectibles_jewelry", "computers_electronics", "other"]

_INSTRUCTIONS = (
    "Classify this liquidation lot into exactly one label from this list: "
    + ", ".join(CANONICAL_LABELS) +
    ". Respond ONLY as compact JSON: {\"label\": <one label>, \"confidence\": <0..1>}."
)


def build_prompt(title: str, description: str) -> str:
    """Assemble the prompt by concatenation, NOT `str.format`.

    The original called `_PROMPT.format(title=…, desc=…)` on a template that
    contains the literal JSON example `{"label": …}`. `str.format` reads that as
    a replacement field named `"label"` and raises `KeyError('"label"')` — on
    the first statement inside the try, before any network call. Wrapped in a
    bare `except`, that produced 37,934 lots labelled `other`/0.0 without a
    single request ever leaving the machine. Escaping the braces would also
    work, and would break again the next time someone edits the example; not
    running `.format` over a string that contains JSON is the fix that stays
    fixed.
    """
    return (f"{_INSTRUCTIONS}\n"
            f"Title: {title[:200]}\n"
            f"Description: {(description or '')[:800]}")

# OpenAI-compatible providers: (env key, base URL, default model). Cerebras'
# free tier is the only one whose daily token budget covers a full sweep;
# Groq's caps out on requests-per-day, so it sits second as spillover.
_OPENAI_COMPATIBLE = {
    "cerebras": ("CEREBRAS_API_KEY", "https://api.cerebras.ai/v1", "gpt-oss-120b"),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
}
# Flash-Lite, not Flash: Flash is a thinking model and bills the hidden
# reasoning tokens, which is a large part of how a prepay balance vanished on a
# workload whose real answer is fifteen tokens of JSON.
_GEMINI_MODEL = "gemini-2.5-flash-lite"

_BREAKER_THRESHOLD = 5


class ClassificationUnavailable(Exception):
    """The model could not be reached, or its answer was unusable.

    Distinct from "the model says this lot is `other`" — that is a real result
    with a real confidence. This is the *absence* of a result.
    """


class _Breaker:
    """Run-scoped consecutive-failure counter. Deliberately not thread-safe:
    discovery is one sequential sweep, and a lock here would imply a concurrency
    story this module doesn't have."""

    def __init__(self):
        self.consecutive = 0
        self.tripped = False
        self.first_error: str | None = None

    def record_failure(self, err: str) -> None:
        if self.first_error is None:
            self.first_error = err
        self.consecutive += 1
        if self.consecutive >= _BREAKER_THRESHOLD and not self.tripped:
            self.tripped = True
            print(f"[classify] {_BREAKER_THRESHOLD} consecutive failures — disabling "
                  f"classification for this run. First error: {self.first_error}",
                  file=sys.stderr)

    def record_success(self) -> None:
        self.consecutive = 0


_breaker = _Breaker()


def reset_breaker() -> None:
    """Test seam, and an escape hatch for any long-lived process."""
    global _breaker
    _breaker = _Breaker()


def breaker_state() -> dict:
    return {"tripped": _breaker.tripped, "consecutive_failures": _breaker.consecutive,
            "first_error": _breaker.first_error}


def active_provider(env=None) -> tuple[str, str] | None:
    """Resolve (provider, api_key) from the environment, or None if no key exists.

    An explicit `DEALS_LLM_PROVIDER` whose key is missing returns None rather
    than quietly falling through to a different provider — if you named one, you
    want to hear that it isn't configured, not get billed somewhere else.
    """
    env = os.environ if env is None else env
    named = (env.get("DEALS_LLM_PROVIDER") or "").strip().lower()
    order = [named] if named else ["cerebras", "groq", "gemini"]
    for provider in order:
        if provider in _OPENAI_COMPATIBLE:
            key = env.get(_OPENAI_COMPATIBLE[provider][0])
        elif provider == "gemini":
            key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
        else:
            continue
        if key:
            return (provider, key)
    return None


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
        raise ClassificationUnavailable(f"unparseable model reply: {(text or '')[:120]!r}") from e
    if not isinstance(data, dict):
        raise ClassificationUnavailable(f"model reply was not an object: {(text or '')[:120]!r}")
    label = data.get("label", "other")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return (label if label in CANONICAL_LABELS else "other", confidence)


def _chat_openai_compatible(provider: str, api_key: str, prompt: str) -> str:
    import requests
    _, base_url, default_model = _OPENAI_COMPATIBLE[provider]
    model = os.getenv("DEALS_LLM_MODEL") or default_model
    r = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}],
              "temperature": 0, "max_tokens": 64},
        timeout=30)
    if r.status_code != 200:
        # Body kept verbatim: "429" alone doesn't distinguish "slow down" from
        # "your balance is gone", and that difference decides whether we wait or
        # switch providers.
        raise ClassificationUnavailable(f"{provider} HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"]


def _chat_gemini(api_key: str, prompt: str) -> str:
    from google import genai
    model = os.getenv("DEALS_LLM_MODEL") or _GEMINI_MODEL
    resp = genai.Client(api_key=api_key).models.generate_content(model=model, contents=prompt)
    return resp.text or ""


def classify_category(title: str, description: str) -> tuple[str, float]:
    """Place a lot in our taxonomy. Raises `ClassificationUnavailable` on any
    provider problem — the caller decides what an absent answer means."""
    if _breaker.tripped:
        raise ClassificationUnavailable("circuit breaker open for this run")
    resolved = active_provider()
    if resolved is None:
        raise ClassificationUnavailable(
            "no LLM key configured (set CEREBRAS_API_KEY, GROQ_API_KEY or GEMINI_API_KEY)")
    provider, api_key = resolved
    prompt = build_prompt(title, description)
    try:
        text = (_chat_gemini(api_key, prompt) if provider == "gemini"
                else _chat_openai_compatible(provider, api_key, prompt))
        result = parse_response(text)
    except ClassificationUnavailable as e:
        _breaker.record_failure(str(e))
        raise
    except Exception as e:
        _breaker.record_failure(f"{type(e).__name__}: {e}")
        raise ClassificationUnavailable(f"{provider}: {type(e).__name__}: {e}") from e
    _breaker.record_success()
    return result


def apply_classification(lot: Lot, classifier=classify_category) -> Lot:
    """Attach the LLM's read to the lot, or leave the columns NULL when we
    couldn't get one. Never raises: a lot we can't categorise is still a lot we
    want stored."""
    try:
        label, conf = classifier(lot.title, lot.description)
    except ClassificationUnavailable:
        lot.llm_category = None
        lot.llm_category_confidence = None
        lot.category_agreement = None
        return lot
    lot.llm_category = label
    lot.llm_category_confidence = conf
    lot.category_agreement = (label == lot.canonical_category)
    return lot
