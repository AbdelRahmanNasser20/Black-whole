"""Tiny structured-progress emitter shared between run.py and the dashboard.

Writes JSON-encoded events as single lines prefixed with `<<<EVENT>>>` to
stdout. Existing human-readable prints are untouched, so the CLI experience
doesn't change. The dashboard subprocess wrapper parses these lines to drive
the SSE stream.

Usage:
    from automation import progress
    progress.emit("phase", phase="scrape", status="running")
    progress.emit("phase", phase="scrape", status="done", images=42)
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

EVENT_PREFIX = "<<<EVENT>>>"


def emit(kind: str, **fields: Any) -> None:
    payload = {"ts": time.time(), "kind": kind, **fields}
    try:
        sys.stdout.write(f"{EVENT_PREFIX}{json.dumps(payload, default=str)}\n")
        sys.stdout.flush()
    except Exception:
        pass


def parse(line: str) -> dict | None:
    if not line.startswith(EVENT_PREFIX):
        return None
    try:
        return json.loads(line[len(EVENT_PREFIX):].strip())
    except Exception:
        return None
