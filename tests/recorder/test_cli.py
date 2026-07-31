"""Offline tests for recorder/cli.py — the orchestration layer.

No network, no DB: every test monkeypatches `cli.store`, `cli.db`, and/or
`cli.build_registry` with fakes. `_check_schema` is monkeypatched to a no-op
in every test that exercises `main()` so we never need a real connection.
"""
from datetime import datetime, timedelta, timezone

import pytest

from recorder import cli
from recorder.models import Observation

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


class FakeSource:
    """Minimal RecorderSource stand-in. Each method either returns a fixed
    list of Observations or raises, per the flags passed in."""

    def __init__(self, name, discover_obs=None, poll_obs=None, sold_obs=None,
                 fail_discover=False, fail_poll=False, fail_sold=False):
        self.SOURCE = name
        self._discover_obs = discover_obs or []
        self._poll_obs = poll_obs or []
        self._sold_obs = sold_obs or []
        self._fail_discover = fail_discover
        self._fail_poll = fail_poll
        self._fail_sold = fail_sold
        self.discover_calls = 0
        self.poll_calls = 0
        self.sold_sweep_calls = 0

    def discover(self):
        self.discover_calls += 1
        if self._fail_discover:
            raise RuntimeError("discover boom")
        return list(self._discover_obs)

    def poll(self, lots):
        self.poll_calls += 1
        if self._fail_poll:
            raise RuntimeError("poll boom")
        return list(self._poll_obs)

    def sold_sweep(self):
        self.sold_sweep_calls += 1
        if self._fail_sold:
            raise RuntimeError("sold_sweep boom")
        return list(self._sold_obs)


def _obs(status="active", source="govdeals"):
    return Observation(source=source, source_lot_id="1", status=status, raw={})


# --- registry completeness --------------------------------------------------

def test_registry_has_all_six_sources():
    registry = cli.build_registry()
    assert set(registry.keys()) == set(cli.SOURCE_NAMES)
    assert set(cli.SOURCE_NAMES) == {
        "govdeals", "public_surplus", "purple_wave", "municibid", "mibid", "gsa",
    }


def test_registry_adapter_source_attr_matches_its_registry_key():
    registry = cli.build_registry()
    for name, adapter in registry.items():
        assert adapter.SOURCE == name


def test_registry_builds_fresh_instances_each_call():
    a = cli.build_registry()
    b = cli.build_registry()
    for name in cli.SOURCE_NAMES:
        assert a[name] is not b[name]


# --- cmd_discover: per-source error isolation -------------------------------

def test_discover_runs_every_source_even_when_one_fails(monkeypatch):
    inserted = []
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: inserted.append(list(obs)) or len(obs))

    good_a = FakeSource("a", discover_obs=[_obs()])
    bad = FakeSource("b", fail_discover=True)
    good_c = FakeSource("c", discover_obs=[_obs()])
    registry = {"a": good_a, "b": bad, "c": good_c}

    rc = cli.cmd_discover(registry)

    assert good_a.discover_calls == 1
    assert bad.discover_calls == 1
    assert good_c.discover_calls == 1
    # bad source's failure must not stop the others
    assert good_a.sold_sweep_calls == 1
    assert good_c.sold_sweep_calls == 1
    assert rc == 1  # nonzero because one source failed


def test_discover_returns_zero_when_all_sources_succeed(monkeypatch):
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: len(list(obs)))
    registry = {"a": FakeSource("a"), "b": FakeSource("b")}
    assert cli.cmd_discover(registry) == 0


def test_discover_source_filter_only_runs_that_source(monkeypatch):
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: len(list(obs)))
    a = FakeSource("a")
    b = FakeSource("b")
    registry = {"a": a, "b": b}
    cli.cmd_discover(registry, source="a")
    assert a.discover_calls == 1
    assert b.discover_calls == 0


def test_discover_unknown_source_name_is_a_loud_failure_not_a_crash(monkeypatch):
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: len(list(obs)))
    rc = cli.cmd_discover({}, source="nonexistent")
    assert rc == 1


# --- cmd_poll_once: due filtering + grouping + error isolation -------------

def test_poll_once_groups_due_rows_by_source_and_isolates_failures(monkeypatch):
    tracked_rows = [
        {"source": "a", "source_lot_id": "1", "observed_at": NOW - timedelta(hours=10),
         "end_date": None, "current_bid": None, "bid_count": None},
        {"source": "b", "source_lot_id": "2", "observed_at": NOW - timedelta(minutes=1),
         "end_date": None, "current_bid": None, "bid_count": None},  # not due (far interval = 6h)
        {"source": "c", "source_lot_id": "3", "observed_at": NOW - timedelta(hours=10),
         "end_date": None, "current_bid": None, "bid_count": None},
    ]
    monkeypatch.setattr(cli.store, "tracked_active", lambda: tracked_rows)
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: len(list(obs)))

    a = FakeSource("a", poll_obs=[_obs(status="closed")])
    b = FakeSource("b", poll_obs=[_obs()])
    c = FakeSource("c", fail_poll=True)
    registry = {"a": a, "b": b, "c": c}

    rc = cli.cmd_poll_once(registry, now=NOW)

    assert a.poll_calls == 1     # was due
    assert b.poll_calls == 0     # not due, never called
    assert c.poll_calls == 1     # was due, raised
    assert rc == 1


def test_poll_once_returns_zero_when_nothing_due(monkeypatch):
    monkeypatch.setattr(cli.store, "tracked_active", lambda: [])
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: len(list(obs)))
    assert cli.cmd_poll_once({}, now=NOW) == 0


def test_poll_once_counts_gone_and_closed_statuses(monkeypatch, capsys):
    tracked_rows = [
        {"source": "a", "source_lot_id": "1", "observed_at": NOW - timedelta(hours=10),
         "end_date": None, "current_bid": None, "bid_count": None},
    ]
    monkeypatch.setattr(cli.store, "tracked_active", lambda: tracked_rows)
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: len(list(obs)))

    a = FakeSource("a", poll_obs=[_obs(status="gone"), _obs(status="closed"), _obs(status="active")])
    rc = cli.cmd_poll_once({"a": a}, now=NOW)

    out = capsys.readouterr().out
    assert rc == 0
    assert "gone=1" in out
    assert "closed=1" in out
    assert "inserted=3" in out


# --- cmd_coverage ------------------------------------------------------------

def test_coverage_prints_table(monkeypatch, capsys):
    rows = [
        {"source": "govdeals", "closed_lots": 10, "covered": 9, "missed": 1, "pct": 90.0},
        {"source": "_all", "closed_lots": 10, "covered": 9, "missed": 1, "pct": 90.0},
    ]
    monkeypatch.setattr(cli.store, "coverage", lambda days: rows)
    rc = cli.cmd_coverage(7)
    out = capsys.readouterr().out
    assert rc == 0
    assert "govdeals" in out
    assert "90.0" in out


def test_coverage_handles_empty_rows(monkeypatch, capsys):
    monkeypatch.setattr(cli.store, "coverage", lambda days: [])
    rc = cli.cmd_coverage(7)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no coverage data" in out.lower()


# --- cmd_run: conditional discover ------------------------------------------

def test_run_discovers_only_stale_or_never_discovered_sources(monkeypatch):
    monkeypatch.setattr(cli.store, "tracked_active", lambda: [])
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: len(list(obs)))

    newest_by_source = {
        "fresh": NOW - timedelta(hours=1),      # well inside the 6h threshold
        "stale": NOW - timedelta(hours=7),      # older than the 6h threshold
        "never": None,                          # never discovered
    }
    monkeypatch.setattr(cli.store, "newest_observed_at", lambda source: newest_by_source[source])

    fresh = FakeSource("fresh")
    stale = FakeSource("stale")
    never = FakeSource("never")
    registry = {"fresh": fresh, "stale": stale, "never": never}

    rc = cli.cmd_run(registry, discover_stale_hours=6, now=NOW)

    assert fresh.discover_calls == 0
    assert stale.discover_calls == 1
    assert never.discover_calls == 1
    assert rc == 0


def test_run_exit_code_reflects_poll_and_discover_failures(monkeypatch):
    monkeypatch.setattr(cli.store, "tracked_active", lambda: [])
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: len(list(obs)))
    monkeypatch.setattr(cli.store, "newest_observed_at", lambda source: None)

    failing = FakeSource("failing", fail_discover=True)
    rc = cli.cmd_run({"failing": failing}, discover_stale_hours=6, now=NOW)
    assert rc == 1


def test_run_skips_discover_entirely_when_nothing_is_stale(monkeypatch):
    monkeypatch.setattr(cli.store, "tracked_active", lambda: [])
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: len(list(obs)))
    monkeypatch.setattr(cli.store, "newest_observed_at", lambda source: NOW)

    fresh = FakeSource("fresh")
    rc = cli.cmd_run({"fresh": fresh}, discover_stale_hours=6, now=NOW)

    assert fresh.discover_calls == 0
    assert rc == 0


# --- main(): argparse wiring + schema guard placement -----------------------

def test_main_help_never_touches_db(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("DB must not be touched for --help")
    monkeypatch.setattr(cli.db, "fetch_one", _boom)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])
    assert exc_info.value.code == 0


def test_main_exits_3_when_schema_missing(monkeypatch):
    monkeypatch.setattr(cli.db, "fetch_one", lambda *a, **k: {"reg": None})
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["coverage"])
    assert exc_info.value.code == 3


def test_main_dispatches_coverage_when_schema_present(monkeypatch, capsys):
    monkeypatch.setattr(cli.db, "fetch_one", lambda *a, **k: {"reg": "listing_snapshots"})
    monkeypatch.setattr(cli.store, "coverage", lambda days: [])
    rc = cli.main(["coverage", "--days", "3"])
    assert rc == 0
    assert "no coverage data" in capsys.readouterr().out.lower()


def test_main_dispatches_discover_with_source_flag(monkeypatch):
    monkeypatch.setattr(cli.db, "fetch_one", lambda *a, **k: {"reg": "listing_snapshots"})

    called = {}

    def fake_build_registry():
        called["built"] = True
        return {"mibid": FakeSource("mibid")}

    monkeypatch.setattr(cli, "build_registry", fake_build_registry)
    monkeypatch.setattr(cli.store, "insert_observations", lambda obs: len(list(obs)))

    rc = cli.main(["discover", "--source", "mibid"])
    assert rc == 0
    assert called.get("built") is True


def test_main_rejects_unknown_source_choice(monkeypatch):
    monkeypatch.setattr(cli.db, "fetch_one", lambda *a, **k: {"reg": "listing_snapshots"})
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["discover", "--source", "not-a-real-source"])
    assert exc_info.value.code == 2
