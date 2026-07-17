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
