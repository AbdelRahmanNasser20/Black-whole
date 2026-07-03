from datetime import datetime, timezone
from deals.discover import run_discovery
from deals.models import Lot

def _lot(cat="372", canon="seating_furniture", bids=0):
    return Lot(1,2,3,"9 chairs","d",cat,"n",canon,datetime(2026,7,3,13,tzinfo=timezone.utc),
        bids,10.0,10.0,"USD",0,False,False,None,False,"s","c","st","z",None,None,"",("STA"),False,{})

class FakeAdapter:
    site="govdeals"
    def __init__(self, lots): self._lots=lots
    def discover(self, **kw): return iter(self._lots)
    def fetch_gallery(self, a, ac): return []

def test_discovery_upserts_classifies_and_schedules(monkeypatch):
    ups=[]; sched=[]
    monkeypatch.setattr("deals.discover.upsert_lot", lambda l: ups.append(l))
    monkeypatch.setattr("deals.discover.set_poll_schedule", lambda k,t,ln: sched.append((k,ln)))
    monkeypatch.setattr("deals.discover.apply_classification", lambda l, **k: l)
    monkeypatch.setattr("deals.discover.archive_lot_images", lambda l,g: [])
    rep = run_discovery(FakeAdapter([_lot()]), categories=["372"],
                        now=datetime(2026,7,3,12,tzinfo=timezone.utc))
    assert rep.discovered == 1 and rep.upserted == 1
    assert len(sched) == 1                          # every lot gets a poll schedule

def test_classify_skipped_for_known_category(monkeypatch):
    calls = []
    monkeypatch.setattr("deals.discover.upsert_lot", lambda l: None)
    monkeypatch.setattr("deals.discover.set_poll_schedule", lambda k, t, ln: None)
    monkeypatch.setattr("deals.discover.apply_classification", lambda l, **k: calls.append(l) or l)
    monkeypatch.setattr("deals.discover.archive_lot_images", lambda l, g: [])
    rep = run_discovery(FakeAdapter([_lot(cat="94A", canon="vehicles")]), categories=["94A"],
                        now=datetime(2026, 7, 3, 12, tzinfo=timezone.utc))
    assert calls == [] and rep.classified == 0        # known category -> no LLM call

def test_archive_only_for_zero_bid_seating(monkeypatch):
    arch = []
    monkeypatch.setattr("deals.discover.upsert_lot", lambda l: None)
    monkeypatch.setattr("deals.discover.set_poll_schedule", lambda k, t, ln: None)
    monkeypatch.setattr("deals.discover.apply_classification", lambda l, **k: l)
    monkeypatch.setattr("deals.discover.archive_lot_images", lambda l, g: arch.append(l) or [])
    lots = [_lot(bids=1),                                   # seating but has a bid -> skip
            _lot(cat="266", canon="general_merchandise"),  # 0-bid but not seating -> skip
            _lot()]                                         # 0-bid seating -> archive
    rep = run_discovery(FakeAdapter(lots), categories=["x"],
                        now=datetime(2026, 7, 3, 12, tzinfo=timezone.utc))
    assert rep.archived == 1 and len(arch) == 1

def test_per_lot_error_is_isolated(monkeypatch):
    seen = []
    def bad_upsert(l):
        if l.asset_id == 99:
            raise RuntimeError("boom")
        seen.append(l)
    monkeypatch.setattr("deals.discover.upsert_lot", bad_upsert)
    monkeypatch.setattr("deals.discover.set_poll_schedule", lambda k, t, ln: None)
    monkeypatch.setattr("deals.discover.apply_classification", lambda l, **k: l)
    monkeypatch.setattr("deals.discover.archive_lot_images", lambda l, g: [])
    a = _lot(); a.asset_id = 1
    b = _lot(); b.asset_id = 99
    c = _lot(); c.asset_id = 2
    rep = run_discovery(FakeAdapter([a, b, c]), categories=["x"],
                        now=datetime(2026, 7, 3, 12, tzinfo=timezone.utc))
    assert rep.errors == 1 and {l.asset_id for l in seen} == {1, 2}
