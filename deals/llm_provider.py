"""One LLM transport for every deals workload: provider choice, pacing, breaker.

Extracted from `classify.py` when the `analyze` pass had to move off its
hardcoded `gemini-2.5-flash` (whose prepay balance is spent). Both workloads now
share one dispatch, so "which LLM is free this month" is answered in one place —
that question changes far more often than what we want to ask the model.

**Pacing is the reason this module exists rather than a bare `requests.post`.**
Groq's free tier caps at 14,400 requests/day *and* 6,000 tokens/minute, and the
two limits bind at completely different sizes of work:

    classify   ~290-token prompt   →  ~20 requests/min before tokens run out
    analyze  ~1,500-token prompt   →  ~4 requests/min for the same ceiling

A fixed requests-per-minute limiter tuned for one would either crawl on the
other or 429 constantly. So `_Paced` budgets on a rolling 60-second window of
*estimated tokens* as well as request count, which lets small calls run fast and
throttles big ones exactly as much as they deserve. Overlapping cron services
(discover, analyze, backfill) can't see each other's usage, so a 429 is still
possible — hence the Retry-After honouring retry underneath.

Env: `DEALS_LLM_PROVIDER`, `DEALS_LLM_MODEL`, `DEALS_LLM_TPM`, `DEALS_LLM_RPM`.
"""
import os
import sys
import time
from collections import deque

# OpenAI-compatible providers: (env key, base URL, default model).
#
# Model choice here is set by requests-per-DAY, not quality: the sweep needs
# ~3,100 classify calls plus ~1,200-2,300 analyze calls a day. Measured live via
# `scripts/check_llm_provider.py --headroom`:
#
#   llama-3.3-70b-versatile   1,000 req/day   ← can't cover either workload
#   llama-3.1-8b-instant     14,400 req/day   ← covers both with ~3x headroom
#
# Cerebras is kept wired but is NOT free on a new org: a fresh account sits at
# $0.00 and returns HTTP 402 payment_required. Set CEREBRAS_API_KEY only
# alongside real credits.
_OPENAI_COMPATIBLE = {
    "cerebras": ("CEREBRAS_API_KEY", "https://api.cerebras.ai/v1", "gpt-oss-120b"),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.1-8b-instant"),
}
# Flash-Lite, not Flash: Flash is a thinking model and bills hidden reasoning
# tokens, which is a large part of how a prepay balance vanished on workloads
# whose real answer is a few dozen tokens of JSON.
_GEMINI_MODEL = "gemini-2.5-flash-lite"

_BREAKER_THRESHOLD = 5

# Default ceilings sit just under Groq's free tier (6,000 tok/min, 14,400/day)
# so that a burst never spends the whole minute's budget on one call.
_DEFAULT_TPM = 5500
_DEFAULT_RPM = 20


class LlmUnavailable(Exception):
    """The model could not be reached, or its answer was unusable.

    Distinct from a real answer the caller dislikes. This is the *absence* of a
    result, and callers must be able to tell the two apart — conflating them is
    what let 39,758 lots be stored as `other`/0.0 without a single request ever
    being made.
    """


class _Breaker:
    """Run-scoped consecutive-failure counter. Deliberately not thread-safe:
    each cron is one sequential pass, and a lock would imply a concurrency story
    this module doesn't have."""

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
            print(f"[llm] {_BREAKER_THRESHOLD} consecutive failures — disabling LLM "
                  f"calls for this run. First error: {self.first_error}", file=sys.stderr)

    def record_success(self) -> None:
        self.consecutive = 0


class _Paced:
    """Rolling 60-second budget over both estimated tokens and request count."""

    def __init__(self):
        self.events: deque[tuple[float, int]] = deque()

    def acquire(self, tokens: int, tpm: int, rpm: int) -> None:
        while True:
            now = time.monotonic()
            while self.events and now - self.events[0][0] >= 60:
                self.events.popleft()
            if (sum(t for _, t in self.events) + tokens <= tpm
                    and len(self.events) + 1 <= rpm):
                self.events.append((now, tokens))
                return
            if not self.events:      # single call bigger than the whole budget
                self.events.append((now, tokens))
                return
            time.sleep(max(60 - (now - self.events[0][0]) + 0.05, 0.05))


_breaker = _Breaker()
_paced = _Paced()


def reset_breaker() -> None:
    """Test seam, and an escape hatch for any long-lived process."""
    global _breaker, _paced
    _breaker = _Breaker()
    _paced = _Paced()


def breaker_state() -> dict:
    return {"tripped": _breaker.tripped, "consecutive_failures": _breaker.consecutive,
            "first_error": _breaker.first_error}


def estimate_tokens(prompt: str, max_tokens: int) -> int:
    """~4 chars per token, plus whatever we let the model write back. Rough on
    purpose — it only has to be right enough to pace, and over-estimating costs
    a little throughput while under-estimating costs a 429."""
    return len(prompt) // 4 + max_tokens


def active_provider(env=None) -> tuple[str, str] | None:
    """Resolve (provider, api_key) from the environment, or None if no key exists.

    An explicit `DEALS_LLM_PROVIDER` whose key is missing returns None rather
    than quietly falling through — if you named a provider, you want to hear it
    isn't configured, not get billed somewhere else.
    """
    env = os.environ if env is None else env
    named = (env.get("DEALS_LLM_PROVIDER") or "").strip().lower()
    # Groq first: the only one of the three free at our daily volume today.
    order = [named] if named else ["groq", "cerebras", "gemini"]
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


def _chat_openai_compatible(provider: str, api_key: str, prompt: str,
                            max_tokens: int) -> str:
    import requests
    _, base_url, default_model = _OPENAI_COMPATIBLE[provider]
    model = os.getenv("DEALS_LLM_MODEL") or default_model
    for attempt in range(3):
        r = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0, "max_tokens": max_tokens},
            timeout=60)
        if r.status_code == 429 and attempt < 2:
            # Sibling crons share the quota and can't see each other's pacing.
            # The provider tells us exactly how long to wait; believe it.
            wait = float(r.headers.get("retry-after") or 2 ** attempt)
            print(f"[llm] 429, sleeping {wait:.1f}s", file=sys.stderr)
            time.sleep(min(wait, 30))
            continue
        if r.status_code != 200:
            # Body kept verbatim: "429" alone doesn't distinguish "slow down"
            # from "your balance is gone", and that decides whether we wait or
            # switch providers.
            raise LlmUnavailable(f"{provider} HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]
    raise LlmUnavailable(f"{provider}: rate limited after 3 attempts")


def _chat_gemini(api_key: str, prompt: str) -> str:
    from google import genai
    model = os.getenv("DEALS_LLM_MODEL") or _GEMINI_MODEL
    resp = genai.Client(api_key=api_key).models.generate_content(model=model, contents=prompt)
    return resp.text or ""


def chat(prompt: str, *, max_tokens: int = 64) -> str:
    """Send one prompt, paced and breaker-guarded. Raises `LlmUnavailable`."""
    if _breaker.tripped:
        raise LlmUnavailable("circuit breaker open for this run")
    resolved = active_provider()
    if resolved is None:
        raise LlmUnavailable(
            "no LLM key configured (set GROQ_API_KEY, CEREBRAS_API_KEY or GEMINI_API_KEY)")
    provider, api_key = resolved
    _paced.acquire(estimate_tokens(prompt, max_tokens),
                   int(os.getenv("DEALS_LLM_TPM", _DEFAULT_TPM)),
                   int(os.getenv("DEALS_LLM_RPM", _DEFAULT_RPM)))
    try:
        text = (_chat_gemini(api_key, prompt) if provider == "gemini"
                else _chat_openai_compatible(provider, api_key, prompt, max_tokens))
    except LlmUnavailable as e:
        _breaker.record_failure(str(e))
        raise
    except Exception as e:
        _breaker.record_failure(f"{type(e).__name__}: {e}")
        raise LlmUnavailable(f"{provider}: {type(e).__name__}: {e}") from e
    _breaker.record_success()
    return text
