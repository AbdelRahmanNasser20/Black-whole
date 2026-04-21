"""
auction_extractors — public integration surface.

Other applications (e.g. the website) should import from here:

    from auction_extractors import get_top_chairs

    listings = get_top_chairs(source="gd", n=15, min_quantity=50)
    for it in listings:
        render(it["title"], it["quantity"], it["price"],
               it["image_url"], it["link"], it["condition"])

The function is read-only. Refreshing data is done by running the scrapers
(``govdeals_chairs_extraction.py`` / ``public_surplus_automation.py``) on
whatever cadence suits the integrator — typically via cron / ``run_all.sh``.
"""

# The top-level scripts in this directory (top_chairs, paths, listings_db, …)
# were historically written as standalone modules with flat imports. To keep
# them working both as scripts (``python top_chairs.py``) and as a package
# (``python -m auction_extractors``), we inject the directory onto sys.path
# so the bare ``from paths import …`` statements inside them still resolve.
import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
if _here not in _sys.path:
    _sys.path.insert(0, _here)

from top_chairs import get_top_chairs  # noqa: E402

__all__ = ["get_top_chairs"]
