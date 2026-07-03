import json
from deals.models import Lot
from automation import config

CANONICAL_LABELS = ["seating_furniture", "general_merchandise", "vehicles",
                    "collectibles_jewelry", "computers_electronics", "other"]

_PROMPT = (
    "Classify this liquidation lot into exactly one label from this list: "
    + ", ".join(CANONICAL_LABELS) +
    ". Respond ONLY as compact JSON: {\"label\": <one label>, \"confidence\": <0..1>}.\n"
    "Title: {title}\nDescription: {desc}"
)

def classify_category(title: str, description: str) -> tuple[str, float]:
    """Call Gemini to place a lot in our canonical taxonomy. Returns (label, confidence).
    Falls back to ('other', 0.0) on any error so classification never blocks ingestion."""
    from google import genai
    try:
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=_PROMPT.format(title=title[:200], desc=(description or "")[:800]))
        data = json.loads(resp.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        label = data.get("label", "other")
        return (label if label in CANONICAL_LABELS else "other", float(data.get("confidence", 0.0)))
    except Exception:
        return ("other", 0.0)

def apply_classification(lot: Lot, classifier=classify_category) -> Lot:
    label, conf = classifier(lot.title, lot.description)
    lot.llm_category = label
    lot.llm_category_confidence = conf
    lot.category_agreement = (label == lot.canonical_category)
    return lot
