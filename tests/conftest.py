"""Shared test fixtures."""

import json
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_alerts_file(monkeypatch, tmp_path):
    """Point RH_ALERTS_FILE at a fresh empty JSON file for the duration of a test."""
    alerts_path = tmp_path / "price_alerts.json"
    alerts_path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("RH_ALERTS_FILE", str(alerts_path))
    # Reload the module so the new env var is picked up
    import importlib
    from rh_mcp.tools import alerts as alerts_mod
    importlib.reload(alerts_mod)
    yield alerts_path
