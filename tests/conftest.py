"""Suite-wide guards."""
import pytest


@pytest.fixture(autouse=True)
def _no_photo_autosync(monkeypatch):
    """`inventory.set_fields` starts a background R2 photo sync when a lot
    crosses `active_bid` (automation/photo_sync.py). Never from a test: it would
    read `.env` R2 creds and the real DB. Tests that want it re-enable it."""
    monkeypatch.setenv("PHOTO_POLICY_AUTOSYNC", "0")
