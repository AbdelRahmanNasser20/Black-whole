import base64
import json
from pathlib import Path

from .base import Extraction, EXTRACTION_PROMPT
from ..config import OPENAI_API_KEY, OPENAI_MODEL


class OpenAIExtractor:
    """GPT-4o-mini-based extractor. Vision-capable, JSON-mode response.

    Used as the secondary (A/B) extractor when Gemini is primary. Promotes to
    primary when `GEMINI_API_KEY` is unset and `OPENAI_API_KEY` is set.
    """

    name = "openai"

    def __init__(self, api_key: str | None = None, model: str = OPENAI_MODEL):
        self.api_key = api_key or OPENAI_API_KEY
        self.model = model

    async def extract(self, dom_hint: dict, screenshots: dict[str, Path]) -> Extraction:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise RuntimeError(
                "openai package not installed. Run: pip install -e '.[extractors]'"
            ) from e

        client = AsyncOpenAI(api_key=self.api_key)

        content: list = [
            {"type": "text", "text": EXTRACTION_PROMPT},
            {"type": "text", "text": f"DOM hint:\n{json.dumps(dom_hint, indent=2)}"},
        ]
        for _, path in screenshots.items():
            b = Path(path).read_bytes()
            b64 = base64.b64encode(b).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })

        resp = await client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": content}],
            temperature=0,
        )
        text = resp.choices[0].message.content or ""
        payload = json.loads(text)
        # Tolerate extra keys the model might emit; the dataclass rejects unknowns.
        allowed = {
            "title", "location", "city", "state", "zip_code", "quantity",
            "chair_type", "chair_title", "description_text", "dimensions",
            "suggested_price_per_chair", "style_suffix",
            "contact_name", "contact_email", "contact_phone",
        }
        clean = {k: v for k, v in payload.items() if k in allowed}
        return Extraction(source=self.name, **clean)
