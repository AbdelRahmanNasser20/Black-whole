"""Single place for `auction_extractors/` directory paths (runtime state, reports, logs)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"
