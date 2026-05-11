"""Smoke test for get_quote. Requires a live rh_config.json with valid credentials.

Run with: pytest tests/test_quotes_smoke.py -v -m live

Skipped by default since most environments don't have RH credentials configured.
Set the env var RH_LIVE_TESTS=1 to enable.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("RH_LIVE_TESTS"),
    reason="set RH_LIVE_TESTS=1 to enable live Robinhood integration tests",
)

from rh_mcp.tools import quotes


def test_get_quote_single():
    """Single-ticker quote returns expected fields."""
    result = quotes.get_quote("AAPL")
    assert result["success"] is True
    assert result["count"] == 1
    q = result["quotes"][0]
    assert q["symbol"] == "AAPL"
    assert q["last_trade_price"] is not None
    assert q["bid_price"] is not None
    assert q["ask_price"] is not None


def test_get_quote_batch():
    """Batch ticker quotes return all symbols."""
    result = quotes.get_quote("AAPL,NVDA,TSLA")
    assert result["success"] is True
    assert result["count"] == 3
    symbols = {q["symbol"] for q in result["quotes"]}
    assert symbols == {"AAPL", "NVDA", "TSLA"}


def test_get_quote_empty():
    """Empty ticker string returns error."""
    result = quotes.get_quote("")
    assert result["success"] is False
