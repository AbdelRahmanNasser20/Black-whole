"""Source health: the breaker (recorder/health.py), PollBudget + 429 fail-fast
(recorder/sources/base.py), how `run`/`poll-once` use them, the `health`
command, and the lot archive's private-bucket guard. Offline: no DB, no
network, no Telegram."""
from datetime import datetime, timedelta, timezone

import pytest

from recorder import cli, health, lot_archive, store
from recorder.models import Observation
from recorder.sources import base, public_surplus

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("RECORDER_HEALTH_TELEGRAM", "0")
    monkeypatch.setenv("RECORDER_ARCHIVE_ENABLED", "0")
    for k in ("RECORDER_BREAKER_FAILURES", "RECORDER_BREAKER_BASE_MIN", "RECORDER_BREAKER_MAX_H",
              "RECORDER_POLL_ABANDON_DAYS", "RECORDER_POLL_MAX_CONSECUTIVE_FAILURES"):
        monkeypatch.delenv(k, raising=False)


# --- breaker -------------------------------------------------------------------

def test_three_failures_open_the_breaker_with_ten_minute_backoff():
    reg = health.Registry({})
    for i in range(2):
        reg.record_failure("ps", NOW, "timeout")
        assert reg.get("ps").state == health.CLOSED
    reg.record_failure("ps", NOW, "timeout")
    h = reg.get("ps")
    assert h.state == health.OPEN
    assert h.next_attempt_at == NOW + timedelta(minutes=10)
    assert not reg.allows("ps", NOW + timedelta(minutes=9))
    assert reg.allows("ps", NOW + timedelta(minutes=10))
    assert [(t.old, t.new) for t in reg.transitions] == [("closed", "open")]


def test_half_open_probe_failure_doubles_backoff_and_success_closes():
    reg = health.Registry({})
    for _ in range(3):
        reg.record_failure("ps", NOW, "x")
    t1 = NOW + timedelta(minutes=10)
    assert reg.begin_attempt("ps", t1) is True
    assert reg.get("ps").state == health.HALF_OPEN
    reg.record_failure("ps", t1, "still down")
    assert reg.get("ps").state == health.OPEN
    assert reg.get("ps").next_attempt_at == t1 + timedelta(minutes=20)
    t2 = t1 + timedelta(minutes=20)
    assert reg.begin_attempt("ps", t2)
    reg.record_success("ps", t2)
    h = reg.get("ps")
    assert (h.state, h.consecutive_failures, h.next_attempt_at, h.last_error) == ("closed", 0, None, None)
    assert h.last_success_at == t2
    assert [t.new for t in reg.transitions] == ["open", "half_open", "open", "half_open", "closed"]


def test_backoff_caps_at_24h_and_knobs_apply(monkeypatch):
    assert health.backoff(0) == timedelta(minutes=10)
    assert health.backoff(3) == timedelta(minutes=80)
    assert health.backoff(50) == timedelta(hours=24)
    monkeypatch.setenv("RECORDER_BREAKER_BASE_MIN", "1")
    monkeypatch.setenv("RECORDER_BREAKER_MAX_H", "1")
    assert health.backoff(10) == timedelta(hours=1)
    monkeypatch.setenv("RECORDER_BREAKER_FAILURES", "1")
    reg = health.Registry({})
    reg.record_failure("x", NOW, "e")
    assert reg.get("x").state == health.OPEN


def test_registry_loads_rows_and_only_dirty_rows_are_saved():
    rows = {"ps": {"state": "open", "consecutive_failures": 5, "next_attempt_at": NOW + timedelta(hours=1),
                   "last_success_at": None, "last_attempt_at": NOW, "last_error": "e", "updated_at": NOW},
            "gsa": {"state": "closed", "consecutive_failures": 0}}
    reg = health.Registry(rows)
    assert not reg.begin_attempt("ps", NOW)
    reg.record_success("gsa", NOW)
    assert [r["source"] for r in reg.dirty_rows()] == ["gsa"]


@pytest.mark.parametrize("attempted,failed,aborted,inserted,want", [
    (10, 10, True, 0, "failure"),
    (5, 4, False, 0, "failure"),      # 80 % of >= 3 lots
    (2, 2, False, 0, "neutral"),      # too few lots to call it
    (10, 1, False, 0, "neutral"),
    (10, 1, False, 3, "success"),
    (10, 0, False, 0, "success"),     # clean empty poll
])
def test_poll_outcome(attempted, failed, aborted, inserted, want):
    assert health.poll_outcome(attempted, failed, aborted, inserted) == want


def test_open_too_long():
    h = health.SourceHealth("ps", state="open", last_success_at=NOW - timedelta(hours=25))
    assert health.open_too_long(h, NOW)
    h.last_success_at = NOW - timedelta(hours=2)
    assert not health.open_too_long(h, NOW)
    assert not health.open_too_long(health.SourceHealth("gd"), NOW)


def test_notify_only_on_open_and_close_and_never_raises(monkeypatch):
    monkeypatch.setenv("RECORDER_HEALTH_TELEGRAM", "1")
    reg = health.Registry({})
    for _ in range(3):
        reg.record_failure("ps", NOW, "down")
    reg.begin_attempt("ps", NOW + timedelta(hours=1))
    reg.record_success("ps", NOW + timedelta(hours=1))
    sent = []
    assert health.notify(reg.transitions, send=sent.append) == 2   # closed→open, half_open→closed
    assert "OPEN" in sent[0] and "recovered" in sent[1]

    def boom(text):
        raise RuntimeError("telegram down")
    assert health.notify(reg.transitions, send=boom) == 0     # swallowed
    monkeypatch.setenv("RECORDER_HEALTH_TELEGRAM", "0")
    assert health.notify(reg.transitions, send=sent.append) == 0


# --- PollBudget + 429 ------------------------------------------------------------

def test_poll_budget_aborts_after_ten_consecutive_failures():
    b = base.PollBudget()
    for _ in range(9):
        b.record(False)
    b.record(True)              # a success resets the run
    for _ in range(9):
        b.record(False)
    assert not b.exhausted
    b.record(False)
    assert b.exhausted
    assert b.stats(40) == {"attempted": 20, "failed": 19, "aborted": True, "skipped": 20}


def test_public_surplus_poll_stops_after_ten_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(public_surplus, "_fetch_detail", lambda lot_id: calls.append(lot_id))
    src = public_surplus.PublicSurplusSource()
    lots = [{"source_lot_id": str(i), "end_date": NOW} for i in range(100)]
    assert src.poll(lots) == []
    assert len(calls) == 10
    assert src.last_poll_stats == {"attempted": 10, "failed": 10, "aborted": True, "skipped": 90}


class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


def test_429_fails_fast_until_retry_after(monkeypatch):
    base._rate_limited_until.clear()
    sent = []
    monkeypatch.setattr(base.requests, "get",
                        lambda url, **k: sent.append(k["timeout"]) or _Resp(429, {"Retry-After": "120"}))
    monkeypatch.setattr(base, "_throttle", lambda host: None)
    r = base.polite_get("https://ps.example/x")
    assert r.status_code == 429                     # returned immediately, no sleep/retry
    assert sent == [base.DEFAULT_TIMEOUT] == [(10, 30)]
    with pytest.raises(base.RateLimited):
        base.polite_get("https://ps.example/y")     # no network while the window is open
    assert len(sent) == 1
    base.polite_get("https://other.example/y")      # other hosts unaffected
    assert len(sent) == 2
    base._rate_limited_until.clear()


# --- cli ------------------------------------------------------------------------

class _Src:
    def __init__(self, name, poll_obs=None, stats=None, discover_obs=None, fail_poll=False,
                 fail_discover=False):
        self.SOURCE = name
        self.poll_calls = self.discover_calls = 0
        self._poll_obs, self._stats = poll_obs or [], stats
        self._discover_obs = discover_obs or []
        self._fail_poll = fail_poll
        self._fail_discover = fail_discover
        self.last_poll_stats = None
        self.polled_rows = []

    def poll(self, rows):
        self.poll_calls += 1
        self.polled_rows = rows
        if self._fail_poll:
            raise RuntimeError("boom")
        self.last_poll_stats = self._stats
        return list(self._poll_obs)

    def discover(self):
        self.discover_calls += 1
        if self._fail_discover:
            raise base.SourceFetchFailed("discover() aborted — fetch failed")
        return list(self._discover_obs)

    def sold_sweep(self):
        return []


class _Lock:
    def execute(self, *a):
        return self

    def fetchone(self):
        return {"locked": True}

    def commit(self):
        pass

    def close(self):
        pass


def _row(source, lot, end):
    return {"source": source, "source_lot_id": lot, "observed_at": NOW - timedelta(days=30),
            "end_date": end}


@pytest.fixture
def db_stubs(monkeypatch):
    saved = []
    monkeypatch.setattr(cli.db, "connect", lambda **k: _Lock())
    monkeypatch.setattr(store, "insert_observations", lambda obs: len(list(obs)))
    monkeypatch.setattr(store, "filter_changed", lambda obs: list(obs))
    monkeypatch.setattr(store, "save_source_health", lambda rows: saved.extend(rows) or len(rows))
    return saved


def test_poll_abandons_lots_two_weeks_past_their_clock(monkeypatch, db_stubs, capsys):
    rows = [_row("ps", "old", NOW - timedelta(days=15)), _row("ps", "new", NOW - timedelta(hours=1))]
    monkeypatch.setattr(store, "tracked_active", lambda: rows)
    src = _Src("ps")
    assert cli.cmd_poll_once({"ps": src}, now=NOW) == 0
    assert [r["source_lot_id"] for r in src.polled_rows] == ["new"]
    out = capsys.readouterr().out
    assert "poll source=ps abandoned=1" in out and "abandoned=1" in out.splitlines()[-1]


def test_poll_skips_open_source_and_records_failures(monkeypatch, db_stubs, capsys):
    monkeypatch.setattr(store, "tracked_active", lambda: [_row("ps", "1", NOW - timedelta(hours=1))])
    reg = health.Registry({})
    src = _Src("ps", stats={"attempted": 10, "failed": 10, "aborted": True, "skipped": 0})
    for _ in range(3):
        cli.cmd_poll_once({"ps": src}, now=NOW, health_reg=reg)
    assert reg.get("ps").state == health.OPEN
    assert src.poll_calls == 3
    cli.cmd_poll_once({"ps": src}, now=NOW + timedelta(minutes=1), health_reg=reg)
    assert src.poll_calls == 3
    assert "poll source=ps skipped: circuit open until 2026-10-02T12:10:00Z" in capsys.readouterr().out


def test_run_skips_discover_for_open_source_and_uses_last_discover_for_staleness(monkeypatch, db_stubs, capsys):
    monkeypatch.setattr(store, "tracked_active", lambda: [])
    monkeypatch.setattr(store, "newest_observed_at", lambda s: NOW - timedelta(days=3))
    monkeypatch.setattr(store, "load_source_health", lambda: {
        "ps": {"state": "open", "consecutive_failures": 4, "next_attempt_at": NOW + timedelta(hours=2)},
        "quiet": {"state": "closed", "consecutive_failures": 0, "last_discover_at": NOW - timedelta(hours=1)},
    })
    ps, quiet, mibid = _Src("ps"), _Src("quiet"), _Src("mibid", discover_obs=[
        Observation(source="mibid", source_lot_id="g", status="active", raw={})])
    rc = cli.cmd_run({"ps": ps, "quiet": quiet, "mibid": mibid}, discover_stale_hours=6, now=NOW)
    assert rc == 0
    assert ps.discover_calls == 0          # breaker open
    assert quiet.discover_calls == 0       # clean discover 1 h ago, nothing new inserted since
    assert mibid.discover_calls == 1
    out = capsys.readouterr().out
    assert "discover source=ps skipped: circuit open until" in out
    assert "run: elapsed=" in out
    assert {r["source"]: r["state"] for r in db_stubs} == {"mibid": "closed"}


def test_run_empty_discover_counts_as_success(monkeypatch, db_stubs):
    """Fetched fine, matched 0 lots (mibid most days) = a quiet day, not an
    outage: the breaker resets and last_discover_at moves."""
    monkeypatch.setattr(store, "tracked_active", lambda: [])
    monkeypatch.setattr(store, "newest_observed_at", lambda s: None)
    monkeypatch.setattr(store, "load_source_health", lambda: {
        "ps": {"state": "closed", "consecutive_failures": 2, "last_error": "earlier"}})
    cli.cmd_run({"ps": _Src("ps")}, discover_stale_hours=6, now=NOW)
    row = db_stubs[0]
    assert (row["state"], row["consecutive_failures"], row["last_error"]) == ("closed", 0, None)
    assert row["last_discover_at"] == NOW and row["last_success_at"] == NOW


def test_run_fetch_failed_discover_counts_as_failure(monkeypatch, db_stubs, capsys):
    monkeypatch.setattr(store, "tracked_active", lambda: [])
    monkeypatch.setattr(store, "newest_observed_at", lambda s: None)
    monkeypatch.setattr(store, "load_source_health", lambda: {})
    cli.cmd_run({"ps": _Src("ps", fail_discover=True)}, discover_stale_hours=6, now=NOW)
    assert db_stubs[0]["consecutive_failures"] == 1
    assert db_stubs[0]["last_error"] == "discover fetch failed: discover() aborted — fetch failed"
    assert "RECORDER ERROR source=ps discover failed: discover() aborted" in capsys.readouterr().err


def test_run_without_table_uses_in_memory_breaker(monkeypatch, db_stubs, capsys):
    monkeypatch.setattr(store, "tracked_active", lambda: [])
    monkeypatch.setattr(store, "newest_observed_at", lambda s: NOW)
    monkeypatch.setattr(store, "load_source_health", lambda: None)
    assert cli.cmd_run({"ps": _Src("ps")}, now=NOW) == 0
    assert "migration 018 not applied" in capsys.readouterr().err
    assert db_stubs == []


def test_health_command_table_and_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(store, "load_source_health", lambda: {
        "govdeals": {"state": "closed", "consecutive_failures": 0, "last_success_at": NOW},
        "public_surplus": {"state": "open", "consecutive_failures": 7, "last_error": "timeout",
                           "last_success_at": NOW - timedelta(days=2),
                           "next_attempt_at": NOW + timedelta(hours=8)},
    })
    assert cli.cmd_health(now=NOW) == 1
    out = capsys.readouterr()
    assert "STATE" in out.out and "public_surplus" in out.out and "timeout" in out.out
    assert "open > 24h: public_surplus" in out.err
    monkeypatch.setattr(store, "load_source_health", lambda: None)
    assert cli.cmd_health(now=NOW) == 0


def test_health_subcommand_is_wired():
    assert cli.build_parser().parse_args(["health"]).cmd == "health"


# --- lot archive private bucket (#107 latent bug) ---------------------------------

_CFG = {"bucket": "blackwhole-images", "account_id": "a", "access_key_id": "k",
        "secret_access_key": "s", "public_base": "https://pub.r2.dev"}


def test_r2store_refuses_without_private_bucket(monkeypatch):
    monkeypatch.delenv("LOT_ARCHIVE_R2_BUCKET", raising=False)
    with pytest.raises(lot_archive.PrivateBucketNotConfigured):
        lot_archive.R2Store(cfg=_CFG, s3=object())


def test_r2store_refuses_the_public_bucket(monkeypatch):
    monkeypatch.setenv("LOT_ARCHIVE_R2_BUCKET", "blackwhole-images")
    with pytest.raises(lot_archive.PrivateBucketNotConfigured):
        lot_archive.R2Store(cfg=_CFG, s3=object())


def test_r2store_uses_the_private_bucket(monkeypatch):
    monkeypatch.setenv("LOT_ARCHIVE_R2_BUCKET", "blackwhole-archive")
    assert lot_archive.R2Store(cfg=_CFG, s3=object()).bucket == "blackwhole-archive"


def test_store_from_env_none_with_reason_and_writer_raises(monkeypatch):
    from automation import r2_images
    monkeypatch.delenv("LOT_ARCHIVE_STORE", raising=False)
    monkeypatch.delenv("LOT_ARCHIVE_R2_BUCKET", raising=False)
    monkeypatch.setattr(r2_images, "env_config", lambda: dict(_CFG))
    assert lot_archive.store_from_env() is None
    assert "LOT_ARCHIVE_R2_BUCKET" in lot_archive.last_store_error()
    with pytest.raises(lot_archive.StoreNotConfigured):
        lot_archive.require_store()


def test_health_file_fallback_persists_across_runs(monkeypatch, db_stubs, tmp_path):
    path = str(tmp_path / "health.json")
    monkeypatch.setenv("RECORDER_HEALTH_FILE", path)
    monkeypatch.setattr(store, "tracked_active", lambda: [])
    monkeypatch.setattr(store, "newest_observed_at", lambda s: None)
    monkeypatch.setattr(store, "load_source_health", lambda: None)
    src = _Src("ps", fail_discover=True)   # fetch failed = failed attempt
    for _ in range(3):
        cli.cmd_run({"ps": src}, discover_stale_hours=6, now=NOW)
    assert health.load_file(path)["ps"]["state"] == "open"
    cli.cmd_run({"ps": src}, discover_stale_hours=6, now=NOW + timedelta(minutes=5))
    assert src.discover_calls == 3
    assert db_stubs == []


def test_lock_connection_backs_off_when_pooler_is_full(monkeypatch):
    import psycopg
    calls = []

    def connect(**k):
        calls.append(1)
        if len(calls) < 3:
            raise psycopg.OperationalError("FATAL: (EMAXCONNSESSION) max clients reached in session mode")
        return "conn"

    monkeypatch.setattr(cli.db, "connect", connect)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli._connect_lock_conn() == "conn" and len(calls) == 3
    monkeypatch.setattr(cli.db, "connect", lambda **k: (_ for _ in ()).throw(psycopg.OperationalError("bad password")))
    with pytest.raises(psycopg.OperationalError):
        cli._connect_lock_conn()



# --- review fixes (2026-10-02) -------------------------------------------------------

def test_clean_quiet_polls_never_hold_discover_off(monkeypatch, db_stubs):
    """Polls succeed every run (refreshing last_success_at) but insert nothing;
    the newest row is 3 days old → discover must still fire once 6 h pass."""
    monkeypatch.setattr(store, "tracked_active", lambda: [_row("ps", "1", NOW + timedelta(days=2))])
    monkeypatch.setattr(store, "newest_observed_at", lambda s: NOW - timedelta(days=3))
    monkeypatch.setattr(store, "filter_changed", lambda obs: [])        # nothing new
    rows = {}
    monkeypatch.setattr(store, "load_source_health", lambda: dict(rows))
    monkeypatch.setattr(store, "save_source_health",
                        lambda rs: rows.update({r["source"]: r for r in rs}) or len(rs))
    obs = [Observation(source="ps", source_lot_id="1", status="active", raw={})]
    src = _Src("ps", poll_obs=obs, discover_obs=obs,
               stats={"attempted": 1, "failed": 0, "aborted": False, "skipped": 0})
    cli.cmd_run({"ps": src}, discover_stale_hours=6, now=NOW)
    assert src.discover_calls == 1                       # newest row 3 d old → stale
    assert rows["ps"]["last_discover_at"] == NOW
    cli.cmd_run({"ps": src}, discover_stale_hours=6, now=NOW + timedelta(hours=1))
    assert src.discover_calls == 1                       # discovered 1 h ago
    assert rows["ps"]["last_success_at"] == NOW + timedelta(hours=1)   # poll success
    cli.cmd_run({"ps": src}, discover_stale_hours=6, now=NOW + timedelta(hours=6, minutes=1))
    assert src.discover_calls == 2                       # clean polls did not hold it off


def test_failed_half_open_probe_is_not_pinged(monkeypatch):
    monkeypatch.setenv("RECORDER_HEALTH_TELEGRAM", "1")
    reg = health.Registry({})
    for _ in range(3):
        reg.record_failure("ps", NOW, "down")
    for k in range(1, 4):                       # three failed probes
        t = NOW + timedelta(hours=k * 10)
        reg.begin_attempt("ps", t)
        reg.record_failure("ps", t, "still down")
    sent = []
    assert health.notify(reg.transitions, send=sent.append) == 1 and "OPEN" in sent[0]


def test_public_surplus_discover_stops_after_a_connect_failure(monkeypatch):
    import requests
    calls = []

    def dead(url, **k):
        calls.append(k["params"]["keyWord"])
        raise requests.exceptions.ConnectTimeout("connect timeout=10")

    monkeypatch.setattr(public_surplus, "polite_get", dead)
    with pytest.raises(base.SourceFetchFailed):
        public_surplus.PublicSurplusSource().discover()
    assert len(calls) == 1


def test_run_budget_skips_remaining_discovers(monkeypatch, db_stubs, capsys):
    monkeypatch.setenv("RECORDER_RUN_BUDGET_S", "5")
    monkeypatch.setattr(store, "tracked_active", lambda: [])
    monkeypatch.setattr(store, "newest_observed_at", lambda s: None)
    monkeypatch.setattr(store, "load_source_health", lambda: {})
    clock = iter([0.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock, 100.0))
    a = _Src("a")
    cli.cmd_run({"a": a}, discover_stale_hours=6, now=NOW)
    assert a.discover_calls == 0
    assert "skipped: run budget spent" in capsys.readouterr().out


def test_health_upsert_sql_with_and_without_last_discover_at():
    base_sql = store._health_upsert_sql(store._HEALTH_COLS)
    assert "last_discover_at" not in base_sql and base_sql.count("%s") == len(store._HEALTH_COLS)
    full = store._health_upsert_sql(store._HEALTH_COLS + ("last_discover_at",))
    assert "last_discover_at = EXCLUDED.last_discover_at" in full


# --- per-host proxy (RECORDER_PROXY_URL / RECORDER_PROXY_HOSTS) -----------------

PROXY = "socks5h://127.0.0.1:1081"


@pytest.fixture
def proxy_env(monkeypatch):
    monkeypatch.setenv(base.PROXY_URL_ENV, PROXY)
    monkeypatch.setenv(base.PROXY_HOSTS_ENV, "publicsurplus.com, mibid.michigan.gov")
    monkeypatch.setattr(base, "_throttle", lambda host: None)
    base._rate_limited_until.clear()
    base._last_failure.clear()
    sent = []
    monkeypatch.setattr(base.requests, "get",
                        lambda url, **k: sent.append((url, k["proxies"])) or _Resp(200))
    return sent


def test_only_listed_hosts_are_proxied(monkeypatch, proxy_env):
    monkeypatch.setattr(base, "_socket_open", lambda h, p, timeout=0: True)
    base.polite_get("https://www.publicsurplus.com/sms/browse/search")   # suffix match
    base.polite_get("https://mibid.michigan.gov/")
    base.polite_get("https://maestro.govdeals.com/search")               # direct
    base.polite_get("https://notpublicsurplus.com/x")                    # no partial-label match
    assert [p for _, p in proxy_env] == [{"http": PROXY, "https": PROXY},
                                         {"http": PROXY, "https": PROXY}, None, None]


def test_proxy_down_fails_loud_before_any_request(monkeypatch, proxy_env):
    monkeypatch.setattr(base, "_socket_open", lambda h, p, timeout=0: False)
    with pytest.raises(base.ProxyUnavailable) as exc:
        base.polite_get("https://www.publicsurplus.com/sms/browse/search")
    assert "not listening" in str(exc.value) and "NOT fetched direct" in str(exc.value)
    assert proxy_env == []                                               # never went direct
    assert isinstance(exc.value, base.requests.exceptions.ConnectionError)
    base.polite_get("https://maestro.govdeals.com/search")               # unlisted host unaffected
    assert len(proxy_env) == 1
    # the reason reaches the breaker through SourceFetchFailed
    err = base.SourceFetchFailed("discover() aborted", url="https://www.publicsurplus.com/x")
    assert "not listening" in str(err)


def test_proxy_hosts_without_url_is_a_config_error_not_a_direct_fetch(monkeypatch, proxy_env):
    monkeypatch.delenv(base.PROXY_URL_ENV)
    with pytest.raises(base.ProxyUnavailable, match="RECORDER_PROXY_URL is unset"):
        base.polite_get("https://www.publicsurplus.com/sms/browse/search")
    assert proxy_env == []


def test_no_proxy_env_means_everything_direct(monkeypatch, proxy_env):
    monkeypatch.delenv(base.PROXY_URL_ENV)
    monkeypatch.delenv(base.PROXY_HOSTS_ENV)
    base.polite_get("https://www.publicsurplus.com/sms/browse/search")
    assert [p for _, p in proxy_env] == [None]


def test_socket_probe_against_real_ports():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert base._socket_open("127.0.0.1", port, timeout=1)
    finally:
        srv.close()
    assert not base._socket_open("127.0.0.1", port, timeout=1)


def test_proxy_down_opens_public_surplus_breaker_with_readable_reason(monkeypatch, proxy_env, db_stubs):
    monkeypatch.setattr(base, "_socket_open", lambda h, p, timeout=0: False)
    reg = health.Registry({})
    src = public_surplus.PublicSurplusSource()
    with pytest.raises(base.SourceFetchFailed, match="not listening"):
        src.discover()
    assert proxy_env == []                      # host_down short-circuit: one probe, no direct fetch
    cli.cmd_discover({"public_surplus": src}, health_reg=reg, now=NOW)
    h = reg.get("public_surplus")
    assert h.consecutive_failures == 1 and "not listening" in h.last_error
