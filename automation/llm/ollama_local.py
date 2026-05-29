"""Ollama-backed extractor (local or Ollama Cloud).

Two shapes sharing the same interface:
  - OllamaTextExtractor:  sends DOM hint text only — works with text-only
    models like `gpt-oss:120b-cloud` or any local text LLM.
  - OllamaVisionExtractor: sends DOM hint + screenshots — needs a vision
    model like `llama3.2-vision:11b`.

Both speak to a running Ollama daemon (`ollama serve`). The `-cloud` suffix
on a model name routes through Ollama's hosted inference transparently — no
different call, just pre-pull the tagged model.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from .base import Extraction, EXTRACTION_PROMPT


DEFAULT_TEXT_MODEL = os.getenv("OLLAMA_TEXT_MODEL", "gpt-oss:120b-cloud")
DEFAULT_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "llama3.2-vision:11b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST")  # None → ollama lib default (http://127.0.0.1:11434)


_ALLOWED_EXTRACTION_KEYS = {
    "title", "location", "city", "state", "zip_code", "quantity", "chair_type",
    "chair_title", "description_text", "dimensions",
    "suggested_price_per_chair", "style_suffix",
    "contact_name", "contact_email", "contact_phone",
}


def _extract_json_block(text: str) -> dict:
    # Models love to wrap JSON in ```json ... ``` fences or pre/post chatter.
    # Find the first balanced {...} block and parse it.
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"Ollama returned non-JSON: {text[:300]}")
    payload = json.loads(m.group(0))
    # Drop unknown keys so Extraction(**payload) doesn't crash when the model
    # hallucinates extra fields. Missing fields fall back to dataclass defaults.
    return {k: v for k, v in payload.items() if k in _ALLOWED_EXTRACTION_KEYS}


def _ollama_client():
    import ollama  # imported lazily so the module stays cheap to import
    if OLLAMA_HOST:
        return ollama.AsyncClient(host=OLLAMA_HOST)
    return ollama.AsyncClient()


def _text_prompt(dom_hint: dict) -> str:
    desc = dom_hint.get("description_head") or ""
    title = dom_hint.get("title") or ""
    location = dom_hint.get("location") or ""
    dom_qty = dom_hint.get("quantity") or ""
    # Keep the prompt under a few KB — these models don't need the full HTML.
    trimmed_desc = desc[:3000]
    return (
        f"{EXTRACTION_PROMPT}\n\n"
        "You are working from description text only — no screenshot. Use this data:\n\n"
        f"DOM-scraped title: {title}\n"
        f"DOM-scraped location: {location}\n"
        f"DOM-scraped quantity guess (often wrong, e.g. captures a stray `(10)` from pagination): {dom_qty}\n\n"
        f"Description excerpt (up to 3000 chars):\n---\n{trimmed_desc}\n---\n\n"
        "Return ONLY the JSON object. No prose, no code fences."
    )


class OllamaTextExtractor:
    """Primary extractor when paired with a text-only model like gpt-oss:120b-cloud."""

    name = "ollama_text"

    def __init__(self, model: str = DEFAULT_TEXT_MODEL):
        self.model = model

    async def extract(self, dom_hint: dict, screenshots: dict[str, Path]) -> Extraction:
        client = _ollama_client()
        prompt = _text_prompt(dom_hint)
        resp = await client.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.2},
        )
        content = (resp.get("message") or {}).get("content") or ""
        payload = _extract_json_block(content)
        return Extraction(source=f"{self.name}:{self.model}", **payload)


class OllamaVisionExtractor:
    """Vision extractor — sends screenshots like Gemini did."""

    name = "ollama_vision"

    def __init__(self, model: str = DEFAULT_VISION_MODEL):
        self.model = model

    async def extract(self, dom_hint: dict, screenshots: dict[str, Path]) -> Extraction:
        client = _ollama_client()
        image_paths = [str(p) for p in screenshots.values() if Path(p).exists()]
        # llama3.2-vision:11b only accepts one image per request (HTTP 500 on 2+).
        # Prefer 'header' if present (tighter crop → fewer tokens); otherwise first.
        if image_paths:
            header = screenshots.get("header")
            if header and Path(header).exists():
                image_paths = [str(header)]
            else:
                image_paths = image_paths[:1]
        if not image_paths:
            # No screenshots available — fall back to text-only prompt against
            # the same vision model (these models still accept plain chat).
            prompt = _text_prompt(dom_hint)
            resp = await client.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.2},
            )
        else:
            prompt = (
                f"{EXTRACTION_PROMPT}\n\n"
                f"DOM hint:\n{json.dumps(dom_hint, indent=2)}\n\n"
                "Return ONLY the JSON object. No prose, no code fences."
            )
            resp = await client.chat(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": prompt,
                    "images": image_paths,
                }],
                options={"temperature": 0.2},
            )
        content = (resp.get("message") or {}).get("content") or ""
        payload = _extract_json_block(content)
        return Extraction(source=f"{self.name}:{self.model}", **payload)


# Back-compat alias for a generic "OllamaExtractor" that auto-picks vision vs text
# based on model name heuristics.
def OllamaExtractor(model: str | None = None):
    model = model or DEFAULT_TEXT_MODEL
    looks_vision = any(tag in model.lower() for tag in ("vision", "vl", "llava", "bakllava"))
    return OllamaVisionExtractor(model) if looks_vision else OllamaTextExtractor(model)
