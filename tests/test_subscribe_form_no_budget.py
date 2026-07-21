"""The alert-signup form must not render a budget question. Static file check.
Run: .venv/bin/python -m pytest tests/test_subscribe_form_no_budget.py -v
"""
from pathlib import Path

FORM = Path("automation/web/templates/_subscribe_form.html").read_text()


def test_no_budget_field_in_subscribe_form():
    assert "budget_per_chair" not in FORM
    assert "BUDGET" not in FORM.upper()
