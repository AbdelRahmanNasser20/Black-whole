"""Buyers say which chair type they want (banquet, church, folding, Chiavari ...).

One list (`automation/chair_types.py`) feeds the inquiry forms, the server check
and the site chat. The answer rides at the top of `inquiries.message`, so the
Telegram ping and the admin Inquiries card show it with no migration.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import chair_types, inventory, site_chat as sc
from automation.web.app import app

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "automation" / "web" / "templates"


class _FakeCur:
    def fetchone(self):
        return {"id": 1}


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self._sink["sql"] = sql
        self._sink["params"] = params
        return _FakeCur()

    def commit(self):
        pass


@pytest.fixture
def sink(monkeypatch):
    seen = {}
    monkeypatch.setattr(inventory, "connect", lambda: _FakeConn(seen))
    monkeypatch.setattr(inventory, "get_inquiry", lambda i: {"id": i})
    monkeypatch.setattr(inventory, "inquiries_have_zip_column", lambda: True)
    monkeypatch.setattr(inventory.attribution_mod, "columns_ready", lambda table: False)
    return seen


def test_the_list_has_the_operator_types():
    assert chair_types.CHAIR_TYPES == (
        "Banquet (stackable)", "Church chairs", "Folding with pads",
        "Metal folding", "Chiavari", "Other / not sure",
    )


# ───────────────────────────── create_inquiry ────────────────────────

def test_chair_type_goes_on_top_of_the_message(sink):
    inventory.create_inquiry(kind="buy", name="Dana", email="d@x.com", zip_code="30301",
                             message="Need 200 by March", chair_type="Chiavari")
    assert sink["params"][6] == "Chair type: Chiavari\n\nNeed 200 by March"


def test_chair_type_alone_is_the_message(sink):
    inventory.create_inquiry(kind="buy", name="Dana", email="d@x.com", zip_code="30301",
                             chair_type="Metal folding")
    assert sink["params"][6] == "Chair type: Metal folding"


def test_unknown_chair_type_is_rejected(sink):
    with pytest.raises(ValueError, match="chair type"):
        inventory.create_inquiry(kind="buy", name="Dana", email="d@x.com", zip_code="30301",
                                 chair_type="<script>")
    assert "sql" not in sink


def test_no_chair_type_leaves_the_message_alone(sink):
    """Pages cached before this deploy do not send the field."""
    inventory.create_inquiry(kind="buy", name="Dana", email="d@x.com", zip_code="30301",
                             message="Hi")
    assert sink["params"][6] == "Hi"


def test_contact_passes_chair_type(monkeypatch):
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return {"id": 1, "kind": "buy", "name": "Dana"}
    monkeypatch.setattr(inventory, "create_inquiry", fake_create)
    client = TestClient(app, base_url="https://testserver")
    r = client.post("/contact", json={"kind": "buy", "name": "Dana", "email": "d@x.com",
                                      "zip_code": "30301", "chair_type": "Church chairs"})
    assert r.status_code == 200
    assert captured["chair_type"] == "Church chairs"


# ───────────────────────────── forms ─────────────────────────────────

@pytest.mark.parametrize("template", ["about.html", "landing.html"])
def test_general_inquiry_forms_require_a_chair_type(template):
    html = (TEMPLATES / template).read_text()
    form = html.split('id="contact-form"', 1)[1].split("</form>", 1)[0]
    select = form.split('name="chair_type"', 1)[1].split("</select>", 1)[0]
    assert "required" in select.split(">", 1)[0]
    assert "chair_types" in select          # options come from the one list


def test_template_global_is_the_list():
    from automation.web.app import templates
    assert templates.env.globals["chair_types"] == chair_types.CHAIR_TYPES


# ───────────────────────────── site chat ─────────────────────────────

def _to_lots(zip_code="30301"):
    state = sc.new_state()
    sc.advance(state, zip_code)
    return state


def test_chat_asks_chair_type_when_no_lot_is_picked():
    state = _to_lots()
    sc.advance(state, "none", {"mode": "near", "cards": []})
    assert state["step"] == "type"
    p = sc.prompt(state)
    assert p["options"] == list(chair_types.CHAIR_TYPES)
    sc.advance(state, "Chiavari")
    assert state["step"] == "qty"
    assert "type: Chiavari" in sc.summary(state)


def test_chat_skips_chair_type_when_a_lot_is_picked():
    state = _to_lots()
    card = {"lot_id": "9006", "title": "Mauve Banquet Chairs", "url": "/x", "price": None,
            "place": "Phoenix, AZ", "address": None, "ship_only": False}
    sc.advance(state, ["9006"], {"mode": "near", "cards": [card]})
    assert state["step"] == "qty"


def test_launcher_says_chat_with_us():
    html = (TEMPLATES / "_site_chat.html").read_text()
    launch = html.split('class="bwchat-launch"', 1)[1].split("</button>", 1)[0]
    assert "Chat with us" in launch and "Find chairs near you" in launch
