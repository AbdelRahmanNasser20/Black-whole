# tests/deals/test_cli_sweep.py
from deals.cli import sweep_categories, DEFAULT_CATEGORIES

def test_explicit_arg_wins():
    assert sweep_categories("372,47B", {}) == ["372", "47B"]

def test_env_var_used_when_no_arg():
    assert sweep_categories(None, {"DEALS_SWEEP_CATEGORIES": "22,90"}) == ["22", "90"]

def test_env_var_all_means_whole_site():
    assert sweep_categories(None, {"DEALS_SWEEP_CATEGORIES": "all"}) == [""]

def test_default_is_curated_cluster():
    assert sweep_categories(None, {}) == DEFAULT_CATEGORIES

def test_arg_all_means_whole_site():
    assert sweep_categories("all", {}) == [""]

def test_foreign_sites_sweep_once_not_per_category():
    from deals.cli import site_categories
    cats = ["372", "47B", "47C"]
    assert site_categories("govdeals", cats) == cats
    assert site_categories("allsurplus", cats) == cats         # same maestro API
    assert site_categories("txauction", cats) == [""]
    assert site_categories("publicsurplus", cats) == [""]


def test_watch_once_and_mirror_accept_site(monkeypatch):
    import sys
    import deals.cli as cli
    seen = {}
    monkeypatch.setattr(cli, "poll_once", lambda adapter, now, extra_where=None, site="govdeals":
                        seen.update(watch=(type(adapter).__name__, site)) or "ok")
    monkeypatch.setattr(sys, "argv", ["deals.cli", "watch-once", "--site", "txauction"])
    cli.main()
    assert seen["watch"] == ("TXAuctionAdapter", "txauction")
    import deals.listings_bridge as lb
    monkeypatch.setattr(lb, "mirror", lambda site, profile, dry_run=False:
                        seen.update(mirror=(site, profile.slug, dry_run)) or "ok")
    monkeypatch.setattr(sys, "argv", ["deals.cli", "mirror-auctions", "--site", "txauction",
                                      "--profile", "chairs", "--dry-run"])
    monkeypatch.setattr(cli._profiles, "resolve", lambda slug: cli._profiles.SEED_PROFILES[slug or "chairs"])
    cli.main()
    assert seen["mirror"] == ("txauction", "chairs", True)
