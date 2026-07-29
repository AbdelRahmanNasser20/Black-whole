"""Regression tests for the failure that ran silently for weeks.

37,934 lots were stored as `other`/0.0 because a bare `except` turned every
"the API key is dead" into "I looked at it and it's other". These tests pin the
distinction: an unreachable model leaves NULLs, a reachable one writes answers.
"""
import pytest

from datetime import datetime, timezone

from deals import classify
from deals.classify import (ClassificationUnavailable, active_provider,
                            apply_classification, build_prompt, classify_category,
                            parse_response)
from deals.models import Lot


@pytest.fixture(autouse=True)
def _fresh_breaker():
    classify.reset_breaker()
    yield
    classify.reset_breaker()


def _lot(canon="general_merchandise"):
    return Lot(1, 2, 3, "9 stackable banquet chairs", "chairs", "266", "General Merchandise",
               canon, datetime(2026, 7, 3, tzinfo=timezone.utc), 0, 10.0, 10.0, "USD", 0,
               False, False, None, False, "s", "c", "st", "z", None, None, "", "STA", False, {})


class TestBuildPrompt:
    """The actual root cause: the prompt embeds a JSON example, and the old
    `str.format` call read `{"label": ...}` as a replacement field. It raised
    KeyError before any request was made, on every single lot, for weeks."""

    def test_renders_without_raising_on_the_embedded_json_example(self):
        prompt = build_prompt("9 stackable banquet chairs", "saffron vinyl")
        assert '{"label"' in prompt          # the example survives verbatim
        assert "9 stackable banquet chairs" in prompt
        assert "saffron vinyl" in prompt

    def test_no_unsubstituted_placeholders_leak_to_the_model(self):
        prompt = build_prompt("t", "d")
        assert "{title}" not in prompt and "{desc}" not in prompt

    def test_a_title_containing_braces_is_not_interpreted(self):
        # Lot titles are scraped text; one containing {qty} must not blow up or
        # get substituted.
        assert "{qty}" in build_prompt("Lot of {qty} chairs", "d")

    def test_none_description_is_tolerated(self):
        assert "Description:" in build_prompt("t", None)

    def test_long_fields_are_truncated(self):
        prompt = build_prompt("t" * 500, "d" * 2000)
        assert "t" * 200 in prompt and "t" * 201 not in prompt
        assert "d" * 800 in prompt and "d" * 801 not in prompt


class TestFailureIsNotAnAnswer:
    def test_unreachable_model_leaves_columns_null(self):
        def dead(t, d):
            raise ClassificationUnavailable("429 prepayment credits are depleted")

        lot = apply_classification(_lot(), classifier=dead)
        # The bug: this used to be ('other', 0.0), indistinguishable from a real read.
        assert lot.llm_category is None
        assert lot.llm_category_confidence is None
        assert lot.category_agreement is None

    def test_lot_is_still_returned_so_ingestion_continues(self):
        def dead(t, d):
            raise ClassificationUnavailable("no key")

        assert apply_classification(_lot(), classifier=dead) is not None

    def test_a_real_other_verdict_is_kept_with_its_confidence(self):
        lot = apply_classification(_lot(), classifier=lambda t, d: ("other", 0.72))
        assert (lot.llm_category, lot.llm_category_confidence) == ("other", 0.72)


class TestCircuitBreaker:
    def test_opens_after_five_consecutive_failures(self, monkeypatch):
        monkeypatch.setenv("CEREBRAS_API_KEY", "k")
        calls = []

        def boom(provider, key, prompt):
            calls.append(provider)
            raise ClassificationUnavailable("HTTP 429")

        monkeypatch.setattr(classify, "_chat_openai_compatible", boom)
        for _ in range(8):
            with pytest.raises(ClassificationUnavailable):
                classify_category("t", "d")
        # Stops calling out after the threshold instead of burning ~3,000
        # doomed round-trips per sweep.
        assert len(calls) == 5
        assert classify.breaker_state()["tripped"] is True

    def test_a_success_resets_the_streak(self, monkeypatch):
        monkeypatch.setenv("CEREBRAS_API_KEY", "k")
        replies = iter(["boom", "boom", '{"label":"seating_furniture","confidence":0.9}'])

        def flaky(provider, key, prompt):
            nxt = next(replies)
            if nxt == "boom":
                raise ClassificationUnavailable("HTTP 500")
            return nxt

        monkeypatch.setattr(classify, "_chat_openai_compatible", flaky)
        for _ in range(2):
            with pytest.raises(ClassificationUnavailable):
                classify_category("t", "d")
        assert classify_category("t", "d") == ("seating_furniture", 0.9)
        assert classify.breaker_state()["consecutive_failures"] == 0


class TestProviderSelection:
    def test_prefers_groq_then_cerebras_then_gemini(self):
        # Groq leads because it is the only one free at our volume: Gemini's
        # prepay is spent, and a new Cerebras org 402s on every call.
        env = {"CEREBRAS_API_KEY": "c", "GROQ_API_KEY": "g", "GEMINI_API_KEY": "m"}
        assert active_provider(env) == ("groq", "g")
        assert active_provider({"CEREBRAS_API_KEY": "c", "GEMINI_API_KEY": "m"}) == ("cerebras", "c")
        assert active_provider({"GEMINI_API_KEY": "m"}) == ("gemini", "m")

    def test_explicit_choice_wins(self):
        env = {"DEALS_LLM_PROVIDER": "cerebras", "CEREBRAS_API_KEY": "c", "GROQ_API_KEY": "g"}
        assert active_provider(env) == ("cerebras", "c")

    def test_explicit_choice_without_its_key_does_not_fall_through(self):
        # Naming a provider you haven't configured should surface as "not
        # configured", not silently bill a different vendor.
        env = {"DEALS_LLM_PROVIDER": "cerebras", "GEMINI_API_KEY": "m"}
        assert active_provider(env) is None

    def test_no_keys_at_all(self):
        assert active_provider({}) is None

    def test_google_api_key_is_accepted_for_gemini(self):
        assert active_provider({"GOOGLE_API_KEY": "m"}) == ("gemini", "m")

    def test_missing_key_raises_rather_than_defaulting(self, monkeypatch):
        for k in ("CEREBRAS_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                  "DEALS_LLM_PROVIDER"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(ClassificationUnavailable):
            classify_category("t", "d")


class TestParseResponse:
    def test_plain_json(self):
        assert parse_response('{"label":"vehicles","confidence":0.8}') == ("vehicles", 0.8)

    @pytest.mark.parametrize("fenced", [
        '```json\n{"label":"vehicles","confidence":0.8}\n```',
        '```\n{"label":"vehicles","confidence":0.8}\n```',
    ])
    def test_markdown_fences_are_stripped(self, fenced):
        assert parse_response(fenced) == ("vehicles", 0.8)

    def test_unknown_label_maps_to_other_but_keeps_confidence(self):
        # A real answer we can't map — not the same as no answer.
        assert parse_response('{"label":"tractors","confidence":0.6}') == ("other", 0.6)

    def test_non_numeric_confidence_degrades_to_zero(self):
        assert parse_response('{"label":"vehicles","confidence":"high"}') == ("vehicles", 0.0)

    @pytest.mark.parametrize("bad", ["", "I think it's a chair.", "[1,2,3]", None])
    def test_unusable_replies_raise(self, bad):
        with pytest.raises(ClassificationUnavailable):
            parse_response(bad)
