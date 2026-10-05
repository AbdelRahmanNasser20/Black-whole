"""The one parser for scraped auction end-date strings.

GovDeals displays *every* auction's close time in US Eastern, site-wide,
regardless of the lot's physical location (verified: lots in CA, CO, TX, NE
all stamp "EDT"). Its maestro API returns a NAIVE ISO string with no zone
(e.g. "2026-06-15T19:50:00"), so a naive end_date means Eastern — NOT UTC.
Labeling it UTC shifts every close 4–5h early.

That bug was fixed once in the favorites alert path and survived in the
Auctions tab's active-window filter, which hid live lots for the last 4 hours
of their auction (Orlando 28860/2863, 2026-10-05). Both paths now import this
module, so the rule lives in one place.

Strings that carry an explicit zone (the DOM-timer "... PM EDT" path, Public
Surplus / BidSpotter "...Z") keep their own zone. ZoneInfo is backed by system
tzdata or the `tzdata` package, so it resolves on any host.
"""
from __future__ import annotations

import warnings
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as _date_parser
from dateutil.parser import UnknownTimezoneWarning

warnings.filterwarnings("ignore", category=UnknownTimezoneWarning)

EASTERN = ZoneInfo("America/New_York")
TZINFOS = {
    "EST": EASTERN, "EDT": EASTERN,
    "CST": ZoneInfo("America/Chicago"), "CDT": ZoneInfo("America/Chicago"),
    "MST": ZoneInfo("America/Denver"), "MDT": ZoneInfo("America/Denver"),
    "PST": ZoneInfo("America/Los_Angeles"), "PDT": ZoneInfo("America/Los_Angeles"),
}


def parse_end_date(raw: str | None) -> datetime | None:
    """Normalize a scraped end_date string to aware UTC. Empty / unparseable → None.

    Naive timestamps are interpreted as US Eastern (see module docstring).
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        dt = _date_parser.parse(s, fuzzy=True, tzinfos=TZINFOS)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EASTERN)
    return dt.astimezone(timezone.utc)
