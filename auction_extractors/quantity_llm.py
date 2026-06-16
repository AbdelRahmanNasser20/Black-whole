"""
Optional LLM pass to infer chair *quantity* from title + (optional) long description.

Designed for GovDeals where counts often appear only in the asset description.

Env (same providers as ranking): LLM_PROVIDER, OLLAMA_*, GROQ_API_KEY, OPENAI_API_KEY, OLLAMA_TIMEOUT.

Sustainability:
  - One batched JSON request per chunk of listings (default 12), not one HTTP call per row.
  - Groq/OpenAI: small cost per daily run; Ollama: no API cost, local GPU/CPU time.
  - Descriptions add N page loads if FETCH_GOVDEALS_DESCRIPTION=1 — main latency/courtesy concern.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

# Chunk size keeps prompts small for free-tier limits and fast failures.
_CHUNK = int(os.getenv("LLM_QUANTITY_CHUNK", "12"))

# Must match tests/test_quantity_accuracy.py / any tooling that shows "what the LLM sees"
LLM_QUANTITY_TITLE_MAX = 500
LLM_QUANTITY_DESC_MAX = 2500


def _ollama_chat(base_url: str, model: str, prompt: str, timeout: int) -> str:
    url = f"{base_url.rstrip('/')}/api/chat"
    r = requests.post(
        url,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content") or ""


def _openai_chat(api_key: str, prompt: str, model: str = "gpt-4o-mini") -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=4000,
    )
    return resp.choices[0].message.content or ""


def _groq_chat(api_key: str, prompt: str, model: str = "llama-3.3-70b-versatile") -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=4000,
    )
    return resp.choices[0].message.content or ""


def _claude_max_chat(prompt: str) -> str:
    """Run a prompt through the user's Claude Max subscription via the official
    ``claude-agent-sdk``. Strips all tools (we want a pure text completion),
    forces JSON-only output via the system prompt, and concatenates every
    assistant TextBlock into a single string the existing parser can read.
    """
    import asyncio
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    model = os.getenv("CLAUDE_MAX_MODEL", "claude-sonnet-4-5")

    async def _run() -> str:
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=(
                "You output ONLY raw JSON. No prose, no markdown, no code fences."
            ),
            allowed_tools=[],
            permission_mode="default",
            max_turns=1,
        )
        chunks: list[str] = []
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
        return "".join(chunks)

    return asyncio.run(_run())


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    try:
        data = json.loads(t)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    start, end = t.find("["), t.rfind("]")
    if start != -1 and end != -1 and end > start:
        return json.loads(t[start : end + 1])
    raise ValueError("No JSON array in LLM response")


def refine_quantities_with_llm(
    listings: list[dict[str, Any]],
    *,
    provider: str,
    ollama_base_url: str,
    ollama_model: str,
    ollama_timeout: int,
    groq_api_key: str | None,
    openai_api_key: str | None,
) -> list[dict[str, Any]]:
    """
    Set quantity, quantity_confidence, quantity_source on each listing.
    Expects keys: title, optional description.
    """
    if not listings:
        return listings

    out = [dict(x) for x in listings]
    prov = (provider or "ollama").strip().lower()

    for start in range(0, len(out), _CHUNK):
        chunk = out[start : start + _CHUNK]
        rows = []
        for j, item in enumerate(chunk):
            title = (item.get("title") or "")[:LLM_QUANTITY_TITLE_MAX]
            desc = (item.get("description") or "")[:LLM_QUANTITY_DESC_MAX]
            rows.append({"i": start + j, "title": title, "description": desc})

        payload = json.dumps(rows, ensure_ascii=False)
        prompt = f"""You estimate how many CHAIRS (individual chair units) are in each auction lot.

Input (JSON array; each row has i, title, description). Description may be empty — use title only if so.

Return ONLY a JSON array of objects, ONE per input row, IN THE SAME ORDER as the input:
[{{"i": <same as input>, "quantity": <positive integer>, "confidence": "high"|"medium"|"low"|"unknown"}}]

Rules:
- Prefer explicit phrases: "lot of (10)", "approx 50 chairs", "Qty 200", etc.
- If the lot is clearly not a chair count (e.g. only model numbers), pick best-effort or 1 with confidence low/unknown.
- Numbers describing ROTATION, ANGLE, DIMENSIONS, WEIGHT, or PERCENTAGES are NOT chair counts. Examples:
  • "rotates 360 degrees" / "swivels 360°" → NOT 360 chairs
  • "45 inches tall" / "18\\" to 22\\" height" → NOT 45 / 22 chairs
  • "Weighs 25 lbs" / "holds 300 lbs" → NOT 25 / 300 chairs
  • "50% polyester" → NOT 50 chairs
  If the only numbers nearby are dimensions/angles/weights, return 1 with confidence low.
- A single retail product (e.g. "Herman Miller Caper Chair", "UMF Medical 8678 Phlebotomy Chair") is a lot of 1, even if its description mentions dimensions or rotation.
- Count ONLY the chairs included in THIS listing. Sellers often split one batch across
  several auctions and repeat the grand total in every description. Examples:
  • "105 chairs total across our listings, sold in groups of 1, 4, and 12" → NOT 105;
    use this listing's own count (title or per-lot statement). If unclear, return 1 with confidence low.
  • "Lot of (6) chairs. (2) other sets of 8 chairs are also being posted." → 6, NOT 22.
    Never add chairs from other listings mentioned in the description.
  • "There are an additional 2,100 chairs available in a separate auction" → ignore the 2,100.
- Never return 0; minimum 1.

INPUT:
{payload}
"""

        # ── chunk-level failure: tag every row in this chunk so the failure is
        # visible downstream (alerts, reports) instead of silently leaving the
        # regex fallback in place. The previous behavior masked exactly the
        # bug we hit when llama3.2 returned Python code instead of JSON.
        def _mark_chunk_failed(reason: str, raw_excerpt: str = "") -> None:
            short = reason[:200]
            for it in chunk:
                it["quantity_source"] = "llm_failed"
                it["quantity_confidence"] = "unknown"
                it["quantity_error"] = short
                if raw_excerpt:
                    it["quantity_llm_raw"] = raw_excerpt[:500]

        try:
            if prov == "openai":
                if not openai_api_key:
                    raise ValueError("OPENAI_API_KEY not set")
                raw = _openai_chat(openai_api_key, prompt)
            elif prov == "groq":
                if not groq_api_key:
                    raise ValueError("GROQ_API_KEY not set")
                raw = _groq_chat(groq_api_key, prompt)
            elif prov == "ollama":
                raw = _ollama_chat(ollama_base_url, ollama_model, prompt, ollama_timeout)
            elif prov == "claude_max":
                raw = _claude_max_chat(prompt)
            else:
                raise ValueError(f"Unknown LLM_PROVIDER: {provider}")
        except Exception as e:
            print(f" ! quantity_llm chunk call failed ({start}-{start + len(chunk)}): {e}")
            _mark_chunk_failed(f"call: {e}")
            continue

        try:
            parsed = _parse_json_array(raw)
        except Exception as e:
            print(
                f" ! quantity_llm JSON parse failed ({start}-{start + len(chunk)}): {e}\n"
                f"   raw[:300]={raw[:300]!r}"
            )
            _mark_chunk_failed(f"parse: {e}", raw_excerpt=raw)
            continue

        # Map by index i
        by_i: dict[int, dict] = {}
        for obj in parsed:
            if not isinstance(obj, dict):
                continue
            idx = obj.get("i")
            if idx is None:
                continue
            try:
                by_i[int(idx)] = obj
            except (TypeError, ValueError):
                continue

        for j, item in enumerate(chunk):
            global_idx = start + j
            row = by_i.get(global_idx)
            if not row:
                # Parsed JSON but this row is missing — mark it so it is
                # visible rather than blending in with successful predictions.
                item["quantity_source"] = "llm_missing"
                item["quantity_confidence"] = "unknown"
                item["quantity_error"] = "row absent from LLM response"
                continue
            try:
                q = int(row.get("quantity", 1))
                if q < 1:
                    q = 1
            except (TypeError, ValueError):
                q = 1
            conf = str(row.get("confidence") or "unknown").lower()
            if conf not in ("high", "medium", "low", "unknown"):
                conf = "unknown"
            item["quantity"] = q
            item["quantity_confidence"] = conf
            item["quantity_source"] = "llm"

    return out
