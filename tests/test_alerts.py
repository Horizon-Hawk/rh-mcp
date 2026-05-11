"""Tests for the local alerts file management — file I/O, no live RH calls."""

import json
import pytest


def test_list_alerts_empty(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    r = alerts_mod.list_alerts()
    assert r["success"]
    assert r["count"] == 0


def test_add_alert(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    r = alerts_mod.add_alert("AAPL", 200.0, "above", grade="A", note="breakout")
    assert r["success"]
    assert r["added"]["ticker"] == "AAPL"
    assert r["added"]["target"] == 200.0
    assert r["added"]["direction"] == "above"
    assert r["added"]["active"] is True
    assert r["total_alerts"] == 1


def test_add_invalid_direction(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    r = alerts_mod.add_alert("AAPL", 200.0, "sideways")
    assert not r["success"]


def test_list_filters_active(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    alerts_mod.add_alert("AAPL", 200.0, "above")
    alerts_mod.add_alert("AAPL", 180.0, "below", active=False)
    r = alerts_mod.list_alerts(active_only=True)
    assert r["count"] == 1
    r_all = alerts_mod.list_alerts(active_only=False)
    assert r_all["count"] == 2


def test_list_filters_by_ticker(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    alerts_mod.add_alert("AAPL", 200.0, "above")
    alerts_mod.add_alert("NVDA", 500.0, "above")
    r = alerts_mod.list_alerts(ticker="AAPL")
    assert r["count"] == 1
    assert r["alerts"][0]["ticker"] == "AAPL"


def test_deactivate_alert(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    alerts_mod.add_alert("AAPL", 200.0, "above")
    r = alerts_mod.deactivate_alert("AAPL", target=200.0)
    assert r["success"]
    assert r["deactivated_count"] == 1
    after = alerts_mod.list_alerts(active_only=True)
    assert after["count"] == 0


def test_deactivate_all_for_ticker(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    alerts_mod.add_alert("AAPL", 200.0, "above")
    alerts_mod.add_alert("AAPL", 180.0, "below")
    alerts_mod.add_alert("NVDA", 500.0, "above")
    r = alerts_mod.deactivate_alert("AAPL")  # no target → matches all AAPL
    assert r["deactivated_count"] == 2
    after = alerts_mod.list_alerts(active_only=True)
    assert after["count"] == 1  # NVDA still active


def test_delete_alert(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    alerts_mod.add_alert("AAPL", 200.0, "above")
    r = alerts_mod.delete_alert("AAPL", 200.0, "above")
    assert r["removed_count"] == 1
    after = alerts_mod.list_alerts(active_only=False)
    assert after["count"] == 0


def test_auto_stage_trade_card_long(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    r = alerts_mod.auto_stage_trade_card(
        ticker="AAPL", entry=200.0, stop=195.0, target=220.0,
        shares=10, direction="long", grade="A",
    )
    assert r["success"]
    assert r["R"] == 5.0
    assert r["one_R"] == 205.0
    assert r["two_R"] == 210.0
    assert r["alerts_added"] == 4  # STOP + 1R + 2R + TARGET

    after = alerts_mod.list_alerts(active_only=True, ticker="AAPL")
    grades = {a["grade"] for a in after["alerts"]}
    assert grades == {"stop", "1R", "2R", "target"}


def test_auto_stage_trade_card_no_target(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    r = alerts_mod.auto_stage_trade_card(
        ticker="AAPL", entry=200.0, stop=195.0, direction="long",
    )
    assert r["success"]
    assert r["alerts_added"] == 3  # STOP + 1R + 2R, no target


def test_auto_stage_trade_card_short(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    r = alerts_mod.auto_stage_trade_card(
        ticker="AAPL", entry=200.0, stop=205.0, target=180.0,
        direction="short",
    )
    assert r["success"]
    assert r["R"] == 5.0
    assert r["one_R"] == 195.0  # short: 1R = entry - R
    assert r["two_R"] == 190.0

    after = alerts_mod.list_alerts(active_only=True, ticker="AAPL")
    stop_alert = next(a for a in after["alerts"] if a["grade"] == "stop")
    assert stop_alert["direction"] == "above"  # short stop fires when price goes ABOVE entry


def test_auto_stage_trade_card_invalid_stop_long(temp_alerts_file):
    from rh_mcp.tools import alerts as alerts_mod
    # Long with stop ABOVE entry is invalid
    r = alerts_mod.auto_stage_trade_card(ticker="AAPL", entry=200.0, stop=210.0, direction="long")
    assert not r["success"]
