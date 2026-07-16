"""New-inventory alert blast (BLACKWHOLE-10).

Public surface:
  - `run_blast(lot_id, ...)` / `preview_blast(lot_id, ...)` — the job.
  - `match_lot(lot, subscribers, ...)` — the pure matching engine.
  - `email_sender` — provider-agnostic sender interface (dry-run by default).

SEND IS DISABLED BY DEFAULT. See `blast.run_blast` and `email_sender`.
"""
from __future__ import annotations

from . import email_sender
from .blast import BlastReport, compose_email, preview_blast, run_blast
from .matcher import Match, Skip, match_lot

__all__ = [
    "BlastReport",
    "Match",
    "Skip",
    "compose_email",
    "email_sender",
    "match_lot",
    "preview_blast",
    "run_blast",
]
