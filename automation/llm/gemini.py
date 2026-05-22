import json
import re
from pathlib import Path

from .base import Extraction, EXTRACTION_PROMPT
from ..config import GEMINI_API_KEY, GEMINI_MODEL


class GeminiExtractor:
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str = GEMINI_MODEL):
        self.api_key = api_key or GEMINI_API_KEY
        self.model = model

    async def extract(self, dom_hint: dict, screenshots: dict[str, Path]) -> Extraction:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        parts: list = [EXTRACTION_PROMPT, f"\nDOM hint:\n{json.dumps(dom_hint, indent=2)}"]
        for name, path in screenshots.items():
            data = Path(path).read_bytes()
            parts.append(types.Part.from_bytes(data=data, mime_type="image/png"))

        resp = await client.aio.models.generate_content(
            model=self.model,
            contents=parts,
        )
        text = resp.text or ""
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise ValueError(f"Gemini returned non-JSON: {text[:200]}")
        payload = json.loads(m.group(0))
        allowed = {
            "title", "location", "city", "state", "zip_code", "quantity",
            "chair_type", "chair_title", "description_text", "dimensions",
            "suggested_price_per_chair", "style_suffix",
            "contact_name", "contact_email", "contact_phone",
        }
        clean = {k: v for k, v in payload.items() if k in allowed}
        return Extraction(source=self.name, **clean)
