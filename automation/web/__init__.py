"""Local web dashboard for listing_automation.

Launch with::

    python -m automation.web

Serves on http://127.0.0.1:8765 — three tabs:
  1. Launcher  — paste GovDeals URL, stream pipeline progress
  2. Drafts    — review per-listing folders, FB + eBay draft URLs
  3. Compare   — side-by-side LLM A/B viewer with star ratings
"""
from .app import app, main

__all__ = ["app", "main"]
