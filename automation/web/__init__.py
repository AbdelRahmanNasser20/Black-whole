"""Local web dashboard for listing_automation.

Launch with::

    python -m automation.web

Serves on http://127.0.0.1:8765 — public storefront (`/`, `/listings`,
`/deals`) + admin console (`/admin`, ten tabs). The A/B compare tab was
removed 2026-09-04; `llm_compare_logs` is still written by the pipeline and
read by the Inventory backfill.
"""
from .app import app, main

__all__ = ["app", "main"]
