"""static/admin/fmt_time.js fmtClose — readable close times on the Auctions cards.
Runs the real ES module under node with TZ pinned to US Eastern (labels are viewer-local)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path("automation/web/static/admin/fmt_time.js").resolve()
NOW = "2026-10-10T14:00:00Z"          # Sat Oct 10 2026, 10:00 AM EDT

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(cases: list[str]) -> list[dict]:
    script = (f"import {{fmtClose}} from {json.dumps(MODULE.as_uri())};\n"
              f"const now = Date.parse({json.dumps(NOW)});\n"
              f"console.log(JSON.stringify({json.dumps(cases)}.map(c => fmtClose(c, now))));\n")
    r = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True, text=True,
                       env={**os.environ, "TZ": "America/New_York"}, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


CASES = [
    ("2026-10-13T18:00:00Z", "3d 4h left · Tue Oct 13", "ends-ok"),
    ("2026-10-10T19:00:00Z", "5h 0m left · today 3:00 PM", "ends-yellow"),
    ("2026-10-10T15:30:00Z", "1h 30m left · today 11:30 AM", "ends-red"),
    ("2026-10-10T14:30:00Z", "30m left · today 10:30 AM", "ends-red"),
    ("2026-10-11T05:00:00Z", "15h 0m left · tomorrow 1:00 AM", "ends-yellow"),
    ("2026-10-09T18:00:00Z", "Ended Oct 9", "ends-over"),
    ("2025-12-30T18:00:00Z", "Ended Dec 30, 2025", "ends-over"),
    ("2027-01-05T15:00:00Z", "87d 1h left · Tue Jan 5, 2027", "ends-ok"),
]


def test_labels_and_classes():
    out = _run([c[0] for c in CASES])
    for (iso, label, cls), got in zip(CASES, out):
        assert (got["label"], got["cls"]) == (label, cls), iso
        assert got["title"], f"{iso}: hover title carries the full local date"


def test_invalid_input_is_empty_so_the_card_falls_back():
    for got in _run(["", "garbage", "not-a-date"]):
        assert got == {"label": "", "cls": "", "title": ""}
