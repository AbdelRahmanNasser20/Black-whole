"""The storefront chat script (automation/site_chat.py): pure, no DB."""
from automation import site_chat as sc

ATL = {"lot_id": "31225-atl", "title": "Brown MTS Convention Chairs — Atlanta, GA (3,700 total)",
       "status": "won_pickup", "city": "Atlanta", "state": "GA", "zip_code": "30336",
       "quantity_remaining": 1200, "price_per_chair": 25, "fake_sold_out": False,
       "pickup_address": "675 Great SW Pkwy SW, Atlanta, GA 30336",
       "storage_note": "Unit 12 gate code 4471#", "govdeals_password": "hunter2"}
PHX = {"lot_id": "9006", "title": "Mauve Banquet Chairs", "status": "owned", "city": "Phoenix",
       "state": "AZ", "zip_code": None, "quantity_remaining": 442, "price_per_chair": 28,
       "fake_sold_out": False, "storage_note": "Public Storage, gate 9911"}
MAINE = {"lot_id": "bs-1", "title": "552 Mahogany Chiavari Chairs (Oxford, ME)", "status": "active_bid",
         "city": "Oxford", "state": "ME", "zip_code": "04270", "quantity_remaining": 552,
         "price_per_chair": 30, "fake_sold_out": False}
ROWS = [ATL, PHX, MAINE]

_PLACES = {"30301": (33.75, -84.39), "Atlanta": (33.75, -84.39), "Phoenix": (33.45, -112.07),
           "Oxford": (44.13, -70.49), "59101": (45.78, -108.50)}


def _resolve(city, state, zip_code):
    hit = _PLACES.get(city) or _PLACES.get(zip_code)
    return (hit[0], hit[1], "city") if hit else (None, None, None)


def _distance(lat1, lon1, lat2, lon2):
    return ((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2) ** 0.5 * 60


def _cards(rows, zip_code):
    return sc.lot_cards(rows, zip_code, resolve=_resolve, distance=_distance,
                        hero=lambda r: f"/img/{r['lot_id']}.jpg", url=lambda r: f"/listings/{r['lot_id']}",
                        unit=lambda r: "chair")


def test_near_zip_gets_the_closest_lot_first():
    offer = _cards(ROWS, "30301")
    assert offer["mode"] == "near"
    assert [c["lot_id"] for c in offer["cards"]] == ["31225-atl"]
    card = offer["cards"][0]
    assert card["price"] == "$25" and card["quantity"] == 1200 and card["distance_mi"] == 0


def test_far_zip_gets_the_lots_we_own():
    offer = _cards(ROWS, "59101")          # Billings, MT: nothing within 300 miles
    assert offer["mode"] == "owned"
    assert {c["lot_id"] for c in offer["cards"]} == {"31225-atl", "9006"}      # not the active bid


def test_cards_are_an_allow_list():
    offer = _cards(ROWS, "30301")
    blob = repr(offer)
    for secret in ("4471", "9911", "hunter2", "storage_note", "gate"):
        assert secret not in blob
    assert set(offer["cards"][0]) == {"lot_id", "title", "quantity", "unit", "price", "place",
                                      "distance_mi", "hero", "url", "address", "ship_only", "owned"}


def test_fake_sold_out_and_empty_lots_are_never_offered():
    rows = [dict(ATL, fake_sold_out=True), dict(PHX, quantity_remaining=0)]
    assert _cards(rows, "30301") == {"mode": "none", "cards": []}


def _run(answers, rows=ROWS):
    """Drive the script; `answers` maps step -> value. Returns the final state."""
    state = sc.new_state()
    offer = None
    for _ in range(30):
        step = state["step"]
        if step == "done":
            break
        if step == "lots":
            offer = _cards(rows, state["answers"]["zip"])
        err = sc.advance(state, answers[step], offer)
        assert err is None, (step, err)
    return state


FULL = {"zip": "30301", "lots": ["31225-atl"], "qty": "100", "when": "This week",
        "fulfil": "I can pick up", "pay": "Zelle", "org": "Nonprofit", "slot": "9-11 AM",
        "date": "Friday at 10", "name": "Dana Reed", "phone": "(404) 555-0143"}


def test_full_pickup_chat_asks_every_question_and_builds_the_lead():
    state = _run(FULL)
    assert state["step"] == "done"
    assert [e["step"] for e in state["log"]] == list(sc.STEPS[:-1])
    lead = sc.lead(state)
    assert lead["name"] == "Dana Reed" and lead["phone"] == "4045550143" and lead["email"] is None
    assert lead["zip_code"] == "30301" and lead["quantity_interested"] == 100
    assert lead["lot_id"] == "31225-atl"
    assert lead["message"].startswith("[SITE CHAT] ") and "Zelle" in lead["message"]
    assert sc.prompt(state)["messages"] == ["Thanks Dana! Abdel will text you soon."]


def test_pickup_question_gives_the_street_address_and_the_hours():
    state = sc.new_state()
    sc.advance(state, "30301")
    sc.advance(state, ["31225-atl"], _cards(ROWS, "30301"))
    sc.advance(state, "100")
    sc.advance(state, "This week")
    text = "\n".join(sc.prompt(state)["messages"])
    assert "675 Great SW Pkwy SW, Atlanta, GA 30336" in text
    assert "9 AM to 5 PM, Monday to Friday" in text
    assert "4471" not in text and "gate" not in text.lower()


def test_lot_without_a_pickup_address_gets_city_only():
    state = sc.new_state()
    sc.advance(state, "59101")
    sc.advance(state, ["9006"], _cards(ROWS, "59101"))
    sc.advance(state, "50")
    sc.advance(state, "This week")
    text = "\n".join(sc.prompt(state)["messages"])
    assert "Mauve Banquet Chairs: Phoenix, AZ." in text
    assert "Public Storage" not in text and "9911" not in text


def test_delivery_skips_the_viewing_questions():
    state = _run(dict(FULL, fulfil="I need delivery"))
    steps = [e["step"] for e in state["log"]]
    assert "slot" not in steps and "date" not in steps
    assert sc.prompt(state)["messages"] == ["Thanks Dana! Abdel will text you a freight quote."]


def test_ship_only_lot_never_invites_a_viewing():
    rows = [dict(ATL, fulfillment="dropship")]
    state = _run({k: v for k, v in FULL.items() if k not in ("fulfil", "slot", "date")}, rows)
    steps = [e["step"] for e in state["log"]]
    assert not {"fulfil", "slot", "date"} & set(steps)
    assert state["answers"]["fulfil"] == "I need delivery"


def test_typed_answers_are_accepted_on_button_questions():
    state = _run(dict(FULL, qty="about 75 for our hall", when="before Christmas", pay="check"))
    assert state["answers"]["qty"] == "about 75 for our hall"
    assert sc.quantity(state) == 75


def test_none_of_these_and_typed_wish_keep_the_chat_going():
    state = _run(dict(FULL, lots="none"))
    assert state["picked"] == [] and sc.lead(state)["lot_id"] is None
    state = _run(dict(FULL, lots="white folding chairs"))
    assert state["answers"]["wanted"] == "white folding chairs"


def test_bad_answers_ask_again_without_moving_on():
    state = sc.new_state()
    assert "ZIP" in sc.advance(state, "atlanta")
    assert state["step"] == "zip" and state["log"] == []
    sc.advance(state, "30301")
    assert sc.advance(state, ["not-a-lot"], _cards(ROWS, "30301"))
    assert state["step"] == "lots"
    state = _run({k: v for k, v in FULL.items() if k != "phone"} | {"phone": "4045550143"})
    state2 = sc.new_state()
    state2.update(step="phone")
    assert sc.advance(state2, "12345")
    assert sc.advance(state2, "dana@church.org") is None
    assert sc.lead(state2)["email"] == "dana@church.org" and sc.lead(state2)["phone"] is None


def test_long_answers_are_clipped_and_the_log_is_capped():
    state = sc.new_state()
    state["step"] = "when"
    sc.advance(state, "x" * 5000)
    assert len(state["answers"]["when"]) == sc.MAX_TEXT
    state["log"] = [{"step": "qty", "q": "", "a": "", "ts": 0}] * 100
    state["step"] = "when"
    sc.advance(state, "soon")
    assert len(state["log"]) == sc.MAX_LOG


def test_no_dashes_in_any_line_the_bot_says():
    """Customer voice rule: no em or en dashes."""
    state = sc.new_state()
    for step in sc.STEPS:
        state["step"] = step
        for line in sc.prompt(state, {"mode": "owned", "cards": []})["messages"]:
            assert "—" not in line and "–" not in line, (step, line)


def test_pickup_address_rejects_codes_and_unit_numbers(monkeypatch):
    """The admin field feeds buyers: text from storage_note must not get in."""
    import pytest
    from automation import inventory

    def no_db(*a, **k):
        raise AssertionError("validation must fail before any DB call")
    monkeypatch.setattr(inventory, "connect", no_db)
    for bad in ("675 Great SW Pkwy, gate code 4471", "10199 W Van Buren St #8", "Public Storage unit 212"):
        with pytest.raises(ValueError):
            inventory.set_fields("lot-1", pickup_address=bad)
