import pytest
from datetime import datetime, timezone, timedelta
from deals.models import Lot, Snapshot, lot_key
from deals.watch import poll_once

NOW = datetime(2026,7,3,12,0,tzinfo=timezone.utc)
def _lot(end):
    return Lot(1,2,3,"t","d","372","n","seating_furniture",end,0,10.0,10.0,"USD",0,
        False,False,None,False,"s","c","st","z",None,None,"","STA",False,{})

class FakeAdapter:
    site="govdeals"
    def __init__(self, present): self._present=present   # dict key->Snapshot
    def refetch(self, keys): return self._present

def test_dropped_lot_is_finalized_as_outcome(monkeypatch):
    lot = _lot(NOW-timedelta(minutes=1))                 # past clock
    monkeypatch.setattr("deals.watch.due_for_poll", lambda now: [lot])
    monkeypatch.setattr("deals.watch.append_snapshot", lambda s: None)
    last = Snapshot(1,2,3, NOW-timedelta(minutes=2), 0, 10.0, NOW-timedelta(minutes=1), "STA")
    monkeypatch.setattr("deals.watch.latest_snapshot", lambda k: last)
    recorded=[]
    monkeypatch.setattr("deals.watch.record_outcome",
        lambda k,o,fb,fbc,ca,c: recorded.append((o,fb,fbc,c)))
    rep = poll_once(FakeAdapter(present={}), NOW)        # empty refetch => dropped
    assert recorded == [("no_bid", 10.0, 0, True)]
    assert rep.finalized == 1

def test_live_lot_gets_snapshot_and_reschedule(monkeypatch):
    lot=_lot(NOW+timedelta(minutes=10))
    monkeypatch.setattr("deals.watch.due_for_poll", lambda now:[lot])
    monkeypatch.setattr("deals.watch.latest_snapshot", lambda k: None)
    snaps=[]; sched=[]
    monkeypatch.setattr("deals.watch.append_snapshot", lambda s: snaps.append(s))
    monkeypatch.setattr("deals.watch.record_outcome", lambda *a: (_ for _ in ()).throw(AssertionError("should not finalize")))
    monkeypatch.setattr("deals.watch.update_live_state", lambda k, s, t, ln: sched.append(ln))
    present={lot_key(1,2,3): Snapshot(1,2,3,NOW,1,12.0,NOW+timedelta(minutes=13),"STA")}  # bid + extension
    rep=poll_once(FakeAdapter(present), NOW)
    assert len(snaps)==1 and rep.snapshotted==1
    assert sched==["hot"]                                # 13 min out -> hot lane

def test_absent_cold_lot_requeued_not_finalized(monkeypatch):
    lot = _lot(NOW + timedelta(days=2))            # cold, far from close
    monkeypatch.setattr("deals.watch.due_for_poll", lambda now: [lot])
    monkeypatch.setattr("deals.watch.latest_snapshot", lambda k: None)
    monkeypatch.setattr("deals.watch.append_snapshot", lambda s: None)
    monkeypatch.setattr("deals.watch.record_outcome",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not finalize a cold lot")))
    sched = []
    monkeypatch.setattr("deals.watch.set_poll_schedule", lambda k, t, ln: sched.append(ln))
    rep = poll_once(FakeAdapter(present={}), NOW)   # absent from refetch
    assert rep.finalized == 0 and rep.requeued == 1 and sched == ["cold"]


class _TxAdapter:
    site = "txauction"
    def __init__(self, present): self._present, self.remembered = present, []
    def remember(self, lots): self.remembered += lots
    def refetch(self, keys): return self._present


@pytest.mark.parametrize("status,outcome", [("CLO", "sold"), ("RNM", "reserve_not_met")])
def test_foreign_closed_snapshot_is_finalized(monkeypatch, status, outcome):
    lot = _lot(NOW - timedelta(hours=2))
    calls = {}
    monkeypatch.setattr("deals.watch.due_for_poll",
                        lambda now, extra_where=None, site="govdeals": calls.update(site=site) or [lot])
    snaps = []
    monkeypatch.setattr("deals.watch.append_snapshot", lambda s, site="govdeals": snaps.append(site))
    recorded = []
    monkeypatch.setattr("deals.watch.record_outcome",
                        lambda k, o, fb, fbc, ca, c: recorded.append((o, fb, fbc, c)))
    monkeypatch.setattr("deals.watch.update_live_state",
                        lambda *a: (_ for _ in ()).throw(AssertionError("closed lot must not be rescheduled")))
    final = Snapshot(1, 2, 3, NOW, 682, 5645.0, NOW - timedelta(hours=2), status)
    ad = _TxAdapter({lot_key(1, 2, 3): final})
    rep = poll_once(ad, NOW, site="txauction")
    assert calls["site"] == "txauction" and ad.remembered == [lot] and snaps == ["txauction"]
    assert recorded == [(outcome, 5645.0, 682, True)] and rep.finalized == 1
