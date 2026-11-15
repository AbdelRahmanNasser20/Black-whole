"""Alert matcher, second half: lots from snapshot-only sources, and searches with the 037 filters."""
from datetime import datetime, timedelta, timezone

import pytest

from automation import db, platform_alerts, platform_email
from automation.web import deals_sources, public_deals

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
UID = "11111111-1111-1111-1111-111111111111"


def snap(n, title="Forklift", source="gsa", state="GA", bid=500.0, seen_h=1, ends_d=2, bids=1, cat=None):
    return {"id": f"{source}:{n}", "source": source, "source_lot_id": str(n), "title": title,
            "canonical_category": cat, "state": state, "current_bid": bid, "bid_count": bids,
            "end_utc": NOW + timedelta(days=ends_d), "first_seen_at": NOW - timedelta(hours=seen_h),
            "lat": None, "lng": None, "asset_id": None, "account_id": None, "auction_id": None,
            "url": f"https://example.test/{source}/{n}"}


class FakeDB:
    def __init__(self):
        self.alerts, self.sends, self.deal_rows, self.queries = [], [], [], []

    def alert(self, id=1, keyword="forklift", **extra):
        self.alerts.append({"id": id, "user_id": UID, "keyword": keyword, "state_filter": None,
                            "source_filter": None, "max_price": None, "active": True,
                            "created_at": NOW - timedelta(days=5), "email": "me@x.test", **extra})

    def fetch_all(self, sql, params=None):
        self.queries.append(sql)
        if sql == platform_alerts.ALERTS_SQL:
            return list(self.alerts)
        if sql == platform_alerts.MATCH_SQL:
            return list(self.deal_rows)
        if sql == platform_alerts.SENT_SQL:
            return [{"source": s, "lot_key": k} for (a, s, k, *_r) in self.sends if a == params[0]]
        raise AssertionError(sql)

    def fetch_one(self, sql, params=None):
        assert sql == platform_alerts.SENDS_TODAY_SQL
        return {"n": len({(a, t) for (a, _s, _k, _e, t) in self.sends if t >= params[1]})}

    def execute(self, sql, params=None):
        if sql == platform_alerts.INSERT_SEND_SQL:
            a, s, k, e, t = params
            if any(x[:3] == (a, s, k) for x in self.sends):
                return 0
            self.sends.append((a, s, k, e, t)); return 1
        assert sql == platform_alerts.FIRED_SQL
        return 1


@pytest.fixture
def env(monkeypatch):
    f = FakeDB()
    for n in ("fetch_all", "fetch_one", "execute"):
        monkeypatch.setattr(db, n, getattr(f, n))
    f.mails, f.snaps, f.order = [], [], []

    def send(to, alert, lots, unsub):
        f.order.append(("mail", len(f.sends)))
        f.mails.append({"to": to, "alert": alert, "lots": lots})
        return True

    monkeypatch.setattr(platform_email, "send_alert_email", send)
    monkeypatch.setattr(platform_email, "make_unsub_token", lambda uid: "tok")
    monkeypatch.setattr(deals_sources, "lots", lambda status="active", **k: list(f.snaps))
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setenv("PLATFORM_BASE_URL", "https://p.test")
    return f


def test_keyword_alert_also_matches_snapshot_only_sources(env):
    env.alert()
    env.snaps = [snap(1), snap(2, title="Office desks"), snap(3, title="Old forklift", seen_h=72)]
    rep = platform_alerts.run_pass(NOW)
    assert rep["emailed"] == 1 and rep["matched"] == 1
    assert [(s, k) for (_a, s, k, *_r) in env.sends] == [("gsa", "1")]
    lot = env.mails[0]["lots"][0]
    assert lot["title"] == "Forklift" and lot["url"] == "https://example.test/gsa/1"
    assert env.order == [("mail", 1)]                       # the send row is in before the email goes out


def test_a_snapshot_lot_is_emailed_once(env):
    env.alert()
    env.snaps = [snap(1)]
    platform_alerts.run_pass(NOW)
    rep = platform_alerts.run_pass(NOW + timedelta(minutes=15))
    assert len(env.mails) == 1 and rep["emailed"] == 0 and len(env.sends) == 1


def test_state_source_and_price_filters_apply_to_the_snapshot_half(env):
    env.alert(state_filter="GA", source_filter="gsa", max_price=600.0)
    env.snaps = [snap(1), snap(2, state="FL"), snap(3, source="municibid"), snap(4, bid=900.0)]
    platform_alerts.run_pass(NOW)
    assert [k for (_a, _s, k, *_r) in env.sends] == ["1"]


def test_snapshot_read_failure_does_not_stop_the_deal_lots_half(env, monkeypatch):
    env.alert()
    env.deal_rows = [{"source": "govdeals", "lot_key": "1/2/3", "title": "GD forklift", "current_bid": 10.0,
                      "end_utc": NOW + timedelta(days=1), "state": "GA", "asset_id": 1, "account_id": 2}]
    def boom(*a, **k):
        raise RuntimeError("snapshot down")
    monkeypatch.setattr(deals_sources, "lots", boom)
    rep = platform_alerts.run_pass(NOW)
    assert rep["emailed"] == 1 and [k for (_a, _s, k, *_r) in env.sends] == ["1/2/3"]


def test_a_search_with_the_new_filters_reads_the_public_feed(env, monkeypatch):
    env.alert(keyword=None, name="GA vehicles", category="vehicles", state_filter="GA", max_bids=3,
              min_price=100.0, ending_within=48)
    asked = {}

    def fetch_page(**kw):
        asked.update(kw)
        return {"rows": [
            dict(snap(7, title="Truck"), source_name="GSA"),
            {"source": "govdeals", "asset_id": 1, "account_id": 2, "auction_id": 3, "title": "Van",
             "current_bid": 300.0, "end_utc": NOW + timedelta(days=1), "state": "GA",
             "first_seen_at": NOW - timedelta(hours=3), "govdeals_url": "https://www.govdeals.com/en/asset/1/2"},
            dict(snap(8, title="Stale bus", seen_h=100)),
        ], "total": 3}

    monkeypatch.setattr(public_deals, "fetch_page", fetch_page)
    rep = platform_alerts.run_pass(NOW)
    assert asked == {"q": None, "category": "vehicles", "state": "GA", "site": None, "max_bids": 3,
                     "ending_within": 48, "min_price": 100.0, "max_price": None, "status": "active",
                     "sort": "newest", "page": 1, "per_page": 100}
    assert platform_alerts.MATCH_SQL not in env.queries          # one reader for this kind of search
    assert sorted((s, k) for (_a, s, k, *_r) in env.sends) == [("govdeals", "1/2/3"), ("gsa", "7")]
    assert rep["emailed"] == 1 and env.mails[0]["alert"]["keyword"] == "GA vehicles"
    assert {l["url"] for l in env.mails[0]["lots"]} == {"https://example.test/gsa/7",
                                                       "https://www.govdeals.com/en/asset/1/2"}


def test_a_row_with_no_filter_at_all_never_matches_everything(env, monkeypatch):
    env.alert(keyword=None)
    monkeypatch.setattr(public_deals, "fetch_page", lambda **k: pytest.fail("must not read the feed"))
    env.snaps = [snap(1)]
    assert platform_alerts.run_pass(NOW)["matched"] == 0 and env.sends == []


def test_daily_cap_and_no_key_still_hold_for_snapshot_lots(env, monkeypatch):
    env.alert()
    env.snaps = [snap(1)]
    monkeypatch.setenv("PLATFORM_ALERTS_DAILY_CAP", "0")
    assert platform_alerts.run_pass(NOW)["emailed"] == 0 and env.sends == []
    monkeypatch.setenv("PLATFORM_ALERTS_DAILY_CAP", "20")
    monkeypatch.delenv("RESEND_API_KEY")
    assert platform_alerts.run_pass(NOW)["emailed"] == 0 and env.sends == []


def test_alerts_sql_reads_whole_rows_so_it_works_before_and_after_037():
    assert "a.*" in platform_alerts.ALERTS_SQL
    for col in ("category", "min_price", "max_bids", "ending_within"):
        assert col not in platform_alerts.ALERTS_SQL
