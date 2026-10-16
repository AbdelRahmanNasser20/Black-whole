"""Alert matcher run_pass against an in-memory fake that emulates the SQL semantics."""
import re
from datetime import datetime, timedelta, timezone

import pytest

from automation import db, platform_alerts, platform_email

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
UID = "11111111-1111-1111-1111-111111111111"


def like_to_re(pat):
    out, i = "", 0
    while i < len(pat):
        c = pat[i]
        if c == "\\" and i + 1 < len(pat):
            out += re.escape(pat[i + 1]); i += 2; continue
        out += ".*" if c == "%" else "." if c == "_" else re.escape(c)
        i += 1
    return re.compile("^" + out + "$", re.I | re.S)


class FakeDB:
    def __init__(self):
        self.alerts, self.lots, self.sends, self.fired = [], [], [], {}

    def add_alert(self, id=1, user_id=UID, keyword="chair", state=None, source=None, max_price=None):
        self.alerts.append({"id": id, "user_id": user_id, "keyword": keyword, "state_filter": state,
                            "source_filter": source, "max_price": max_price,
                            "created_at": NOW - timedelta(days=5), "email": f"{user_id[:2]}@x.test"})

    def add_lot(self, n, title="Banquet chairs", state="GA", bid=50.0, ends=1, seen=1, site="govdeals"):
        self.lots.append({"site": site, "native_id": str(n), "title": title, "state": state,
                          "current_bid": bid, "end_utc": NOW + timedelta(days=ends),
                          "first_seen_at": NOW - timedelta(hours=seen), "asset_id": n, "account_id": 9})

    def fetch_all(self, sql, params=None):
        if sql == platform_alerts.ALERTS_SQL:
            return list(self.alerts)
        assert sql == platform_alerts.MATCH_SQL
        created, now, _, pat, st, _, mp, _, src, _, aid = params
        rx = like_to_re(pat)
        floor = max(created, now - timedelta(days=2))
        done = {(a, s, k) for (a, s, k, *_r) in self.sends}
        return [{"source": l["site"], "lot_key": l["native_id"], **{k: l[k] for k in
                 ("title", "current_bid", "end_utc", "state", "asset_id", "account_id")}}
                for l in self.lots
                if l["first_seen_at"] > floor and l["end_utc"] > now and rx.match(l["title"])
                and (st is None or l["state"] == st) and (mp is None or l["current_bid"] <= mp)
                and (src is None or l["site"] == src) and (aid, l["site"], l["native_id"]) not in done]

    def fetch_one(self, sql, params=None):
        assert sql == platform_alerts.SENDS_TODAY_SQL
        uid, start = params
        mine = {a["id"] for a in self.alerts if a["user_id"] == uid}
        return {"n": len({(a, t) for (a, _s, _k, _e, t) in self.sends if a in mine and t >= start})}

    def execute(self, sql, params=None):
        if sql == platform_alerts.INSERT_SEND_SQL:
            a, s, k, e, t = params
            if any(x[:3] == (a, s, k) for x in self.sends):
                return 0
            self.sends.append((a, s, k, e, t)); return 1
        assert sql == platform_alerts.FIRED_SQL
        self.fired[params[1]] = params[0]; return 1


@pytest.fixture
def env(monkeypatch):
    f = FakeDB()
    for n in ("fetch_all", "fetch_one", "execute"):
        monkeypatch.setattr(db, n, getattr(f, n))
    mails = []
    ok = {"v": True}

    def send(to, alert, lots, unsub):
        mails.append((to, alert, lots, unsub)); return ok["v"]
    monkeypatch.setattr(platform_email, "send_alert_email", send)
    monkeypatch.setenv("PLATFORM_BASE_URL", "https://p.test")
    monkeypatch.delenv("PLATFORM_ALERTS_DAILY_CAP", raising=False)
    f.mails, f.ok = mails, ok
    return f


def test_sql_text_key_clauses():
    m = platform_alerts.MATCH_SQL
    assert "ILIKE %s ESCAPE '\\'" in m and "NOT EXISTS" in m and "platform_alert_sends" in m
    assert "interval '2 days'" in m and "end_utc >" in m and "first_seen_at" in m


def test_keyword_match_sends_once(env):
    env.add_alert(); env.add_lot(1); env.add_lot(2, title="Folding CHAIR set"); env.add_lot(3, title="Table")
    r = platform_alerts.run_pass(NOW)
    assert r == {"matched": 2, "emailed": 1, "skipped_dup": 0, "users": 1, "failed": 0}
    to, alert, lots, unsub = env.mails[0]
    assert len(lots) == 2 and lots[0]["url"].startswith("https://www.govdeals.com/en/asset/")
    assert unsub.startswith("https://p.test/platform/unsubscribe?token=")
    assert env.fired[1] == NOW


def test_rerun_no_duplicate(env):
    env.add_alert(); env.add_lot(1)
    platform_alerts.run_pass(NOW)
    r = platform_alerts.run_pass(NOW + timedelta(minutes=15))
    assert r["emailed"] == 0 and r["matched"] == 0 and len(env.mails) == 1


def test_percent_in_keyword_literal(env):
    env.add_alert(keyword="100%"); env.add_alert(id=2, keyword="a_b")
    env.add_lot(1, title="Lot 100% cotton"); env.add_lot(2, title="Lot 1000 units"); env.add_lot(3, title="aXb")
    platform_alerts.run_pass(NOW)
    assert {(s[0], s[2]) for s in env.sends} == {(1, "1")}


def test_state_and_price_filters(env):
    env.add_alert(state="GA", max_price=100, source="govdeals")
    env.add_lot(1); env.add_lot(2, state="TX"); env.add_lot(3, bid=150); env.add_lot(4, site="txauction")
    platform_alerts.run_pass(NOW)
    assert [s[2] for s in env.sends] == ["1"]


def test_daily_cap(env, monkeypatch):
    monkeypatch.setenv("PLATFORM_ALERTS_DAILY_CAP", "1")
    env.add_alert(id=1); env.add_alert(id=2, keyword="banquet"); env.add_lot(1)
    r = platform_alerts.run_pass(NOW)
    assert r["emailed"] == 1 and r["skipped_dup"] == 1 and len(env.mails) == 1
    assert not [s for s in env.sends if s[0] == 2]
    # next UTC day the capped lot goes out
    r2 = platform_alerts.run_pass(NOW + timedelta(hours=13))
    assert r2["emailed"] == 1


def test_closed_lots_ignored(env):
    env.add_alert(); env.add_lot(1, ends=-1); env.add_lot(2, seen=24 * 4)
    r = platform_alerts.run_pass(NOW)
    assert r["matched"] == 0 and not env.mails


def test_email_failure_still_records_send(env):
    env.add_alert(); env.add_lot(1); env.ok["v"] = False
    r = platform_alerts.run_pass(NOW)
    assert r["emailed"] == 0 and r["failed"] == 1 and len(env.sends) == 1
    env.ok["v"] = True
    assert platform_alerts.run_pass(NOW + timedelta(minutes=15))["matched"] == 0


def test_one_alert_raising_does_not_starve_others(env, monkeypatch):
    env.add_alert(id=1, user_id=UID); env.add_alert(id=2, user_id="22" + UID[2:]); env.add_lot(1)
    real = platform_email.make_unsub_token

    def tok(uid):
        if uid == UID:
            raise RuntimeError("no secret")
        return real(uid)
    monkeypatch.setattr(platform_email, "make_unsub_token", tok)
    monkeypatch.setattr(platform_email, "_ser", lambda: __import__("itsdangerous").URLSafeTimedSerializer("k"))
    r = platform_alerts.run_pass(NOW)
    assert r["emailed"] == 1 and not [s for s in env.sends if s[0] == 1]
    assert [s[0] for s in env.sends] == [2]
