"""SQL-lint for migration 030: every platform table has RLS + service_role_all."""
import re
from pathlib import Path

SQL = (Path(__file__).resolve().parents[2] / "scripts/sql/030_platform_tenancy.sql").read_text()
TABLES = ["platform_users", "platform_alerts", "platform_saved_lots", "platform_alert_sends"]


def test_creates_exactly_the_four_tables():
    created = re.findall(r"CREATE TABLE IF NOT EXISTS public\.(\w+)", SQL)
    assert created == TABLES


def test_every_table_has_rls_and_service_role_policy():
    for t in TABLES:
        assert f"ALTER TABLE public.{t} ENABLE ROW LEVEL SECURITY;" in SQL
        assert f"DROP POLICY IF EXISTS service_role_all ON public.{t};" in SQL
        assert re.search(
            rf"CREATE POLICY service_role_all ON public\.{t}\s+"
            r"FOR ALL TO service_role USING \(true\) WITH CHECK \(true\);",
            SQL,
        )


def test_no_force_rls():
    assert "FORCE ROW LEVEL SECURITY" not in SQL.upper()


def test_no_semicolons_in_comments():
    for line in SQL.splitlines():
        if line.lstrip().startswith("--"):
            assert ";" not in line, line


def test_active_partial_index():
    assert re.search(r"ON public\.platform_alerts\s*\(active\)\s+WHERE active;", SQL)
