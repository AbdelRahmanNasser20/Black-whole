"""Pacing and shared-transport tests.

Groq's free tier binds on two axes at once — 14,400 requests/day and 6,000
tokens/minute — and the two workloads sharing this transport have prompts that
differ by ~5x in size. A limiter that only counted requests would let the
analyze pass blow the token budget while classify crawled. These pin that the
budget is spent in tokens, and that a lot is never lost to a throttle.
"""
import pytest

from deals import llm_provider, llm_steps
from deals.comps import Comp
from deals.llm_provider import LlmUnavailable, _Paced, estimate_tokens
from deals.llm_steps import LlmStepError


@pytest.fixture(autouse=True)
def _fresh():
    llm_provider.reset_breaker()
    yield
    llm_provider.reset_breaker()


class TestTokenEstimate:
    def test_counts_prompt_and_reply_budget(self):
        # ~4 chars/token, plus the reply we authorised.
        assert estimate_tokens("x" * 400, 64) == 164

    def test_a_big_analyze_prompt_costs_far_more_than_a_classify_one(self):
        classify_like = estimate_tokens("x" * 1160, 64)
        analyze_like = estimate_tokens("x" * 6000, 300)
        assert analyze_like > 5 * classify_like


class TestPacer:
    def test_allows_a_burst_that_fits_the_budget(self):
        p = _Paced()
        for _ in range(10):
            p.acquire(100, tpm=5500, rpm=20)   # 1,000 tokens total — no sleeping
        assert len(p.events) == 10

    def test_waits_on_tokens_even_when_request_count_is_fine(self, monkeypatch):
        # Budget allows 5 x 1000-token calls; the 6th must wait on TOKENS even
        # though the request count is nowhere near its cap. The fake clock is
        # advanced BY sleep, so a limiter that failed to wait long enough would
        # spin here instead of quietly passing.
        clock, slept = [1000.0], []

        def fake_sleep(s):
            slept.append(s)
            clock[0] += s

        monkeypatch.setattr(llm_provider.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(llm_provider.time, "sleep", fake_sleep)

        p = _Paced()
        for _ in range(5):
            p.acquire(1000, tpm=5000, rpm=100)
        assert slept == []          # the first five fit

        p.acquire(1000, tpm=5000, rpm=100)
        assert slept and sum(slept) >= 60    # waited for the window to roll

    def test_waits_on_request_count_even_when_tokens_are_cheap(self, monkeypatch):
        clock, slept = [1000.0], []

        def fake_sleep(s):
            slept.append(s)
            clock[0] += s

        monkeypatch.setattr(llm_provider.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(llm_provider.time, "sleep", fake_sleep)

        p = _Paced()
        for _ in range(3):
            p.acquire(1, tpm=999_999, rpm=3)
        assert slept == []
        p.acquire(1, tpm=999_999, rpm=3)
        assert slept

    def test_a_single_oversized_call_is_not_deadlocked(self):
        # A prompt bigger than the whole per-minute budget must still go out
        # rather than spin forever waiting for room that can never exist.
        p = _Paced()
        p.acquire(99_000, tpm=5500, rpm=20)
        assert len(p.events) == 1

    def test_old_events_fall_out_of_the_window(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(llm_provider.time, "monotonic", lambda: clock[0])
        p = _Paced()
        p.acquire(5000, tpm=5500, rpm=20)
        clock[0] += 61          # a minute later the budget is clean again
        p.acquire(5000, tpm=5500, rpm=20)
        assert len(p.events) == 1


class TestAnalyzeUsesTheSharedProvider:
    """The port: these steps were hardcoded to gemini-2.5-flash, so the whole
    analyze pass died with that key and could not be pointed elsewhere."""

    def test_identity_goes_through_chat(self, monkeypatch):
        seen = {}

        def fake_chat(prompt, *, max_tokens=64):
            seen["prompt"], seen["max_tokens"] = prompt, max_tokens
            return ('{"brand":"Hon","model":null,"item_type":"chair","quantity":9,'
                    '"condition":"used","queries":["hon banquet chair"],'
                    '"est_resale_per_unit":25}')

        monkeypatch.setattr(llm_steps, "chat", fake_chat)
        ident = llm_steps.extract_identity(
            type("L", (), {"title": "9 chairs", "description": "used"})())
        assert ident.quantity == 9 and ident.queries == ["hon banquet chair"]
        # Room for three queries plus the identity fields; a truncated JSON
        # object is a hard parse failure.
        assert seen["max_tokens"] == 300

    def test_unreachable_provider_raises_a_step_error(self, monkeypatch):
        def dead(prompt, *, max_tokens=64):
            raise LlmUnavailable("HTTP 402 payment_required")

        monkeypatch.setattr(llm_steps, "chat", dead)
        with pytest.raises(LlmStepError):
            llm_steps.extract_identity(
                type("L", (), {"title": "t", "description": "d"})())

    def test_a_throttled_judge_keeps_nothing_rather_than_losing_the_lot(self, monkeypatch):
        # Returning [] sends valuation down the estimate path at
        # confidence='low'; raising would cost the whole lot's analysis.
        def dead(prompt, *, max_tokens=64):
            raise LlmUnavailable("rate limited after 3 attempts")

        monkeypatch.setattr(llm_steps, "chat", dead)
        comps = [Comp(listing_id="1", title="chair", price=20.0, condition="used", url="u")]
        assert llm_steps.judge_comps(llm_steps.LotIdentity(item_type="chair"), comps) == []
