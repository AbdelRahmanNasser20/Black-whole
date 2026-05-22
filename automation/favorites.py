"""Auction favorites + Telegram countdown alerts.

Why this exists: the Auctions tab pulls live lots from ``auction_extractors``
but those lots scroll past quickly. Users want to "star" a few and get
Telegram pings as the auction's end_date approaches. This module owns the
state (in ``inventory.db``) and the alert resolution logic; the scheduler
loop lives in ``automation/web/app.py`` and the Telegram send in
``automation/telegram_alerts.py``.

The schedule is fixed and intentional — see ``ALERT_INTERVALS``. Each
``(asset_id, interval_label)`` fires exactly once. End-date changes (auction
relisted under the same asset_id) reset the sent log so the new schedule
re-fires.
"""
from __future__ import annotations

import sqlite3
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from dateutil import parser as _date_parser
from dateutil.parser import UnknownTimezoneWarning

from . import inventory


# Suppress the EDT/EST "unknown timezone" warning dateutil emits on every
# GovDeals end_date string. We don't need sub-hour precision for "5 minutes
# before end" alerts.
warnings.filterwarnings("ignore", category=UnknownTimezoneWarning)


# Order matters: the scheduler walks longest → shortest so a single tick can
# fire the 6d alert and the 5m alert on different favorites in the same pass.
ALERT_INTERVALS: list[tuple[str, timedelta]] = [
    ("6d", timedelta(days=6)),
    ("3d", timedelta(days=3)),
    ("2d", timedelta(days=2)),
    ("1d", timedelta(days=1)),
    ("12h", timedelta(hours=12)),
    ("3h", timedelta(hours=3)),
    ("1h", timedelta(hours=1)),
    ("5m", timedelta(minutes=5)),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_end_date(raw: str | None) -> datetime | None:
    """Normalize a scraped end_date string to UTC. Empty / unparseable → None."""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        dt = _date_parser.parse(s, fuzzy=True)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Favorite:
    asset_id: str
    link: str
    title: str | None
    quantity: int | None
    end_date_iso: str | None
    end_date_raw: str | None
    image_url: str | None
    location: str | None
    starred_at: str
    last_synced_at: str
    notes: str | None
    sent_intervals: list[str]

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "asset_id", "link", "title", "quantity", "end_date_iso",
            "end_date_raw", "image_url", "location", "starred_at",
            "last_synced_at", "notes", "sent_intervals",
        )}
        # Add a derived seconds_until_end so the UI doesn't have to re-parse.
        end_dt = self.end_dt
        d["seconds_until_end"] = (
            int((end_dt - _now()).total_seconds()) if end_dt else None
        )
        return d

    @property
    def end_dt(self) -> datetime | None:
        if not self.end_date_iso:
            return None
        try:
            return datetime.fromisoformat(self.end_date_iso)
        except ValueError:
            return None


def _row_to_favorite(row: sqlite3.Row, sent: list[str]) -> Favorite:
    return Favorite(
        asset_id=row["asset_id"],
        link=row["link"],
        title=row["title"],
        quantity=row["quantity"],
        end_date_iso=row["end_date_iso"],
        end_date_raw=row["end_date_raw"],
        image_url=row["image_url"],
        location=row["location"],
        starred_at=row["starred_at"],
        last_synced_at=row["last_synced_at"],
        notes=row["notes"],
        sent_intervals=sent,
    )


def _sent_intervals(conn: sqlite3.Connection, asset_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT interval_label FROM auction_alerts_sent WHERE asset_id = ?",
        (asset_id,),
    ).fetchall()
    return [r[0] for r in rows]


def list_all() -> list[Favorite]:
    """All favorites, newest-starred first."""
    with inventory.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM auction_favorites ORDER BY starred_at DESC"
        ).fetchall()
        out: list[Favorite] = []
        for r in rows:
            out.append(_row_to_favorite(r, _sent_intervals(conn, r["asset_id"])))
    return out


def get(asset_id: str) -> Favorite | None:
    if not asset_id:
        return None
    with inventory.connect() as conn:
        row = conn.execute(
            "SELECT * FROM auction_favorites WHERE asset_id = ?", (asset_id,),
        ).fetchone()
        if not row:
            return None
        return _row_to_favorite(row, _sent_intervals(conn, asset_id))


def is_favorited(asset_id: str) -> bool:
    if not asset_id:
        return False
    with inventory.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM auction_favorites WHERE asset_id = ?", (asset_id,),
        ).fetchone()
    return row is not None


def upsert(
    *,
    asset_id: str,
    link: str,
    title: str | None,
    quantity: int | None,
    end_date_raw: str | None,
    image_url: str | None,
    location: str | None,
    notes: str | None = None,
) -> Favorite:
    """Add or refresh a favorite. Idempotent — re-starring the same asset
    just refreshes the snapshot. Resets the sent log if end_date changed."""
    if not asset_id:
        raise ValueError("asset_id required")
    end_dt = _parse_end_date(end_date_raw)
    end_iso = end_dt.isoformat(timespec="seconds") if end_dt else None
    now = _now_iso()
    with inventory.connect() as conn:
        existing = conn.execute(
            "SELECT end_date_iso FROM auction_favorites WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO auction_favorites (
                    asset_id, link, title, quantity, end_date_iso, end_date_raw,
                    image_url, location, starred_at, last_synced_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (asset_id, link, title, quantity, end_iso, end_date_raw,
                 image_url, location, now, now, notes),
            )
        else:
            old_iso = existing["end_date_iso"]
            conn.execute(
                """
                UPDATE auction_favorites SET
                    link = ?, title = COALESCE(?, title),
                    quantity = COALESCE(?, quantity),
                    end_date_iso = ?, end_date_raw = ?,
                    image_url = COALESCE(?, image_url),
                    location = COALESCE(?, location),
                    last_synced_at = ?,
                    notes = COALESCE(?, notes)
                WHERE asset_id = ?
                """,
                (link, title, quantity, end_iso, end_date_raw, image_url,
                 location, now, notes, asset_id),
            )
            # Relist: end_date moved → re-arm alerts.
            if end_iso and old_iso and end_iso != old_iso:
                conn.execute(
                    "DELETE FROM auction_alerts_sent WHERE asset_id = ?",
                    (asset_id,),
                )
        conn.commit()
    return get(asset_id)  # re-read


def delete(asset_id: str) -> bool:
    with inventory.connect() as conn:
        cur = conn.execute(
            "DELETE FROM auction_favorites WHERE asset_id = ?", (asset_id,),
        )
        conn.execute(
            "DELETE FROM auction_alerts_sent WHERE asset_id = ?", (asset_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def mark_sent(asset_id: str, interval_label: str) -> None:
    with inventory.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO auction_alerts_sent "
            "(asset_id, interval_label, sent_at) VALUES (?, ?, ?)",
            (asset_id, interval_label, _now_iso()),
        )
        conn.commit()


def due_alerts(
    favorites: Iterable[Favorite] | None = None,
    *,
    now: datetime | None = None,
) -> list[tuple[Favorite, str]]:
    """Return the (favorite, interval_label) pairs that should be sent now.

    A pair is due iff:
      - the favorite has a parseable end_date,
      - the end_date is still in the future,
      - now >= end_date - interval,
      - the (asset_id, interval_label) is not already in auction_alerts_sent.

    Walking longest → shortest also catches "we missed the 1d window because
    the loop was paused" — both 1d and 12h fire on the same tick if both are
    overdue, in order.
    """
    favs = list(favorites) if favorites is not None else list_all()
    now = now or _now()
    out: list[tuple[Favorite, str]] = []
    for fav in favs:
        end_dt = fav.end_dt
        if not end_dt or end_dt <= now:
            continue
        sent = set(fav.sent_intervals)
        for label, delta in ALERT_INTERVALS:
            if label in sent:
                continue
            if now >= end_dt - delta:
                out.append((fav, label))
    return out
