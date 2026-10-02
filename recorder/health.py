"""Per-source circuit breaker for the recorder (source health).

Why: a dead source used to cost every run. Public Surplus answered ~46k
30-second connect timeouts; Municibid 403'd; a PS poll batch could burn ~1 h,
the 5-minute run overran, the advisory lock turned the next ticks into
no-ops, and GovDeals closes were missed. One sick source must cost one probe
per backoff window, not a whole run.

Pure: no DB, no network, no clock beyond the `now` it is handed. The CLI loads
the rows once per run (`store.load_source_health`), asks `begin_attempt`
before touching a source, reports `record_success` / `record_failure`, and
saves once at the end.

    closed ──3 failed attempts──▶ open ──next_attempt_at──▶ half_open (one probe)
      ▲                                                       │ fail → open, backoff doubles
      └──────────────────────── success ◀─────────────────────┘

Backoff once open: min(BASE × 2^k, MAX), k = failures beyond the threshold.
Knobs: RECORDER_BREAKER_FAILURES (3), RECORDER_BREAKER_BASE_MIN (10),
RECORDER_BREAKER_MAX_H (24).

What counts (decided by the CLI, recorded here):
- failed attempt: discover raised or came back with 0 observations (every
  adapter "aborts" by printing a RECORDER ERROR and returning []); a poll
  batch that raised, was aborted by its PollBudget, or had ≥ 80 % of its lots
  fail (≥ 3 lots — the BLOCK_SUSPECT thresholds public_surplus uses).
- success: discover returned observations; a poll batch that inserted ≥ 1
  row or had no failed lot.
- anything in between (a few lots failed, nothing new) changes nothing.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from datetime import datetime, timedelta

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

FAILURES_DEFAULT = 3
BASE_MIN_DEFAULT = 10.0
MAX_H_DEFAULT = 24.0

# A poll batch is a failed attempt when at least this many lots AND at least
# this fraction of the batch failed — the same thresholds as
# recorder/sources/base.py's BLOCK_SUSPECT_* (shared with public_surplus).
from recorder.sources.base import (  # noqa: E402
    BLOCK_SUSPECT_MIN_COUNT as POLL_FAIL_MIN_COUNT,
    BLOCK_SUSPECT_MIN_FRACTION as POLL_FAIL_MIN_FRACTION,
)

OPEN_ALERT_AFTER = timedelta(hours=24)
_ERR_MAX = 500


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def failure_threshold() -> int:
    return max(1, int(_env_float("RECORDER_BREAKER_FAILURES", FAILURES_DEFAULT)))


def backoff(k: int) -> timedelta:
    """Wait before the next probe after the k-th failure beyond the threshold
    (k = 0 for the attempt that opened the breaker)."""
    base = timedelta(minutes=_env_float("RECORDER_BREAKER_BASE_MIN", BASE_MIN_DEFAULT))
    cap = timedelta(hours=_env_float("RECORDER_BREAKER_MAX_H", MAX_H_DEFAULT))
    k = max(0, k)
    if k > 30:   # 2**k overflows timedelta long before it matters
        return cap
    return min(base * (2 ** k), cap)


@dataclass
class SourceHealth:
    source: str
    state: str = CLOSED
    consecutive_failures: int = 0
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    updated_at: datetime | None = None
    # Last clean DISCOVER (not poll): `run`'s staleness reference. Poll
    # successes refresh last_success_at too, so using that for staleness kept
    # a source with clean, quiet polls from ever being re-discovered.
    # Column = migration 020 (PENDING); absent → not persisted, nothing breaks.
    last_discover_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict) -> "SourceHealth":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in row.items() if k in names})

    def as_row(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class Transition:
    source: str
    old: str
    new: str
    error: str | None
    next_attempt_at: datetime | None


class Registry:
    """The run's view of every source's breaker. `dirty` = what to save."""

    def __init__(self, rows: dict[str, dict] | None = None, persisted: bool = True):
        self.persisted = persisted
        self.file: str | None = None
        self._by_source: dict[str, SourceHealth] = {
            s: SourceHealth.from_row({**r, "source": s}) for s, r in (rows or {}).items()}
        self.dirty: set[str] = set()
        self.transitions: list[Transition] = []

    def get(self, source: str) -> SourceHealth:
        h = self._by_source.get(source)
        if h is None:
            h = self._by_source[source] = SourceHealth(source)
        return h

    def all(self) -> list[SourceHealth]:
        return [self._by_source[s] for s in sorted(self._by_source)]

    def _set_state(self, h: SourceHealth, new: str, now: datetime) -> None:
        if h.state != new:
            self.transitions.append(Transition(h.source, h.state, new, h.last_error,
                                               h.next_attempt_at))
            h.state = new
        h.updated_at = now
        self.dirty.add(h.source)

    # --- the three calls the CLI makes ---------------------------------

    def allows(self, source: str, now: datetime) -> bool:
        """Read-only: may this source be tried now?"""
        h = self.get(source)
        if h.state == CLOSED:
            return True
        return h.next_attempt_at is None or now >= h.next_attempt_at

    def begin_attempt(self, source: str, now: datetime) -> bool:
        """True = go ahead (an open breaker past its window becomes half_open,
        one probe). False = skip this source this run."""
        if not self.allows(source, now):
            return False
        h = self.get(source)
        if h.state == OPEN:
            self._set_state(h, HALF_OPEN, now)
        return True

    def record_success(self, source: str, now: datetime, kind: str = "poll") -> None:
        """kind = "poll" | "discover"; only a discover moves last_discover_at."""
        h = self.get(source)
        if kind == "discover":
            h.last_discover_at = now
        h.consecutive_failures = 0
        h.last_attempt_at = now
        h.last_success_at = now
        h.next_attempt_at = None
        h.last_error = None
        self._set_state(h, CLOSED, now)

    def record_failure(self, source: str, now: datetime, error: str) -> None:
        h = self.get(source)
        h.consecutive_failures += 1
        h.last_attempt_at = now
        h.last_error = (error or "")[:_ERR_MAX]
        threshold = failure_threshold()
        if h.consecutive_failures >= threshold:
            h.next_attempt_at = now + backoff(h.consecutive_failures - threshold)
            self._set_state(h, OPEN, now)
        else:
            h.updated_at = now
            self.dirty.add(source)

    def record_neutral(self, source: str, now: datetime) -> None:
        h = self.get(source)
        h.last_attempt_at = now
        h.updated_at = now
        self.dirty.add(source)

    def dirty_rows(self) -> list[dict]:
        return [self._by_source[s].as_row() for s in sorted(self.dirty)]


def poll_outcome(attempted: int, failed: int, aborted: bool, inserted: int) -> str:
    """'failure' | 'success' | 'neutral' for one source's poll batch."""
    if aborted:
        return "failure"
    if attempted >= POLL_FAIL_MIN_COUNT and failed / attempted >= POLL_FAIL_MIN_FRACTION:
        return "failure"
    if inserted > 0 or failed == 0:
        return "success"
    return "neutral"


def open_too_long(h: SourceHealth, now: datetime, after: timedelta = OPEN_ALERT_AFTER) -> bool:
    """`health` exits 1 for these: not closed, and no success for > 24 h."""
    if h.state == CLOSED:
        return False
    ref = h.last_success_at or h.updated_at or h.last_attempt_at
    return ref is None or (now - ref) > after


def format_transition(t: Transition) -> str:
    if t.new == OPEN:
        when = t.next_attempt_at.strftime("%Y-%m-%d %H:%M UTC") if t.next_attempt_at else "?"
        return (f"recorder: {t.source} circuit OPEN ({t.old} → open) — next probe {when}. "
                f"Last error: {t.error or '—'}")
    if t.new == CLOSED:
        return f"recorder: {t.source} recovered ({t.old} → closed)"
    return f"recorder: {t.source} {t.old} → {t.new}"


def telegram_enabled() -> bool:
    return (os.getenv("RECORDER_HEALTH_TELEGRAM") or "1").strip().lower() not in (
        "0", "false", "no", "off")


def pingworthy(t: Transition) -> bool:
    """closed → open (a source just broke) and any → closed (it recovered).
    A failed half-open probe (half_open → open) is the same outage still
    going on — not worth a ping every backoff window."""
    return (t.old == CLOSED and t.new == OPEN) or (t.new == CLOSED and t.old != CLOSED)


def notify(transitions: list[Transition], send=None) -> int:
    """Best-effort Telegram "health" ping per `pingworthy` transition. Never
    raises. Returns pings sent."""
    wanted = [t for t in transitions if pingworthy(t)]
    if not wanted or not telegram_enabled():
        return 0
    sent = 0
    try:
        if send is None:
            from automation import telegram_alerts
            if not telegram_alerts.is_configured():
                return 0
            send = lambda text: telegram_alerts.send_message_sync(text, topic="health")  # noqa: E731
        for t in wanted:
            try:
                send(format_transition(t))
                sent += 1
            except Exception as e:  # noqa: BLE001 - alerts never break the run
                print(f"RECORDER NOTE: health telegram failed ({e!r})")
    except Exception as e:  # noqa: BLE001
        print(f"RECORDER NOTE: health telegram unavailable ({e!r})")
    return sent


# --- dev fallback while migration 018 is PENDING --------------------------------
#
# RECORDER_HEALTH_FILE=/path.json keeps the breaker across runs on ONE machine
# (the laptop soak) when the table is missing. Render cron disks are
# ephemeral, so production needs the table; the file is never read once the
# table exists.

def state_file() -> str | None:
    return (os.getenv("RECORDER_HEALTH_FILE") or "").strip() or None


def _ts(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


_TS_FIELDS = ("last_attempt_at", "last_success_at", "next_attempt_at", "updated_at",
              "last_discover_at")


def load_file(path: str) -> dict[str, dict]:
    try:
        with open(os.path.expanduser(path)) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    return {s: {k: (_ts(v) if k in _TS_FIELDS else v) for k, v in r.items()}
            for s, r in (data or {}).items()}


def save_file(path: str, reg: "Registry") -> None:
    path = os.path.expanduser(path)
    data = {h.source: {k: (v.isoformat() if isinstance(v, datetime) else v)
                       for k, v in h.as_row().items()} for h in reg.all()}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
