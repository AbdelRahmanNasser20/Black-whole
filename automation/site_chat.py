"""The storefront chat: a fixed question script, no LLM.

The script is the operator's Messenger question form (2026-11-16) with two
steps in front of it: the buyer's ZIP, then a pick from the lots closest to
that ZIP. Every line the buyer sees is written here, and every fact in a line
(price, quantity, city, street address) comes from an `inventory` row, so the
chat cannot invent a price or a promise.

Pure module: no DB, no network. `site_chat_store` persists the state dict and
`web/site_chat_routes` serves it.

**What the chat may say (operator, 2026-11-17).** Price per chair, the
quantity in stock, the viewing hours (`HOURS`), and the street address of a
lot, on its own, with no approval step.

**Address rule.** The only address source is `inventory.pickup_address`, a
column the operator fills by hand with the street address a buyer may see.
`storage_note` is never read here: it holds gate codes and unit numbers. A lot
with no `pickup_address` is described by city and state only.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

HOURS = "9 AM to 5 PM, Monday to Friday"
NEAR_MILES = 300.0          # same radius as the CRM lot-to-buyer match (CRM-R300)
MAX_CARDS = 4
MAX_PICKS = 3
MAX_TEXT = 300              # one typed answer
MAX_LOG = 40                # question/answer pairs kept per session

# Lots we hold or have won: what "chairs we own" means when nothing is near.
OWNED_STATUSES = ("owned", "won_pickup")

STEPS = ("zip", "lots", "qty", "when", "fulfil", "pay", "org", "slot", "date", "name", "phone", "done")

# Short labels for the admin view ("stopped at ...").
STEP_LABELS = {
    "zip": "ZIP code", "lots": "Chair pick", "qty": "Quantity", "when": "Time frame",
    "fulfil": "Pickup or delivery", "pay": "Payment", "org": "Organization",
    "slot": "Viewing time", "date": "Viewing date", "name": "Name", "phone": "Phone",
    "done": "Finished",
}

QTY_OPTIONS = ("50", "100", "200")
WHEN_OPTIONS = ("Same day as viewing, if I like them", "This week", "Within 1 month")
FULFIL_OPTIONS = ("I can pick up", "I need delivery", "Not sure yet")
PAY_OPTIONS = ("Zelle", "Venmo", "Cash App")
ORG_OPTIONS = ("Nonprofit", "For-profit business", "Personal use")
SLOT_OPTIONS = ("9-11 AM", "11 AM-1 PM", "1-3 PM", "3-5 PM")
DATE_OPTIONS = ("No date yet",)
NONE_OF_THESE = "none"

_ZIP_RE = re.compile(r"^\s*(\d{5})(?:-\d{4})?\s*$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_INT_RE = re.compile(r"\d[\d,]*")


def new_state() -> dict:
    return {"step": "zip", "answers": {}, "log": [], "picked": []}


def clip(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


# ───────────────────────────── lot cards ─────────────────────────────

def _money(value: Any) -> str | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"${n:,.0f}" if n == int(n) else f"${n:,.2f}"


def lot_cards(rows: list[dict], zip_code: str | None, *,
              resolve: Callable[[str | None, str | None, str | None], tuple],
              distance: Callable[[float, float, float, float], float],
              hero: Callable[[dict], str | None],
              url: Callable[[dict], str],
              unit: Callable[[dict], str]) -> dict:
    """Pick the lots to show a buyer in `zip_code`.

    `rows` are `inventory.list_catalog_feed()` rows (every column, private ones
    included). The return value is an allow-list: only the keys built here ever
    leave the server.

    Returns ``{"mode": "near" | "owned" | "none", "cards": [...]}``. ``near`` =
    lots within `NEAR_MILES`, closest first. ``owned`` = nothing is near, so
    the lots we hold, closest first.
    """
    origin = resolve(None, None, zip_code) if zip_code else (None, None, None)
    o_lat, o_lon = origin[0], origin[1]
    cards: list[dict] = []
    for row in rows:
        if row.get("fake_sold_out"):
            continue
        try:
            qty = int(row.get("quantity_remaining") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        miles = None
        if o_lat is not None and o_lon is not None:
            lat, lon, _ = resolve(row.get("city"), row.get("state"), row.get("zip_code"))
            if lat is not None and lon is not None:
                miles = round(distance(o_lat, o_lon, lat, lon))
        place = ", ".join(p for p in (clip(row.get("city"), 60), clip(row.get("state"), 20)) if p)
        cards.append({
            "lot_id": str(row.get("lot_id")),
            "title": _short(clip(row.get("title"), 120)),   # "Brown MTS Convention Chairs", not the full lot title
            "quantity": qty,
            "unit": unit(row),
            "price": _money(row.get("price_per_chair")),
            "place": place,
            "distance_mi": miles,
            "hero": hero(row),
            "url": url(row),
            "address": clip(row.get("pickup_address"), 160) or None,
            "ship_only": (row.get("fulfillment") or "") == "dropship",
            "owned": row.get("status") in OWNED_STATUSES,
        })
    far = float("inf")
    by_distance = lambda c: (c["distance_mi"] if c["distance_mi"] is not None else far, -c["quantity"])
    near = sorted((c for c in cards if c["distance_mi"] is not None and c["distance_mi"] <= NEAR_MILES),
                  key=by_distance)
    if near:
        return {"mode": "near", "cards": near[:MAX_CARDS]}
    owned = sorted((c for c in cards if c["owned"]), key=by_distance)
    pool = owned or sorted(cards, key=by_distance)
    if pool:
        return {"mode": "owned", "cards": pool[:MAX_CARDS]}
    return {"mode": "none", "cards": []}


def _short(title: str) -> str:
    """A lot title cut at its first dash or bracket: enough to name it in a sentence."""
    return re.split(r"\s+[—–-]\s+|\s*\(", title, maxsplit=1)[0].strip() or title


# ───────────────────────────── the script ─────────────────────────────

def _ship_only(state: dict) -> bool:
    picked = state.get("picked") or []
    return bool(picked) and all(p.get("ship_only") for p in picked)


def _wants_delivery(state: dict) -> bool:
    return state["answers"].get("fulfil") == "I need delivery"


def _location_lines(state: dict) -> list[str]:
    lines = []
    for p in (state.get("picked") or [])[:MAX_PICKS]:
        name = _short(p.get("title") or "chairs")
        if p.get("ship_only"):
            continue
        if p.get("address"):
            lines.append(f"{name}: {p['address']}.")
        elif p.get("place"):
            lines.append(f"{name}: {p['place']}.")
    return lines


def prompt(state: dict, offer: dict | None = None) -> dict:
    """What to show for the current step: bot lines, tap options, lot cards.

    `offer` is the `lot_cards()` result and is only needed on the ``lots`` step.
    Every step also takes typed text; ``options`` are shortcuts, never a wall.
    """
    step = state["step"]
    a = state["answers"]
    out: dict[str, Any] = {"step": step, "messages": [], "options": [], "cards": [],
                           "input": "text", "placeholder": "Type your answer", "done": False}
    if step == "zip":
        out["messages"] = ["Hi! Looking for chairs?",
                           "What is your ZIP code? I will show you the chairs closest to you."]
        out["input"] = "zip"
        out["placeholder"] = "ZIP code"
    elif step == "lots":
        cards = (offer or {}).get("cards") or []
        if (offer or {}).get("mode") == "near":
            out["messages"] = [f"Here is what we have near {a.get('zip')}. Tap the chairs you like."]
        else:
            out["messages"] = [f"We have nothing close to {a.get('zip')} right now.",
                               "These are the chairs we have in stock. We ship anywhere in the US. "
                               "Tap the chairs you like."]
        out["cards"] = cards
        out["input"] = "lots"
        out["max_picks"] = MAX_PICKS
        out["placeholder"] = "Or tell us what you need"
    elif step == "qty":
        out["messages"] = ["How many chairs do you need?"]
        out["options"] = list(QTY_OPTIONS)
        out["placeholder"] = "Or type a number"
    elif step == "when":
        out["messages"] = ["When do you need them?"]
        out["options"] = list(WHEN_OPTIONS)
    elif step == "fulfil":
        lines = _location_lines(state)
        if lines:
            out["messages"].append("The chairs are here:\n" + "\n".join(lines))
            out["messages"].append(f"You can see them {HOURS}.")
        out["messages"].append("Can you pick up, or do you need delivery?")
        out["options"] = list(FULFIL_OPTIONS)
    elif step == "pay":
        out["messages"] = ["Zelle is best for us. We also take Venmo and Cash App. Which works for you?"]
        out["options"] = list(PAY_OPTIONS)
    elif step == "org":
        out["messages"] = ["Is this for a nonprofit (church, school, charity) or a for-profit business?"]
        out["options"] = list(ORG_OPTIONS)
    elif step == "slot":
        out["messages"] = [f"We show the chairs {HOURS}. What time works best?"]
        out["options"] = list(SLOT_OPTIONS)
    elif step == "date":
        out["messages"] = ["Do you have a date and time in mind to see the chairs?"]
        out["options"] = list(DATE_OPTIONS)
        out["placeholder"] = "For example: Friday at 10 AM"
    elif step == "name":
        out["messages"] = ["What is your name?"]
        out["input"] = "name"
        out["placeholder"] = "Your name"
    elif step == "phone":
        out["messages"] = ["What is the best phone number to text you? "
                           "It makes pickup and scheduling easier."]
        out["input"] = "phone"
        out["placeholder"] = "Phone number, or your email"
    else:
        first = (a.get("name") or "").split(" ")[0]
        thanks = f"Thanks {first}!" if first else "Thanks!"
        if _wants_delivery(state) or _ship_only(state):
            out["messages"] = [f"{thanks} Abdel will text you a freight quote."]
        else:
            out["messages"] = [f"{thanks} Abdel will text you soon."]
        out["input"] = "none"
        out["done"] = True
    return out


def _next_step(state: dict) -> str:
    """The step after the current one, with the skips applied."""
    order = list(STEPS)
    i = order.index(state["step"]) + 1
    while i < len(order):
        step = order[i]
        # A ship-only lot has no pickup and no viewing: never invite one.
        if step == "fulfil" and _ship_only(state):
            state["answers"]["fulfil"] = "I need delivery"
        elif step in ("slot", "date") and (_wants_delivery(state) or _ship_only(state)):
            pass
        else:
            return step
        i += 1
    return "done"


def _record(state: dict, question: str, answer: str) -> None:
    log = state.setdefault("log", [])
    log.append({"step": state["step"], "q": question, "a": answer, "ts": int(time.time())})
    del log[:-MAX_LOG]


def advance(state: dict, value: Any, offer: dict | None = None) -> str | None:
    """Apply one buyer answer to `state`. Returns an error line, or None when the
    answer was accepted and `state["step"]` moved on.

    `value` is a string for every step but ``lots``, where it is a list of lot
    ids, the string ``"none"``, or typed text.
    """
    step = state["step"]
    a = state["answers"]
    if step == "done":
        return None
    question = " ".join(prompt(state, offer)["messages"][-1:])

    if step == "lots":
        cards = {c["lot_id"]: c for c in (offer or {}).get("cards") or []}
        if isinstance(value, list):
            ids = [str(v) for v in value if str(v) in cards][:MAX_PICKS]
            if not ids:
                return "Tap at least one of the chairs, or tap None of these."
            state["picked"] = [
                {k: cards[i][k] for k in ("lot_id", "title", "url", "price", "place", "address", "ship_only")}
                for i in ids]
            answer = ", ".join(_short(cards[i]["title"]) for i in ids)
        else:
            text = clip(value)
            if not text:
                return "Tap the chairs you like, or type what you need."
            state["picked"] = []
            if text.lower() == NONE_OF_THESE:
                answer = "None of these"
            else:
                answer = text
                a["wanted"] = text
        a["lots"] = answer
    else:
        text = clip(value)
        if not text:
            return "Please type an answer."
        if step == "zip":
            m = _ZIP_RE.match(text)
            if not m:
                return "That does not look like a US ZIP code. Please type 5 digits."
            answer = m.group(1)
        elif step == "phone":
            digits = re.sub(r"\D", "", text)
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) == 10 and digits[0] not in "01":
                answer = digits
                a["email"] = None
            elif _EMAIL_RE.match(text):
                answer = text
                a["email"] = text
            else:
                return "Please type a 10 digit phone number or an email address."
        elif step == "name":
            answer = clip(text, 80)
        else:
            answer = text
        a[step] = answer
        if step == "phone" and a.get("email"):
            a["phone"] = None

    _record(state, question, answer)
    state["step"] = _next_step(state)
    return None


# ───────────────────────────── the lead ─────────────────────────────

def quantity(state: dict) -> int | None:
    m = _INT_RE.search(str(state["answers"].get("qty") or ""))
    if not m:
        return None
    try:
        n = int(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return n if 0 < n < 1_000_000 else None


def summary(state: dict) -> str:
    """One line for `inquiries.message` and the Telegram alert."""
    a = state["answers"]
    parts = [
        ("chairs", a.get("lots")), ("qty", a.get("qty")), ("when", a.get("when")),
        ("pickup or delivery", a.get("fulfil")), ("pay", a.get("pay")), ("for", a.get("org")),
        ("time", a.get("slot")), ("date", a.get("date")),
    ]
    return "[SITE CHAT] " + " | ".join(f"{k}: {v}" for k, v in parts if v)


def lead(state: dict) -> dict:
    """The `inventory.create_inquiry` keyword arguments for a finished chat."""
    a = state["answers"]
    picked = state.get("picked") or []
    return {
        "kind": "buy",
        "name": a.get("name") or "Chat visitor",
        "phone": a.get("phone") or None,
        "email": a.get("email") or None,
        "zip_code": a.get("zip"),
        "quantity_interested": quantity(state),
        "lot_id": picked[0]["lot_id"] if picked else None,
        "message": summary(state),
    }
