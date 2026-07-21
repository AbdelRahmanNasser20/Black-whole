"""BLACKWHOLE-24: forum-topic routing for Telegram alerts.

Covers thread-id resolution, that send_message attaches message_thread_id only
when a topic is configured (backward-compat when it isn't), and that the new
contact-form inquiry alert routes to the Leads tab with the quantity."""
import asyncio

from automation import telegram_alerts as tg


class _FakeResp:
    status_code = 200
    text = ""


class _FakeClient:
    """Captures the JSON payload of the last post() across instances."""
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _FakeClient.captured = json
        return _FakeResp()


def test_thread_id_for_resolution(monkeypatch):
    monkeypatch.setattr(tg, "_TOPICS",
                        {"leads": "2", "deals": "  ", "health": None, "poller": "x"})
    assert tg.thread_id_for("leads") == 2
    assert tg.thread_id_for("deals") is None    # blank string
    assert tg.thread_id_for("health") is None   # unset
    assert tg.thread_id_for("poller") is None   # non-numeric
    assert tg.thread_id_for(None) is None
    assert tg.thread_id_for("nope") is None      # unknown channel


def test_send_message_attaches_thread_when_configured(monkeypatch):
    monkeypatch.setattr(tg, "TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setattr(tg, "TELEGRAM_CHAT_ID", "-1004474425293")
    monkeypatch.setattr(tg, "_TOPICS", {"deals": "3"})
    monkeypatch.setattr(tg.httpx, "AsyncClient", _FakeClient)
    ok, err = asyncio.run(tg.send_message("hi", topic="deals"))
    assert ok and err is None
    assert _FakeClient.captured["chat_id"] == "-1004474425293"
    assert _FakeClient.captured["message_thread_id"] == 3


def test_send_message_omits_thread_without_topic(monkeypatch):
    monkeypatch.setattr(tg, "TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setattr(tg, "TELEGRAM_CHAT_ID", "-1004474425293")
    monkeypatch.setattr(tg, "_TOPICS", {"deals": "3"})
    monkeypatch.setattr(tg.httpx, "AsyncClient", _FakeClient)
    ok, _ = asyncio.run(tg.send_message("hi"))  # no topic → General
    assert ok
    assert "message_thread_id" not in _FakeClient.captured


def test_send_message_unconfigured_is_noop(monkeypatch):
    monkeypatch.setattr(tg, "TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr(tg, "TELEGRAM_CHAT_ID", None)
    ok, err = asyncio.run(tg.send_message("hi", topic="deals"))
    assert ok is False and err == "telegram_not_configured"


def test_new_inquiry_alert_routes_to_leads_with_qty(monkeypatch):
    import importlib
    # `automation.web` re-exports the FastAPI instance as `app`, shadowing the
    # module — import the module explicitly so we can reach its functions.
    web_app = importlib.import_module("automation.web.app")

    sent: list[tuple] = []

    async def fake_send(text, *, topic=None):
        sent.append((topic, text))
        return True, None

    monkeypatch.setattr(web_app.telegram_alerts, "send_message", fake_send)
    row = {"id": 9, "kind": "buy", "name": "Jane Buyer",
           "email": "jane@example.com", "phone": None,
           "message": "need 500 folding chairs", "lot_id": "123",
           "quantity_interested": 500}
    asyncio.run(web_app._notify_new_inquiry(row))

    assert len(sent) == 1
    topic, body = sent[0]
    assert topic == "leads"
    assert "NEW LEAD" in body
    assert "qty 500" in body
    assert "Jane Buyer" in body
    assert "lot 123" in body
