"""The Sales rail badge clears once the operator has looked (2026-10-18 report).

`/api/sales/counts` says WHAT is new (the ids), not only how many, so shell.js
can remember per browser which ones the operator has already seen on the view
itself; a successful write through `ui/state.js::api()` announces itself so
the badge is re-read right after a status change instead of on the next tab
switch. Server side nothing was broken — the readcache middleware already drops
the memo on a write — and that is pinned here too.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import freight_log, inventory
from automation.web import auth as auth_svc
from automation.web import readcache
from automation.web.app import app

SHELL = Path("automation/web/static/admin/shell.js")
STATE = Path("automation/web/static/ui/state.js")


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    for var in ("ADMIN_PASSWORD", "SESSION_SECRET", "TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)
    auth_svc.reset_caches()
    readcache.invalidate_all()
    yield
    auth_svc.reset_caches()
    readcache.invalidate_all()


@pytest.fixture
def ledger(monkeypatch):
    """In-memory inquiries + quotes: the reads the counts endpoint and the
    inquiries PATCH lean on."""
    state = {
        "inquiries": [{"id": 11, "status": "new"}, {"id": 12, "status": "new"}, {"id": 13, "status": "contacted"}],
        "quote_ids": [7, 9],
    }

    def list_inquiries(status=None):
        return [dict(r) for r in state["inquiries"] if not status or r["status"] == status]

    def set_inquiry_status(inquiry_id, status):
        if status not in ("new", "contacted", "closed"):
            raise ValueError(f"invalid status: {status}")
        row = next((r for r in state["inquiries"] if r["id"] == inquiry_id), None)
        if row is not None:
            row["status"] = status
        return dict(row) if row else None

    monkeypatch.setattr(inventory, "list_inquiries", list_inquiries)
    monkeypatch.setattr(inventory, "set_inquiry_status", set_inquiry_status)
    monkeypatch.setattr(inventory, "get_inquiry",
                        lambda i: next((dict(r) for r in state["inquiries"] if r["id"] == i), None))
    monkeypatch.setattr(freight_log, "new_ids", lambda: list(state["quote_ids"]))
    return state


def _client():
    return TestClient(app, base_url="https://testserver")


# ─────────────────────────────────── API ────────────────────────────────────

def test_counts_say_which_rows_are_new(ledger):
    body = _client().get("/api/sales/counts").json()
    assert body == {
        "quotes_new": 2, "quotes_new_ids": [7, 9],
        "inquiries_new": 2, "inquiries_new_ids": [11, 12],
    }


def test_counts_drop_right_after_an_inquiry_status_write(ledger):
    """The memo (15 s) must not outlive the operator's own edit."""
    c = _client()
    assert c.get("/api/sales/counts").json()["inquiries_new"] == 2
    assert c.get("/api/sales/counts").json()["inquiries_new"] == 2       # served from the memo
    assert c.patch("/api/inquiries/11", json={"status": "contacted"}).status_code == 200
    body = c.get("/api/sales/counts").json()
    assert body["inquiries_new"] == 1 and body["inquiries_new_ids"] == [12]


def test_a_rejected_write_keeps_the_count(ledger):
    c = _client()
    assert c.get("/api/sales/counts").json()["inquiries_new"] == 2
    assert c.patch("/api/inquiries/11", json={"status": "bogus"}).status_code == 400
    assert c.get("/api/sales/counts").json()["inquiries_new"] == 2


# ─────────────────────────────── shell.js ───────────────────────────────────

def test_shell_marks_the_open_sales_view_seen_and_counts_only_unseen():
    src = SHELL.read_text()
    fn = src.split("function refreshSalesCounts()")[1].split("\n}\n")[0]
    # the ids, not just the number, drive the rail badge
    assert "quotes_new_ids" in fn and "inquiries_new_ids" in fn
    # viewing a Sales view = seen (per browser): the rail badge drops, the view's own count keeps the real total
    assert "'admin.sales.seen'" in src
    assert "tabFromUrl()" in fn, "the view the operator is looking at right now is what gets marked seen"
    assert re.search(r"setCount\(\$\('#sales-badge'\),\s*unseen\(", fn), "rail badge = unseen new rows"
    assert re.search(r"setCount\(\$\('#sales-count-inquiries'\),\s*ids\.inquiries\.length\)", fn), "view count = every new row"
    # every localStorage touch is guarded — a blocked storage must not take the badge down with it
    touches = list(re.finditer(r"localStorage\.(getItem|setItem)\(SEEN_KEY", src))
    assert len(touches) == 2, "one read, one write of the seen list"
    for m in touches:
        assert "try" in src[max(0, m.start() - 80):m.start()], src[m.start() - 80:m.end()]


def test_shell_rereads_the_counts_after_any_write():
    src = SHELL.read_text()
    assert re.search(r"document\.addEventListener\('admin:write',\s*\(\)\s*=>\s*refreshSalesCounts\(\)\)", src)


def test_state_api_announces_a_successful_write_only():
    src = STATE.read_text()
    fn = src.split("export async function api(")[1].split("\n}\n")[0]
    assert "new CustomEvent('admin:write'" in fn
    # dispatched after the ok check, never for a read
    assert fn.index("if (!res.ok)") < fn.index("admin:write")
    assert "'GET'" in fn, "reads do not announce"
