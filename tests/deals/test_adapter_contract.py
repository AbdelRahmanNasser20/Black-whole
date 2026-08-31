"""Prove the contract kit itself against the existing GovDeals adapter's fixture."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from deals.mapping import asset_to_lot
from tests.deals.adapter_contract import check_lots

FIXTURE = json.loads(Path("tests/deals/fixtures/maestro_page.json").read_text())


def _fixture_lots():
    return [asset_to_lot(raw) for raw in FIXTURE]


def test_kit_passes_on_govdeals_fixture():
    check_lots(_fixture_lots(), site="govdeals")


def test_kit_rejects_wrong_site():
    with pytest.raises(AssertionError):
        check_lots(_fixture_lots(), site="marknet")


def test_kit_rejects_naive_end_utc():
    lots = _fixture_lots()
    lots[0].end_utc = datetime(2026, 7, 3, 13)   # deliberately corrupted: no tzinfo
    with pytest.raises(AssertionError, match="naive end_utc"):
        check_lots(lots, site="govdeals")


def test_kit_rejects_empty():
    with pytest.raises(AssertionError, match="yielded nothing"):
        check_lots([], site="govdeals")
