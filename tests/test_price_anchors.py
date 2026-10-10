"""automation/price_anchors.py — parsing, validation and the Haiku turn loop (no network, no DB)."""
import json
from types import SimpleNamespace as NS

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


def _resp(stop, text="", searches=0):
    return NS(stop_reason=stop,
              content=[NS(type="text", text=text)] if text else [NS(type="server_tool_use")],
              usage=NS(input_tokens=1000, output_tokens=200,
                       server_tool_use=NS(web_search_requests=searches)))


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def test_search_resumes_pause_turn_and_dedupes():
    body = json.dumps({"listings": [GOOD, GOOD, {**GOOD, "listing_url": "https://x.test/a", "price_per_chair": 10}]})
    client = FakeClient([_resp("pause_turn", searches=5), _resp("end_turn", body, searches=3)])
    res = pa.search(known_urls=["https://known.test/1"], client=client)
    assert len(client.calls) == 2
    # resume = original user turn + paused assistant turn, no extra "continue" message
    assert [m["role"] for m in client.calls[1]["messages"]] == ["user", "assistant"]
    assert "https://known.test/1" in client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["model"] == pa.MODEL
    assert len(res.listings) == 1 and len(res.rejected) == 1
    assert res.searches == 8 and res.input_tokens == 2000


def test_search_raises_on_refusal():
    with pytest.raises(RuntimeError):
        pa.search(client=FakeClient([_resp("refusal", "no")]))


def test_cost_estimate():
    res = pa.RunResult(searches=10, input_tokens=1_000_000, output_tokens=100_000)
    assert pa.cost_usd(res) == pytest.approx(0.10 + 0.05 + 0.10)


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
