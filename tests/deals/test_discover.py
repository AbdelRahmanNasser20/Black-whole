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
