"""Data model for the closing-price recorder.

One dataclass, `Observation`: a single append-only snapshot of a listing at
a point in time. Every recorder source adapter (Task 2-4) produces lists of
these; `recorder.store.insert_observations` is the only thing allowed to
turn them into rows in Supabase's `listing_snapshots` table.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class Observation:
    source: str          # govdeals | public_surplus | purple_wave | municibid | mibid | gsa
    source_lot_id: str    # source-native unique id; govdeals uses "asset/account/auction"
    status: str            # active | closed | gone
    raw: dict              # untouched source payload — sacred, never mutated
    current_bid: Decimal | None = None
    bid_count: int | None = None
    end_date: datetime | None = None      # tz-aware UTC
    observed_at: datetime | None = None   # None -> DB default now()
