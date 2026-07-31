"""Operator-editable site settings — a tiny validated k/v store over `site_settings`.

Why this exists: the deposit rule (15% of the order, floor $200) is a *business*
number, not a deployment constant. The operator has to be able to change it from
the admin Deposits tab without a redeploy, so the live value lives in Postgres.
The `DEPOSIT_*_DEFAULT` env keys in `automation.config` are seed/fallback only.

Two hard rules encoded here:
  1. `get_all()` NEVER raises. It sits behind public page renders and the quote
     path — a Supabase blip must degrade to the seed defaults, not a 500.
  2. `set_many()` is allowlist-only and range-checked. Anything not in `SPEC`,
     or outside its bounds, is a ValueError before a statement is issued.

DDL of record: `scripts/sql/004_deposits.sql`. Schema is never created at runtime.
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from . import config, db

# key -> {type, min, max, default}. Adding a setting means adding it here AND
# to the seed INSERT in the migration; nothing else reads the table directly.
SPEC: dict[str, dict[str, Any]] = {
    # Deposit percentage of (quantity × price_per_chair). 1.0 = pay in full.
    "deposit_pct": {
        "type": float,
        "min": 0.01,
        "max": 1.0,
        "default": config.DEPOSIT_PCT_DEFAULT,
    },
    # Floor, in whole dollars. Capped at the order subtotal at quote time, so a
    # $150 order never gets asked for $200.
    "deposit_min_usd": {
        "type": int,
        "min": 0,
        "max": 100_000,
        "default": config.DEPOSIT_MIN_USD_DEFAULT,
    },
}


def defaults() -> dict[str, Any]:
    return {key: spec["default"] for key, spec in SPEC.items()}


def _coerce(key: str, value: Any) -> Any:
    """Cast to the declared type and bounds-check. Raises ValueError."""
    spec = SPEC.get(key)
    if spec is None:
        raise ValueError(f"unknown setting: {key}")
    try:
        typed = spec["type"](value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a {spec['type'].__name__}") from None
    if typed < spec["min"] or typed > spec["max"]:
        raise ValueError(f"{key} must be between {spec['min']} and {spec['max']}")
    return typed


def get_all() -> dict[str, Any]:
    """Every known setting, missing/unreadable ones filled from the seed defaults.

    Deliberately swallows everything: this runs on the public quote path.
    """
    values = defaults()
    try:
        rows = db.fetch_all("SELECT key, value FROM site_settings")
    except Exception:
        return values
    for row in rows or []:
        key = row.get("key")
        if key not in SPEC:
            continue  # a setting some future branch wrote; not ours to interpret
        try:
            values[key] = _coerce(key, row.get("value"))
        except ValueError:
            pass  # garbage in the table falls back to the default for that key
    return values


def set_many(values: dict[str, Any]) -> dict[str, Any]:
    """Validate + upsert. Returns the full settings dict after the write."""
    if not values:
        return get_all()
    cleaned = {key: _coerce(key, value) for key, value in values.items()}
    for key, value in cleaned.items():
        db.execute(
            """
            INSERT INTO site_settings (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, Jsonb(value)),
        )
    return get_all()
