"""Tests for position_size_helper — pure logic, no live RH calls."""

import pytest

from rh_mcp.tools.sizing import position_size_helper, RISK_TIERS


def test_basic_long_setup():
    """Standard A-grade setup with room under max position cap.
    equity $10K, entry $10, stop $8 → risk $2/share, A=3%=$300 → 150 shares × $10 = $1,500 position (under 25% cap of $2,500)."""
    r = position_size_helper(entry=10.0, stop=8.0, grade="A", equity=10_000.0)
    assert r["success"]
    assert r["shares_whole"] == 150
    assert r["risk_per_share"] == 2.0
    assert r["actual_risk_whole"] == 300.0
    assert not r["capped_at_max_position"]


def test_short_setup():
    """Stop above entry for short. Same math, under cap."""
    r = position_size_helper(entry=10.0, stop=12.0, grade="A", equity=10_000.0)
    assert r["success"]
    assert r["shares_whole"] == 150
    assert r["risk_per_share"] == 2.0


def test_max_position_cap_kicks_in():
    """Tight stop on expensive stock blows past 25% position cap.
    equity $10K, entry $200, stop $194 → risk $6/share, A=3% wants 50 shares = $10K position.
    But max position 25% = $2,500 → cap at 12 shares."""
    r = position_size_helper(entry=200.0, stop=194.0, grade="A", equity=10_000.0)
    assert r["success"]
    assert r["capped_at_max_position"]
    assert r["shares_whole"] == 12  # int($2,500 / $200) = 12
    assert r["actual_risk_pct"] < 3.0  # less than target after cap


def test_explicit_risk_pct_overrides_grade():
    """risk_pct=5 should give 5% sizing regardless of grade. entry=$10 keeps under cap."""
    r = position_size_helper(entry=10.0, stop=8.0, risk_pct=5.0, equity=10_000.0)
    assert r["success"]
    assert r["risk_pct"] == 5.0
    # 5% of $10K = $500 / $2 = 250 shares × $10 = $2,500 → right at cap
    assert r["shares_whole"] == 250


def test_grade_tiers_have_expected_values():
    assert RISK_TIERS["A+"] == 5.0
    assert RISK_TIERS["A"] == 3.0
    assert RISK_TIERS["B"] == 1.5


def test_grade_a_plus_higher_than_a():
    """A+ should give more shares than A when neither is capped."""
    # Use a wide stop to keep both under cap. entry $10, stop $7 → risk $3/share.
    # A=3% of $10K = $300 → 100 shares × $10 = $1,000 position (under $2,500 cap)
    # A+=5% = $500 → 166 shares × $10 = $1,660 position (still under cap)
    r_a = position_size_helper(entry=10.0, stop=7.0, grade="A", equity=10_000.0)
    r_a_plus = position_size_helper(entry=10.0, stop=7.0, grade="A+", equity=10_000.0)
    assert r_a_plus["shares_whole"] > r_a["shares_whole"]


def test_unknown_grade_fails():
    r = position_size_helper(entry=50.0, stop=48.0, grade="Z", equity=10_000.0)
    assert not r["success"]
    assert "unknown grade" in r["error"]


def test_missing_both_fails():
    r = position_size_helper(entry=50.0, stop=48.0, equity=10_000.0)
    assert not r["success"]


def test_entry_equals_stop_fails():
    r = position_size_helper(entry=50.0, stop=50.0, grade="A", equity=10_000.0)
    assert not r["success"]


def test_zero_equity_fails():
    r = position_size_helper(entry=50.0, stop=48.0, grade="A", equity=0.0)
    assert not r["success"]
