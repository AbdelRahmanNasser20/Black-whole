"""Storefront chat endpoints (automation/web/site_chat_routes.py) and the widget partial.
The DB is replaced by an in-memory store. The script itself is covered by tests/test_site_chat_flow.py."""
import copy
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import inventory, site_chat_store, telegram_alerts
from automation.web import auth as auth_svc
from automation.web import rate_limit, readcache
from automation.web import site_chat_routes as routes
from automation.web.app import app

STATIC = Path("automation/web/static")
TEMPLATES = Path("automation/web/templates")

ATL = {"lot_id": "31225-atl", "title": "Brown MTS Convention Chairs — Atlanta, GA", "status": "won_pickup",
       "city": "Atlanta", "state": "GA", "zip_code": "30336", "quantity_remaining": 1200,
       "price_per_chair": 25, "fake_sold_out": False,
       "pickup_address": "675 Great SW Pkwy SW, Atlanta, GA 30336",
       "storage_note": "dock 4, gate code 4471#", "contact_phone": "555-000-1111"}


@pytest.fixture
def chat(monkeypatch):
    """An in-memory chat_sessions table, a ready schema, and spies on the lead writers."""
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SITE_CHAT_ENABLED", raising=False)
    auth_svc.reset_caches()
    rate_limit.reset()
    readcache.invalidate_all()
    rows: dict[str, dict] = {}
    spy = {"inquiries": [], "telegram": [], "subscribed": [], "rows": rows}

    def create(state, facts):
        sid = f"00000000-0000-4000-8000-{len(rows):012d}"
        rows[sid] = {"id": sid, "status": "open", "step": state["step"], "state": copy.deepcopy(state),
                     "inquiry_id": None, "facts": facts}
        return sid

    def save(sid, state, *, trail=None, inquiry_id=None):
        rows[sid].update(state=copy.deepcopy(state), step=state["step"], trail=trail,
                         inquiry_id=inquiry_id or rows[sid]["inquiry_id"],
                         status="completed" if state["step"] == "done" else "open")

    def create_inquiry(**kw):
        spy["inquiries"].append(kw)
        return {"id": 77, **kw}

    monkeypatch.setattr(site_chat_store, "schema_ready", lambda: True)
    monkeypatch.setattr(site_chat_store, "_schema_ready", True)
    monkeypatch.setattr(site_chat_store, "create", create)
    monkeypatch.setattr(site_chat_store, "save", save)
    monkeypatch.setattr(site_chat_store, "get", lambda sid: copy.deepcopy(rows.get(sid)) if sid else None)
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [dict(ATL)])
    monkeypatch.setattr(inventory, "create_inquiry", create_inquiry)
    monkeypatch.setattr(inventory, "subscribe_from_inquiry", lambda inq, attr=None: spy["subscribed"].append(inq))
    monkeypatch.setattr(telegram_alerts, "send_message_sync",
                        lambda text, topic=None: spy["telegram"].append((topic, text)) or (True, None))
    monkeypatch.setattr(routes, "_lots_memo", (0.0, []))
    # The lead's side effects run on a thread in production. Inline here so the test can see them.
    monkeypatch.setattr(routes.threading, "Thread", _InlineThread)
    from automation.alerts import geo
    monkeypatch.setattr(geo, "resolve_place", lambda c, s, z: (33.7, -84.4, "city") if (c == "Atlanta" or z == "30301") else (None, None, None))
    yield spy
    rate_limit.reset()
    auth_svc.reset_caches()


class _InlineThread:
    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _client():
    return TestClient(app)


def _start(c, **extra):
    body = {"page": "/listings", "visitor": {"id": "abcdef12-3456", "first": 1763300000000, "days": 2},
            "trail": [{"p": "/", "t": 1763300000}, {"p": "/listings", "t": 1763300050}],
            "attribution": {"source": "Google", "medium": "cpc", "campaign": "atl", "referrer": "", "landing": "/"},
            **extra}
    return c.post("/chat/start", json=body)


def test_start_opens_a_chat_and_stores_the_visitor_facts(chat):
    r = _start(_client())
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["step"] == "zip" and body["history"] == [] and "ZIP" in body["messages"][-1]
    facts = chat["rows"][body["sid"]]["facts"]
    assert facts["visitor_id"] == "abcdef12-3456" and facts["visit_days"] == 2
    assert facts["page"] == "/listings" and [t["p"] for t in facts["trail"]] == ["/", "/listings"]
    assert facts["attr_source"] == "google" and facts["attr_campaign"] == "atl"


def test_full_chat_files_one_inquiry_one_alert_and_never_leaks_private_fields(chat):
    c = _client()
    sid = _start(c).json()["sid"]
    say = lambda value: c.post("/chat/answer", json={"sid": sid, "value": value}).json()

    lots = say("30301")
    assert lots["step"] == "lots" and lots["cards"][0]["lot_id"] == "31225-atl"
    assert lots["cards"][0]["price"] == "$25" and lots["cards"][0]["quantity"] == 1200
    seen = [repr(lots)]
    seen.append(repr(say(["31225-atl"])))
    seen.append(repr(say("100")))
    fulfil = say("This week")
    assert "675 Great SW Pkwy SW, Atlanta, GA 30336" in "\n".join(fulfil["messages"])
    assert "9 AM to 5 PM, Monday to Friday" in "\n".join(fulfil["messages"])
    seen.append(repr(fulfil))
    for value in ("I can pick up", "Zelle", "Nonprofit", "9-11 AM", "Friday 10 AM", "Dana Reed"):
        seen.append(repr(say(value)))
    done = say("404-555-0143")
    assert done["done"] is True and done["messages"] == ["Thanks Dana! Abdel will text you soon."]
    for blob in seen:
        for secret in ("4471", "gate code", "storage_note", "555-000-1111"):
            assert secret not in blob

    assert len(chat["inquiries"]) == 1
    inq = chat["inquiries"][0]
    assert inq["kind"] == "buy" and inq["name"] == "Dana Reed" and inq["phone"] == "4045550143"
    assert inq["zip_code"] == "30301" and inq["quantity_interested"] == 100 and inq["lot_id"] == "31225-atl"
    assert inq["message"].startswith("[SITE CHAT] ")
    assert len(chat["subscribed"]) == 1
    assert len(chat["telegram"]) == 1 and chat["telegram"][0][0] == "leads"
    assert "CHAT LEAD #77" in chat["telegram"][0][1] and "4045550143" in chat["telegram"][0][1]
    row = chat["rows"][sid]
    assert row["status"] == "completed" and row["inquiry_id"] == 77

    # A late or replayed answer does not file a second lead.
    assert say("again")["done"] is True
    assert len(chat["inquiries"]) == 1 and len(chat["telegram"]) == 1


def test_bad_answer_returns_an_error_and_keeps_the_step(chat):
    c = _client()
    sid = _start(c).json()["sid"]
    r = c.post("/chat/answer", json={"sid": sid, "value": "atlanta"}).json()
    assert r["step"] == "zip" and "ZIP" in r["error"]
    assert chat["rows"][sid]["state"]["log"] == []


def test_resume_returns_the_history_and_the_current_question(chat):
    c = _client()
    sid = _start(c).json()["sid"]
    c.post("/chat/answer", json={"sid": sid, "value": "30301"})
    c.post("/chat/answer", json={"sid": sid, "value": "none"})
    again = _start(c, sid=sid).json()
    assert again["sid"] == sid and again["step"] == "qty"
    assert [h["a"] for h in again["history"]] == ["30301", "None of these"]
    assert len(chat["rows"]) == 1


def test_a_lead_is_kept_when_the_inquiry_write_fails(chat, monkeypatch):
    def boom(**kw):
        raise ValueError("zip required")
    monkeypatch.setattr(inventory, "create_inquiry", boom)
    c = _client()
    sid = _start(c).json()["sid"]
    for value in ("30301", "none", "50", "This week", "I need delivery", "Zelle", "Nonprofit", "Sam", "sam@church.org"):
        r = c.post("/chat/answer", json={"sid": sid, "value": value})
        assert r.status_code == 200
    assert chat["rows"][sid]["status"] == "completed"
    assert "NOT saved" in chat["telegram"][0][1] and "sam@church.org" in chat["telegram"][0][1]


def test_unknown_chat_is_404_and_the_limiter_answers_429(chat, monkeypatch):
    c = _client()
    assert c.post("/chat/answer", json={"sid": "00000000-0000-4000-8000-999999999999", "value": "x"}).status_code == 404
    monkeypatch.setattr(routes, "START_PER_IP_LIMIT", 2)
    assert [_start(c).status_code for _ in range(3)] == [200, 200, 429]


def test_chat_is_dark_until_the_migration_is_applied(chat, monkeypatch):
    monkeypatch.setattr(site_chat_store, "schema_ready", lambda: False)
    monkeypatch.setattr(site_chat_store, "_schema_ready", False)
    c = _client()
    assert _start(c).status_code == 503
    assert 'id="bwchat"' not in c.get("/terms").text


def test_env_switch_turns_the_chat_off(chat, monkeypatch):
    monkeypatch.setenv("SITE_CHAT_ENABLED", "0")
    c = _client()
    assert _start(c).status_code == 503
    assert 'id="bwchat"' not in c.get("/terms").text


def test_widget_renders_once_on_storefront_pages_only(chat):
    c = _client()
    html = c.get("/terms").text
    assert html.count('id="bwchat"') == 1
    assert "/static/site/site_chat.js" in html and "/static/site/site_chat.css" in html
    assert 'id="bwchat"' not in c.get("/sell").text
    assert 'id="bwchat"' not in c.get("/admin").text
    for name in ("site_chat.js", "site_chat.css"):
        assert c.get(f"/static/site/{name}").status_code == 200


def test_widget_source_rules():
    partial = (TEMPLATES / "_site_chat.html").read_text()
    body = re.sub(r"\{#.*?#\}", "", partial, flags=re.S)
    assert "storage_note" not in body
    for admin in ("index.html", "_base.html"):
        assert "_site_chat.html" not in (TEMPLATES / admin).read_text()
    js = (STATIC / "site/site_chat.js").read_text()
    assert "fetch(" not in js and "innerHTML" not in js       # api() only, and stored text is never HTML
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "site/site_chat.css").read_text(), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)


def test_visitor_facts_drop_junk():
    facts = site_chat_store.visitor_facts(
        {"visitor": {"id": "<script>", "first": "x", "days": -5}, "page": "javascript:alert(1)",
         "trail": [{"p": "http://evil", "t": 1}, {"p": "/ok", "t": "nope"}, "junk"] + [{"p": f"/p{i}", "t": 1763300000 + i} for i in range(50)]},
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Mobile", country="us", attribution=None)
    assert facts["visitor_id"] is None and facts["first_seen"] is None and facts["page"] is None
    assert facts["visit_days"] == 1 and facts["device"] == "phone" and facts["country"] == "US"
    assert len(facts["trail"]) == site_chat_store.MAX_TRAIL and all(t["p"].startswith("/p") for t in facts["trail"])
