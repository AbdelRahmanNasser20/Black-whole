"""assess_health classifies a source as OK / STALE / DEGRADED. Pure — no DB,
no clock. Run:
  .venv/bin/python -m pytest tests/test_discovery_health_check.py -v
"""
from datetime import datetime, timedelta, timezone

from scripts.discovery_health_check import assess_health

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _h(**kw):
    base = dict(source_label="Public Surplus", newest=NOW, recent_total=100,
                recent_null=0, now=NOW, stale_hours=30, degraded_frac=0.5)
    base.update(kw)
    return assess_health(base.pop("source_label"), base.pop("newest"),
                         base.pop("recent_total"), base.pop("recent_null"),
                         base.pop("now"), stale_hours=base["stale_hours"],
                         degraded_frac=base["degraded_frac"])


def test_fresh_and_populated_is_ok():
    assert _h()["status"] == "OK"


def test_no_rows_is_stale():
    assert _h(newest=None)["status"] == "STALE"


def test_old_newest_is_stale():
    assert _h(newest=NOW - timedelta(hours=48))["status"] == "STALE"


def test_within_window_is_not_stale():
    assert _h(newest=NOW - timedelta(hours=20))["status"] == "OK"


def test_all_null_recent_is_degraded():
    r = _h(recent_total=248, recent_null=248)
    assert r["status"] == "DEGRADED"
    assert "100%" in r["detail"]


def test_below_threshold_is_ok():
    # 40% null < 50% threshold
    assert _h(recent_total=100, recent_null=40)["status"] == "OK"


def test_stale_wins_over_degraded():
    # old AND all-null -> reported as STALE (the more fundamental failure)
    assert _h(newest=NOW - timedelta(hours=48),
              recent_total=0, recent_null=0)["status"] == "STALE"
