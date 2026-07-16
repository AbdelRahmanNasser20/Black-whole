"""New-inventory alert BLAST job (BLACKWHOLE-10).

All DB-free: the matcher is pure Python, and blast tests inject `lot`,
`subscribers`, and `already_sent` so no database or network is touched. The
overarching contract under test is SEND-DISABLED-BY-DEFAULT: with no config the
job emails nothing and writes nothing.
"""
from __future__ import annotations

import logging

import pytest

from automation.alerts import blast as blast_mod
from automation.alerts import email_sender as es
from automation.alerts.geo import haversine_miles, resolve_latlon
from automation.alerts.matcher import match_lot


# ───────── geo ─────────

def test_haversine_atlanta_to_nyc_roughly_750_miles():
    # ATL (33.75,-84.39) → NYC (40.71,-74.01) ≈ 748 mi
    d = haversine_miles(33.75, -84.39, 40.71, -74.01)
    assert 700 < d < 800


def test_resolve_latlon_state_centroid_fallback():
    lat, lon, prec = resolve_latlon(None, "GA")
    assert prec == "state"
    assert 30 < lat < 35 and -86 < lon < -82


def test_resolve_latlon_none_when_unknown():
    assert resolve_latlon(None, None) == (None, None, None)
    assert resolve_latlon(None, "ZZ") == (None, None, None)


# ───────── matcher ─────────

GA_LOT = {
    "lot_id": "L1", "title": "200 Black Banquet Chairs", "chair_type": "banquet",
    "city": "Atlanta", "state": "GA", "zip_code": "30301",
    "quantity_remaining": 200, "price_per_chair": 8,
}


def _sub(**kw):
    base = {"id": 1, "status": "new", "email": "a@b.com", "state": "GA"}
    base.update(kw)
    return base


def test_wildcard_chair_type_matches():
    subs = [_sub(id=1, chair_type=None), _sub(id=2, chair_type="any")]
    matches, _ = match_lot(GA_LOT, subs)
    assert {m.subscriber_id for m in matches} == {1, 2}


def test_chair_type_mismatch_skipped():
    subs = [_sub(id=1, chair_type="chiavari")]
    matches, skips = match_lot(GA_LOT, subs)
    assert matches == []
    assert skips[0].reason == "chair_type"


def test_same_state_passes_by_centroid():
    subs = [_sub(id=1, chair_type="banquet", state="GA")]
    matches, _ = match_lot(GA_LOT, subs, default_radius_miles=100)
    assert [m.subscriber_id for m in matches] == [1]


def test_far_state_out_of_radius_skipped():
    subs = [_sub(id=1, chair_type="banquet", state="WA", zip_code=None)]
    matches, skips = match_lot(GA_LOT, subs, default_radius_miles=100)
    assert matches == []
    assert skips[0].reason == "out_of_radius"


def test_radius_zero_is_anywhere():
    subs = [_sub(id=1, chair_type="banquet", state="WA", radius_miles=0)]
    matches, _ = match_lot(GA_LOT, subs)
    assert [m.subscriber_id for m in matches] == [1]
    assert matches[0].match_reason["geo_precision"] == "anywhere"


def test_quantity_floor_skips_undersupplied_lot():
    # Wants 800 but lot only has 200 → skip (can't fill the order).
    subs = [_sub(id=1, chair_type="banquet", quantity_wanted=800, radius_miles=0)]
    matches, skips = match_lot(GA_LOT, subs)
    assert matches == []
    assert skips[0].reason == "quantity"


def test_phone_only_subscriber_skipped_for_email_blast():
    subs = [_sub(id=1, email=None, phone="+14045551212", radius_miles=0)]
    matches, skips = match_lot(GA_LOT, subs)
    assert matches == []
    assert skips[0].reason == "no_email"


def test_unsubscribed_never_matches():
    subs = [_sub(id=1, status="unsubscribed", radius_miles=0)]
    matches, skips = match_lot(GA_LOT, subs)
    assert matches == []
    assert skips[0].reason == "not_active"


# ───────── email sender interface ─────────

def test_dry_run_sender_records_and_never_fails():
    sender = es.DryRunEmailSender()
    r = sender.send(es.EmailMessage(to="x@y.com", subject="hi", html="<p>", text="hi"))
    assert r.ok and r.provider_message_id is None
    assert len(sender.sent) == 1


def test_build_email_sender_defaults_to_dry_run():
    assert es.build_email_sender(provider="", send_enabled=False).name == "dry_run"
    # Even with a provider named, disabled send stays dry-run.
    assert es.build_email_sender(provider="resend", send_enabled=False).name == "dry_run"


def test_build_email_sender_unknown_provider_raises_when_enabled():
    with pytest.raises(RuntimeError, match="not registered"):
        es.build_email_sender(provider="resend", send_enabled=True)


def test_register_provider_then_build():
    class Fake(es.EmailSender):
        name = "fake"

        def send(self, message):
            return es.SendResult(ok=True, provider_message_id="fake-1")

    es.register_provider("fake", Fake)
    try:
        s = es.build_email_sender(provider="fake", send_enabled=True)
        assert s.name == "fake"
        assert s.send(es.EmailMessage(to="a", subject="b", html="c", text="d")).ok
    finally:
        es._PROVIDERS.pop("fake", None)


# ───────── compose ─────────

def test_compose_email_has_unsubscribe_and_listing():
    sub = _sub(id=5, name="Jane", email="jane@x.com",
               unsubscribe_token="tok-abc-1234567890")
    msg = blast_mod.compose_email(sub, GA_LOT)
    assert "jane@x.com" == msg.to
    assert "L1" in msg.text  # listing url carries lot_id
    assert "tok-abc-1234567890" in msg.text
    assert msg.headers.get("List-Unsubscribe") == "<https://black-whole.com/alerts/unsubscribe?token=tok-abc-1234567890>"
    assert msg.headers.get("List-Unsubscribe-Post") == "List-Unsubscribe=One-Click"


def test_compose_email_notes_missing_token():
    sub = _sub(id=6, email="j@x.com")  # no unsubscribe_token
    msg = blast_mod.compose_email(sub, GA_LOT)
    assert "token pending" in msg.text
    assert "List-Unsubscribe" not in msg.headers


# ───────── blast orchestration (injected, DB-free) ─────────

def test_run_blast_dry_run_sends_nothing_writes_nothing(monkeypatch):
    # Any DB access must blow up — proving dry-run touches no DB.
    def boom(*a, **k):
        raise AssertionError("DB accessed during dry-run")
    monkeypatch.setattr(blast_mod.db, "fetch_one", boom)
    monkeypatch.setattr(blast_mod.db, "fetch_all", boom)
    monkeypatch.setattr(blast_mod.db, "execute", boom)

    subs = [
        _sub(id=1, chair_type="banquet", radius_miles=0, email="a@x.com"),
        _sub(id=2, chair_type="chiavari", radius_miles=0, email="b@x.com"),  # skip
    ]
    report = blast_mod.run_blast(
        "L1", lot=GA_LOT, subscribers=subs, already_sent=set(),
    )
    assert report.dry_run is True
    assert report.provider == "dry_run"
    assert report.matched == 1
    assert report.sent == 0  # dry-run never increments real sends
    assert any("DRY-RUN" in n for n in report.notes)


def test_run_blast_dedupe_suppresses_prior_send():
    subs = [_sub(id=1, chair_type="banquet", radius_miles=0, email="a@x.com")]
    report = blast_mod.run_blast(
        "L1", lot=GA_LOT, subscribers=subs, already_sent={(1, "email")},
    )
    assert report.suppressed_dedupe == 1
    assert report.recipients[0].would_suppress_dedupe is True


def test_preview_blast_reports_reasons():
    subs = [
        _sub(id=1, chair_type="banquet", radius_miles=0, email="a@x.com"),
        _sub(id=2, chair_type="folding", radius_miles=0, email="b@x.com"),
    ]
    report = blast_mod.preview_blast("L1", lot=GA_LOT, subscribers=subs, already_sent=set())
    assert report.dry_run is True
    assert report.matched == 1
    reasons = {s["subscriber_id"]: s["reason"] for s in report.skips}
    assert reasons[2] == "chair_type"


def test_run_blast_live_path_sends_and_records(monkeypatch):
    """With an injected real sender + send_enabled, it sends and records a row."""
    recorded = []
    monkeypatch.setattr(blast_mod, "_record_send",
                        lambda *a, **k: recorded.append(a))

    class OK(es.EmailSender):
        name = "fake"

        def send(self, message):
            return es.SendResult(ok=True, provider_message_id="m-1")

    subs = [_sub(id=1, chair_type="banquet", radius_miles=0, email="a@x.com")]
    report = blast_mod.run_blast(
        "L1", lot=GA_LOT, subscribers=subs, already_sent=set(),
        send_enabled=True, sender=OK(),
    )
    assert report.dry_run is False
    assert report.sent == 1
    assert report.recipients[0].provider_message_id == "m-1"
    assert len(recorded) == 1


def test_run_blast_daily_cap(monkeypatch):
    subs = [
        _sub(id=i, chair_type="banquet", radius_miles=0, email=f"{i}@x.com")
        for i in range(1, 4)
    ]

    class OK(es.EmailSender):
        name = "fake"

        def send(self, message):
            return es.SendResult(ok=True, provider_message_id="m")

    monkeypatch.setattr(blast_mod, "_record_send", lambda *a, **k: None)
    report = blast_mod.run_blast(
        "L1", lot=GA_LOT, subscribers=subs, already_sent=set(),
        send_enabled=True, sender=OK(), max_per_day=2,
    )
    assert report.sent == 2
    assert report.capped == 1


def test_run_blast_missing_lot_is_graceful(monkeypatch):
    monkeypatch.setattr(blast_mod, "load_lot", lambda lot_id: None)
    report = blast_mod.run_blast("NOPE", subscribers=[], already_sent=set())
    assert report.matched == 0
    assert any("not found" in n for n in report.notes)


def test_geo_reason_included_in_match():
    subs = [_sub(id=1, chair_type="banquet", zip_code="30302", radius_miles=200)]
    matches, _ = match_lot(GA_LOT, subs)
    assert matches and "geo_precision" in matches[0].match_reason


# ───────── admin API routes (blast job stubbed, no DB) ─────────

import importlib  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

web_app = importlib.import_module("automation.web.app")


def _fake_report(found=True):
    r = blast_mod.BlastReport(lot_id="L1", blast_id="b1", dry_run=True,
                              provider="dry_run")
    if not found:
        r.notes.append("lot L1 not found")
    else:
        r.matched = 2
    return r


def test_route_blast_preview_ok(monkeypatch):
    monkeypatch.setattr(web_app.alerts_blast, "preview_blast",
                        lambda lot_id: _fake_report())
    tc = TestClient(web_app.app)
    r = tc.post("/api/alerts/blast/L1/preview")
    assert r.status_code == 200
    assert r.json()["dry_run"] is True and r.json()["matched"] == 2


def test_route_blast_run_dry_run_default(monkeypatch):
    captured = {}

    def fake_run(lot_id):
        captured["lot"] = lot_id
        return _fake_report()

    monkeypatch.setattr(web_app.alerts_blast, "run_blast", fake_run)
    tc = TestClient(web_app.app)
    r = tc.post("/api/alerts/blast/L1")
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert captured["lot"] == "L1"


def test_route_blast_missing_lot_404(monkeypatch):
    monkeypatch.setattr(web_app.alerts_blast, "preview_blast",
                        lambda lot_id: _fake_report(found=False))
    tc = TestClient(web_app.app)
    assert tc.post("/api/alerts/blast/L1/preview").status_code == 404
