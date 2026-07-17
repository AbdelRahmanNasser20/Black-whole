"""Gemini steps of the analyze pipeline: identity extraction + comp judging.

Retrieval-then-reasoning: the LLM identifies and filters; prices come only
from retrieved sold comps (deals/valuation.py). est_resale_per_unit is the
degraded-mode fallback and is always confidence='low' downstream."""
import json
from dataclasses import dataclass, field
from deals.comps import Comp
from deals.models import Lot
from automation import config

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

def _gemini(prompt: str) -> str:
    from google import genai
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return resp.text or ""

def extract_identity(lot: Lot) -> LotIdentity:
    try:
        text = _gemini(_IDENTITY_PROMPT.format(
            title=lot.title[:200], desc=(lot.description or "")[:1500]))
    except Exception as e:
        raise LlmStepError(f"gemini identity call failed: {e}") from e
    return parse_identity_response(text)

def judge_comps(identity: LotIdentity, comps: list[Comp]) -> list[Comp]:
    if not comps:
        return []
    listings = "\n".join(f"{i}: {c.title} (${c.price:.0f})" for i, c in enumerate(comps[:40]))
    ident_str = " ".join(filter(None, [identity.brand, identity.model, identity.item_type]))
    try:
        text = _gemini(_JUDGE_PROMPT.format(identity=ident_str, listings=listings))
    except Exception:
        return []
    return parse_judge_response(text, comps[:40])
