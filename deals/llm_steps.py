"""LLM steps of the analyze pipeline: identity extraction + comp judging.

Retrieval-then-reasoning: the LLM identifies and filters; prices come only
from retrieved sold comps (deals/valuation.py). est_resale_per_unit is the
degraded-mode fallback and is always confidence='low' downstream.

Provider comes from `deals/llm_provider.py` — the same dispatch classification
uses. This was hardcoded to `gemini-2.5-flash` until 2026-07-29, which meant the
whole analyze pass died the moment that key's prepay balance ran out, with no
way to point it anywhere else. `2.5-flash` was also a poor fit on its own terms:
it's a *thinking* model, billing hidden reasoning tokens for an answer that is a
few dozen tokens of JSON.

These two prompts are ~5x larger than the classify prompt, which is why the
provider paces on estimated tokens rather than a flat requests-per-minute — see
the module docstring there.
"""
import json
from dataclasses import dataclass, field
from deals.comps import Comp
from deals.llm_provider import LlmUnavailable, chat
from deals.models import Lot

class LlmStepError(Exception):
    pass

@dataclass
class LotIdentity:
    brand: str | None = None
    model: str | None = None
    item_type: str = "unknown"
    quantity: int = 1
    condition: str | None = None
    queries: list[str] = field(default_factory=list)
    est_resale_per_unit: float | None = None

_IDENTITY_PROMPT = """You are analyzing a government-surplus auction lot for resale.
Extract the product identity and give 2-3 eBay search queries (most specific first)
that would find SOLD listings of the same item. Also give your rough estimate of
the USED resale price per unit in USD (number only).
Respond ONLY as compact JSON:
{{"brand": <str|null>, "model": <str|null>, "item_type": <str>,
  "quantity": <int>, "condition": <str|null>,
  "queries": [<str>, ...], "est_resale_per_unit": <number|null>}}
Title: {title}
Description: {desc}"""

_JUDGE_PROMPT = """A surplus lot was identified as: {identity}
Below are eBay SOLD listings found for it, as "index: title ($price)".
Keep ONLY listings that are genuinely the same item (same product family and
whole-unit — not parts, accessories, or different models).
Respond ONLY as compact JSON: {{"keep": [<index>, ...]}}
{listings}"""

def _strip(text: str) -> str:
    return text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

def parse_identity_response(text: str) -> LotIdentity:
    try:
        d = json.loads(_strip(text))
    except (json.JSONDecodeError, TypeError) as e:
        raise LlmStepError(f"unparseable identity response: {text[:120]!r}") from e
    if not d.get("queries"):
        raise LlmStepError("identity response has no queries")
    try:
        qty = max(1, int(d.get("quantity") or 1))
    except (TypeError, ValueError):
        qty = 1
    est = d.get("est_resale_per_unit")
    return LotIdentity(brand=d.get("brand"), model=d.get("model"),
                       item_type=d.get("item_type") or "unknown", quantity=qty,
                       condition=d.get("condition"),
                       queries=[q for q in d["queries"] if isinstance(q, str)][:3],
                       est_resale_per_unit=float(est) if est is not None else None)

def parse_judge_response(text: str, comps: list[Comp]) -> list[Comp]:
    try:
        d = json.loads(_strip(text))
        idx = d.get("keep", [])
    except (json.JSONDecodeError, TypeError, AttributeError):
        return []
    return [comps[i] for i in idx
            if isinstance(i, int) and 0 <= i < len(comps)]

def extract_identity(lot: Lot) -> LotIdentity:
    # 300 output tokens: the reply carries up to three search queries plus the
    # identity fields, and a truncated JSON object parses as a hard failure.
    try:
        text = chat(_IDENTITY_PROMPT.format(
            title=lot.title[:200], desc=(lot.description or "")[:1500]), max_tokens=300)
    except LlmUnavailable as e:
        raise LlmStepError(f"identity call failed: {e}") from e
    return parse_identity_response(text)

def judge_comps(identity: LotIdentity, comps: list[Comp]) -> list[Comp]:
    if not comps:
        return []
    listings = "\n".join(f"{i}: {c.title} (${c.price:.0f})" for i, c in enumerate(comps[:40]))
    ident_str = " ".join(filter(None, [identity.brand, identity.model, identity.item_type]))
    try:
        text = chat(_JUDGE_PROMPT.format(identity=ident_str, listings=listings),
                    max_tokens=200)
    except LlmUnavailable:
        # Keeping nothing is the safe read: downstream falls back to the
        # estimate path at confidence='low' rather than valuing off unvetted
        # comps. Deliberately not raising — one unreachable judge call must not
        # cost us the whole lot's analysis.
        return []
    return parse_judge_response(text, comps[:40])
