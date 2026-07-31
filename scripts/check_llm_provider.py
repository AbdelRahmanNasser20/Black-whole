#!/usr/bin/env python
"""Prove the classifier can actually reach a model. Run after changing keys.

This exists because the failure it checks for was invisible for weeks: the
classifier returned a plausible label (`other`, confidence 0.0) whether or not a
request had been made, so "is the LLM working?" had no observable answer. Here
the answer is observable — a wrong key raises, a right one prints real labels.

    python scripts/check_llm_provider.py

Exits non-zero if no provider is configured or the live call fails.
"""
import sys
from pathlib import Path

# Run from anywhere: sys.path[0] is scripts/, and both packages live one up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config  # noqa: E402,F401  — imports for the .env side effect
from deals.classify import ClassificationUnavailable, active_provider, classify_category

# Four lots with unambiguous answers, so a wrong label means a real problem
# (bad model, bad prompt) rather than a genuinely borderline lot.
SAMPLES = [
    ("9 Stackable Banquet Chairs",
     "Lot of nine padded stacking banquet chairs, saffron vinyl, some wear.",
     "seating_furniture"),
    ("2014 Ford F-150 XLT",
     "Pickup truck, 142k miles, runs and drives, sold as-is.",
     "vehicles"),
    ("Dell OptiPlex 7040 Lot (25 units)",
     "Twenty-five desktop computers, no hard drives, untested.",
     "computers_electronics"),
    ("Assorted Office Supplies",
     "Box of staplers, binders, and pens from a closed department.",
     "general_merchandise"),
]


def report_headroom(provider: str, api_key: str, model: str | None = None) -> None:
    """Print the provider's own rate-limit headers.

    Worth doing empirically: the sweep classifies ~3,100 lots/day, and whether a
    given free tier covers that is a fact the provider will tell you on any
    request. Guessing it from a pricing page is how you find out in production.
    """
    import requests

    from deals.classify import _OPENAI_COMPATIBLE

    if provider not in _OPENAI_COMPATIBLE:
        print(f"(no rate-limit headers for {provider})")
        return
    _, base_url, default_model = _OPENAI_COMPATIBLE[provider]
    r = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model or default_model, "messages": [{"role": "user", "content": "hi"}],
              "max_tokens": 1},
        timeout=30)
    interesting = {k.lower(): v for k, v in r.headers.items() if "ratelimit" in k.lower()}
    print(f"model: {model or default_model}   HTTP {r.status_code}")
    for k in sorted(interesting):
        print(f"  {k:34} {interesting[k]}")


def main() -> int:
    if "--headroom" in sys.argv:
        resolved = active_provider()
        if resolved is None:
            print("no provider configured", file=sys.stderr)
            return 2
        provider, key = resolved
        models = [a for a in sys.argv[1:] if not a.startswith("--")] or [None]
        for m in models:
            report_headroom(provider, key, m)
            print()
        return 0

    resolved = active_provider()
    if resolved is None:
        print("no provider configured — set CEREBRAS_API_KEY, GROQ_API_KEY or "
              "GEMINI_API_KEY in .env", file=sys.stderr)
        return 2
    provider, key = resolved
    print(f"provider: {provider}   key: {key[:8]}…{key[-4:]}\n")

    hits = 0
    for title, desc, expected in SAMPLES:
        try:
            label, confidence = classify_category(title, desc)
        except ClassificationUnavailable as e:
            print(f"  FAILED  {title[:40]:42} {e}", file=sys.stderr)
            return 1
        ok = label == expected
        hits += ok
        print(f"  {'ok ' if ok else 'MISS'}    {title[:40]:42} "
              f"{label} ({confidence:.2f}){'' if ok else f'  expected {expected}'}")

    print(f"\n{hits}/{len(SAMPLES)} as expected — the model is reachable and answering.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
