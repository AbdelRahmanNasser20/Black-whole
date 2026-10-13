"""automation/price_anchors.py — parsing, validation, grounding and the Groq request loop (no network, no DB)."""
import json

import pytest

from automation import price_anchors as pa

GOOD = {
    "listing_url": "https://www.ebay.com/itm/115604981847",
    "title": "Lot of 10 - Shelby Williams Chairs - Beige",
    "seller": "samsdeals01", "brand": "Shelby Williams",
    "condition": "used", "tier": "hotel", "source": "ebay",
    "qty": 450, "price_per_chair": 55, "location": "Newark, NJ",
    "listed_on": None, "image_url": "https://i.ebayimg.com/images/g/q9oAAOSwkuJjc-Ih/s-l1600.jpg",
    "note": None,
}


def test_normalize_keeps_a_good_row():
    row, why = pa.normalize(GOOD)
    assert why is None
    assert row["price_per_chair"] == 55.0 and row["qty"] == 450
    assert row["image_source_url"].startswith("https://i.ebayimg.com/")
    assert set(pa._COLS) <= set(row)


@pytest.mark.parametrize("patch, reason", [
    ({"price_per_chair": 25}, "below"),
    ({"price_per_chair": None}, "no price"),
    ({"price_per_chair": 9000}, "not per-chair"),
    ({"listing_url": "not a url"}, "bad listing_url"),
    ({"listing_url": "https://black-whole.com/listings/31225"}, "our own"),
    ({"seller": "abdel.-3241"}, "our own"),
    ({"condition": "broken"}, "bad condition"),
    ({"title": ""}, "no title"),
])
def test_normalize_rejects(patch, reason):
    row, why = pa.normalize({**GOOD, **patch})
    assert row is None and reason in why


def test_normalize_never_invents_values():
    row, _ = pa.normalize({**GOOD, "qty": "lots", "image_url": "n/a", "seller": None})
    assert row["qty"] is None and row["image_source_url"] is None and row["seller"] is None


def test_normalize_trims_to_column_limits():
    row, _ = pa.normalize({**GOOD, "title": "x" * 500, "note": "y" * 900})
    assert len(row["title"]) == 200 and len(row["note"]) == 300


def test_new_condition_forces_new_tier():
    row, _ = pa.normalize({**GOOD, "condition": "new", "tier": "hotel"})
    assert row["tier"] == "new"
    row, _ = pa.normalize({**GOOD, "condition": "used", "tier": "new"})
    assert row["tier"] == "event"


def test_extract_json_handles_fences_and_prose():
    obj = {"listings": [GOOD]}
    assert pa.extract_json(f"Here you go:\n```json\n{json.dumps(obj)}\n```") == obj
    assert pa.extract_json(f"Found these {json.dumps(obj)} done") == obj
    with pytest.raises(ValueError):
        pa.extract_json("nothing here")


def _tool(urls_text, kind="browser_search"):
    return {"type": kind, "arguments": "{}", "output": "",
            "search_results": {"results": [{"title": t, "url": u, "content": c} for u, t, c in urls_text]}}


def _groq(content, tools=(), tokens=9000, finish="stop"):
    return {"choices": [{"finish_reason": finish,
                         "message": {"role": "assistant", "content": content, "executed_tools": list(tools)}}],
            "usage": {"total_tokens": tokens}}


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.replies.pop(0)


EVIDENCE = [_tool([(GOOD["listing_url"], "Lot of 10 - Shelby Williams Chairs - $55 each", "")])]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")


def test_search_sends_browser_search_and_dedupes():
    body = json.dumps({"listings": [GOOD, GOOD, {**GOOD, "listing_url": "https://x.test/a", "price_per_chair": 10}]})
    http = FakeHttp([FakeResp(200, _groq(body, EVIDENCE))])
    res = pa.search(known_urls=["https://known.test/1"], http=http, max_requests=1)
    url, kw = http.calls[0]
    assert url == pa.GROQ_URL and "anthropic" not in url
    assert kw["json"]["model"] == pa.MODEL == "openai/gpt-oss-20b"
    assert kw["json"]["tools"] == [{"type": "browser_search"}]
    assert kw["headers"]["Authorization"] == "Bearer test-key"
    assert "https://known.test/1" in kw["json"]["messages"][0]["content"]
    assert len(res.listings) == 1 and len(res.rejected) == 1
    assert res.requests == 1 and res.searches == 1 and res.tokens == 9000


def test_search_one_request_per_topic_capped():
    http = FakeHttp([FakeResp(200, _groq('{"listings": []}')) for _ in range(3)])
    res = pa.search(http=http, max_requests=3)
    assert len(http.calls) == 3 == res.requests
    prompts = [kw["json"]["messages"][0]["content"] for _, kw in http.calls]
    assert all(t in p for t, p in zip(pa.TOPICS, prompts))
    http = FakeHttp([FakeResp(200, _groq('{"listings": []}')) for _ in range(len(pa.TOPICS))])
    assert pa.search(http=http, max_requests=10_000).requests == len(pa.TOPICS) <= pa.REQUEST_CEILING


def test_search_rejects_invented_url_and_price():
    invented = {**GOOD, "listing_url": "https://www.ebay.com/itm/185739112345"}
    wrong_price = {**GOOD, "price_per_chair": 45}
    for item, reason in ((invented, "url not in search results"), (wrong_price, "not in source text")):
        http = FakeHttp([FakeResp(200, _groq(json.dumps({"listings": [item]}), EVIDENCE))])
        res = pa.search(http=http, max_requests=1)
        assert res.listings == [] and reason in res.rejected[0][1]


def test_grounding_accepts_lot_total_and_url_variants():
    row, _ = pa.normalize({**GOOD, "listing_url": "https://ebay.com/itm/115604981847/", "price_per_chair": 40, "qty": 100})
    ev = pa.evidence_from([_tool([("https://www.ebay.com/itm/115604981847", "100 chairs $4,000 for the lot", "")])])
    assert pa.grounded(row, ev) is None


def test_search_retries_429_then_succeeds():
    waits = []
    http = FakeHttp([FakeResp(429, {"error": "rate"}, {"retry-after": "7"}),
                     FakeResp(200, _groq(json.dumps({"listings": [GOOD]}), EVIDENCE))])
    res = pa.search(http=http, sleep=waits.append, max_requests=1)
    assert waits == [7.0] and len(res.listings) == 1


def test_search_budget_spent_is_loud_when_nothing_worked():
    http = FakeHttp([FakeResp(429, {}, {"retry-after": "3600"})])
    with pytest.raises(pa.GroqUnavailable):
        pa.search(http=http, sleep=lambda s: None, max_requests=2)


def test_search_keeps_going_past_a_bad_topic():
    http = FakeHttp([FakeResp(200, _groq("| a table, not json |")),
                     FakeResp(200, _groq(json.dumps({"listings": [GOOD]}), EVIDENCE))])
    res = pa.search(http=http, max_requests=2)
    assert len(res.listings) == 1 and len(res.failed_topics) == 1


def test_search_without_key_is_loud(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    with pytest.raises(pa.GroqUnavailable):
        pa.search(http=FakeHttp([]), max_requests=1)


def test_no_anthropic_dependency():
    import pathlib
    src = (pathlib.Path(__file__).parents[1] / "automation/price_anchors.py").read_text()
    assert "import anthropic" not in src


def test_r2_key_is_stable_and_private_prefixed():
    a = pa.r2_key_for("https://i.ebayimg.com/x.jpg", "image/jpeg")
    assert a == pa.r2_key_for("https://i.ebayimg.com/x.jpg", "image/jpeg")
    assert a.startswith("price-anchors/") and a.endswith(".jpg")
    assert pa.r2_key_for("https://a/b", "image/webp").endswith(".webp")


def test_seed_file_is_valid():
    import pathlib
    items = json.loads((pathlib.Path(__file__).parents[1] / "scripts/data/price_anchors_seed_2026-10-10.json").read_text())
    rows = [pa.normalize(i, min_price=0.01)[0] for i in items]
    assert all(rows) and len(rows) == 19
