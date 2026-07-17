"""DC / DMV sourcing alerts — BLACKWHOLE-19.

A DMV-scoped (DC/MD/VA) sourcing-alert component: alert on any new chair lot
within 100 mi of DC so the first DC-area lot can be sourced. Standalone and
additive — reuses the shipped geo helpers (``automation.alerts.geo``) and does
not restructure the deals pipeline.
"""
from .alerts import dmv_match, filter_dmv_lots
from .digest import format_sourcing_digest, run_dmv_sourcing_alert

__all__ = [
    "dmv_match",
    "filter_dmv_lots",
    "format_sourcing_digest",
    "run_dmv_sourcing_alert",
]
