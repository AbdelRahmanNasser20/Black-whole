import asyncio
import json
import os
import sys
import time
from pathlib import Path

from .base import Extraction, Extractor
from .claude_code import ClaudeCodeExtractor
from .gemini import GeminiExtractor
from .openai import OpenAIExtractor
from .dom_fallback import DomFallbackExtractor
from .ollama_local import OllamaTextExtractor, OllamaVisionExtractor
from ..config import LOG_DIR, GEMINI_API_KEY, OPENAI_API_KEY


async def run_parallel(
    primary: Extractor,
    secondary: Extractor | None,
    dom_hint: dict,
    screenshots: dict[str, Path],
) -> tuple[Extraction, Extraction | None]:
    primary_task = asyncio.create_task(primary.extract(dom_hint, screenshots))
    secondary_task = (
        asyncio.create_task(secondary.extract(dom_hint, screenshots))
        if secondary else None
    )

    # Primary must never kill the run. If it fails (quota, network, bad key),
    # fall back to DomFallback — a watermarked draft with a heuristic quantity
    # is still more useful than a crashed pipeline.
    primary_res: Extraction | None = None
    try:
        primary_res = await primary_task
    except Exception as e:
        print(f"[primary LLM {primary.name} failed: {type(e).__name__}: {str(e)[:200]}]")
        print(f"[primary LLM {primary.name} failing open -> DomFallback]")
        try:
            primary_res = await DomFallbackExtractor().extract(dom_hint, screenshots)
        except Exception as e2:
            print(f"[DomFallback also failed: {e2!r}] — this should be impossible; aborting")
            raise

    secondary_res = None
    if secondary_task:
        try:
            secondary_res = await secondary_task
        except Exception as e:
            secondary_res = None
            print(f"[secondary LLM {secondary.name} failed: {e}]")

    log = {
        "timestamp": time.time(),
        "dom_hint": dom_hint,
        "primary": primary_res.to_dict() if primary_res else None,
        "secondary": secondary_res.to_dict() if secondary_res else None,
    }
    log_path = LOG_DIR / f"llm_compare_{int(log['timestamp'])}.json"
    log_path.write_text(json.dumps(log, indent=2))
    return primary_res, secondary_res


def _ollama_reachable() -> bool:
    """Cheap check: can we import the ollama client? The call itself fails
    fast if the daemon isn't running, so no need to probe HTTP here."""
    try:
        import ollama  # noqa: F401
        return True
    except ImportError:
        return False


def _ollama_has_model(model: str) -> bool:
    """Check whether an Ollama model is locally available. Prevents the
    daemon from spending 15+ minutes auto-pulling a 7.8 GB model mid-run."""
    try:
        import ollama
        resp = ollama.list()
        models = resp.get("models") or []
        names = {m.get("model") or m.get("name") or "" for m in models}
        # Accept exact match or prefix match (Ollama's tags are flexible: a
        # bare "llama3.2-vision" should match "llama3.2-vision:11b", etc.)
        if model in names:
            return True
        return any(n.startswith(model.split(":")[0]) for n in names)
    except Exception:
        return False


def default_extractors() -> tuple[Extractor, Extractor | None]:
    """Pick primary/secondary extractors based on environment.

    LISTING_LLM_MODE env var (auto|gemini|openai|claude_code|ollama|dom, default auto):
      - auto (default):
          primary   = Gemini if GEMINI_API_KEY; else OpenAI if OPENAI_API_KEY;
                      else DomFallback.
          secondary = OpenAI if OPENAI_API_KEY is set AND not already primary.
      - gemini | openai | dom | claude_code: force-pick matching primary.
      - ollama: force OllamaText primary + OllamaVision secondary (legacy path).
    """
    mode = os.getenv("LISTING_LLM_MODE", "auto").lower()
    has_gemini = bool(GEMINI_API_KEY)
    has_openai = bool(OPENAI_API_KEY)
    has_ollama = _ollama_reachable()
    interactive = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False

    if mode == "ollama":
        primary: Extractor = OllamaTextExtractor()
        vision_model = os.getenv("OLLAMA_VISION_MODEL", "llama3.2-vision:11b")
        secondary: Extractor | None = (
            OllamaVisionExtractor(vision_model)
            if has_ollama and _ollama_has_model(vision_model)
            else None
        )
        return primary, secondary

    if mode == "claude_code":
        primary = ClaudeCodeExtractor()
    elif mode == "gemini":
        primary = GeminiExtractor() if has_gemini else DomFallbackExtractor()
    elif mode == "openai":
        primary = OpenAIExtractor() if has_openai else DomFallbackExtractor()
    elif mode == "dom":
        primary = DomFallbackExtractor()
    else:  # auto
        if has_gemini:
            primary = GeminiExtractor()
        elif has_openai:
            primary = OpenAIExtractor()
        elif interactive:
            primary = ClaudeCodeExtractor()
        else:
            primary = DomFallbackExtractor()

    secondary = None
    if has_openai and not isinstance(primary, OpenAIExtractor):
        secondary = OpenAIExtractor()
    return primary, secondary


__all__ = [
    "Extraction",
    "Extractor",
    "ClaudeCodeExtractor",
    "GeminiExtractor",
    "OpenAIExtractor",
    "OllamaTextExtractor",
    "OllamaVisionExtractor",
    "run_parallel",
    "default_extractors",
]
