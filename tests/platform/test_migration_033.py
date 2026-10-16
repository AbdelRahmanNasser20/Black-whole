"""Migration 033: alert deletion cascades to platform_alert_sends (fake DB cannot enforce FKs)."""
import re
from pathlib import Path

SQL = (Path(__file__).resolve().parents[2] / "scripts/sql/033_platform_sends_cascade.sql").read_text()


def test_fk_recreated_with_cascade():
    assert "DROP CONSTRAINT IF EXISTS platform_alert_sends_alert_id_fkey" in SQL
    assert re.search(r"REFERENCES public\.platform_alerts\(id\) ON DELETE CASCADE", SQL)


def test_no_semicolons_in_comments():
    for line in SQL.splitlines():
        if line.startswith("--"):
            assert ";" not in line, line
