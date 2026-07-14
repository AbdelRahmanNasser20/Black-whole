"""PUBLICSURPLUS_ALLOW_BROWSER=0 must degrade, never launch Chromium.

The discovery cron runs PS in the cloud image, which ships no Chromium on
purpose (see Dockerfile). Every browser fallback in the PS scraper therefore
has to be gated: if one of them fires there it raises "Executable doesn't
exist" and we lose the whole day of PS rows. These tests pin that contract.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import public_surplus_automation as ps


class BrowserFallbackGuardTest(unittest.TestCase):
    def test_defaults_on_so_local_runs_are_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(ps._browser_fallback_enabled())

    def test_disabled_by_env(self):
        with mock.patch.dict(os.environ, {"PUBLICSURPLUS_ALLOW_BROWSER": "0"}):
            self.assertFalse(ps._browser_fallback_enabled())

    def test_scrape_returns_empty_instead_of_launching_browser(self):
        """HTTP yielding nothing must NOT reach the Playwright path when the
        guard is off — that call is what explodes in the cloud image."""
        env = {"PUBLICSURPLUS_USE_API": "1", "PUBLICSURPLUS_ALLOW_BROWSER": "0"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ps, "scrape_listings_via_http", return_value=[]), \
                mock.patch.object(ps, "scrape_listings_via_browser") as browser:
            self.assertEqual(ps.scrape_listings(), [])
            browser.assert_not_called()

    def test_scrape_still_falls_back_to_browser_when_allowed(self):
        env = {"PUBLICSURPLUS_USE_API": "1", "PUBLICSURPLUS_ALLOW_BROWSER": "1"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ps, "scrape_listings_via_http", return_value=[]), \
                mock.patch.object(ps, "scrape_listings_via_browser",
                                  return_value=[{"title": "x"}]) as browser:
            self.assertEqual(ps.scrape_listings(), [{"title": "x"}])
            browser.assert_called_once()

    def test_failed_description_fetch_keeps_listing_without_browser(self):
        """A flaky detail fetch must cost that row's description, not the run."""
        env = {
            "FETCH_PUBLIC_SURPLUS_DESCRIPTION": "1",
            "PUBLICSURPLUS_USE_API": "1",
            "PUBLICSURPLUS_ALLOW_BROWSER": "0",
            "FETCH_PUBLIC_SURPLUS_DELAY_SEC": "0",
        }
        listings = [{"title": "40 Black Banquet Chairs", "link": "https://ps/x",
                     "quantity": 40}]
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ps.listings_db, "hydrate_from_cache",
                                  return_value=(0, 0, 0)), \
                mock.patch.object(ps.requests, "get",
                                  side_effect=RuntimeError("boom")), \
                mock.patch.object(ps, "sync_playwright") as pw:
            out = ps.enrich_listings_with_descriptions(listings)
            pw.assert_not_called()
            self.assertEqual(out[0]["description"], "")
            self.assertEqual(out[0]["quantity"], 40)


if __name__ == "__main__":
    unittest.main()
