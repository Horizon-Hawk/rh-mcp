"""Composite scanner — runs 52w-high, Bollinger squeeze, and sympathy-laggard
scanners in parallel and reconciles overlapping signals.

A ticker that surfaces in TWO scans is materially higher conviction than a
ticker found in one. APLS in tonight's data showed in both squeeze (0.0%
bandwidth percentile — deepest 6-month compression) AND 52w (within 0.3% of
high) — exactly the kind of stack that justifies a real position size.

Output: tickers ranked first by signal_count desc, then by a composite
strength score within each tier. Each entry preserves all per-scanner data.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime
from pathlib import Path

from rh_mcp.analysis import (
    high_breakout,
    squeeze_breakout,
    sympathy_laggards,
)


def _flatten_sympathy(themes: list[dict]) -> list[dict]:
    """Sympathy returns {industry, leader, laggards: [...]}. Flatten to per-ticker.

    Each flattened entry carries the leader context so the user can see WHICH
    theme they're a sympathy play on.
    """
    out: list[dict] = []
    for theme in themes:
        for lag in theme.get("laggards", []):
            out.append({
                "ticker": lag["ticker"],
                "price": lag["price"],
                "pct_change_today": lag["pct_change"],
                "industry": theme["industry"],
                "sector": theme.get("sector"),
                "sympathy_leader": theme["leader"]["ticker"],
                "leader_move_pct": theme["leader_move_pct"],
                "gap_to_leader": lag["gap_to_leader"],
            })
    return out


def analyze(
    tickers: list[str] | None = None,
    universe_file: str | Path | None = None,
    top_n_per_scanner: int = 25,
    top_n_overall: int = 30,
    # 52w params
    proximity_pct: float = 2.0,
    require_spy_uptrend: bool = True,
    # Squeeze params
    compression_percentile: float = 20.0,
    proximity_upper_pct: float = 2.0,
    # Sympathy params
    leader_move_pct: float = 5.0,
    max_laggard_move_pct: float = 2.0,
) -> dict:
    """Run all three daily scanners in parallel and merge by ticker.

    Returns a structure where multi-signal tickers (showing in 2-3 scanners)
    are surfaced first with full per-scanner data. Single-signal tickers
    follow, grouped by which scanner found them.
    """
    common_kwargs = dict(tickers=tickers, universe_file=universe_file)
    started_at = datetime.now().isoformat(timespec="seconds")

    # Run all three scanners in parallel — each takes 15-45s on a 1500-ticker
    # universe; running them serially would be ~90s, parallel ~45s.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        fut_high = ex.submit(
            high_breakout.analyze,
            proximity_pct=proximity_pct,
            require_spy_uptrend=require_spy_uptrend,
            top_n=top_n_per_scanner,
            **common_kwargs,
        )
        fut_squeeze = ex.submit(
            squeeze_breakout.analyze,
            compression_percentile=compression_percentile,
            proximity_upper_pct=proximity_upper_pct,
            top_n=top_n_per_scanner,
            **common_kwargs,
        )
        fut_sympathy = ex.submit(
            sympathy_laggards.analyze,
            leader_move_pct=leader_move_pct,
            max_laggard_move_pct=max_laggard_move_pct,
            top_n=top_n_per_scanner,
            **common_kwargs,
        )
        r_high = fut_high.result()
        r_squeeze = fut_squeeze.result()
        r_sympathy = fut_sympathy.result()

    finished_at = datetime.now().isoformat(timespec="seconds")

    # Bucket by ticker
    merged: dict[str, dict] = {}

    for c in r_high.get("candidates", []):
        t = c["ticker"]
        merged.setdefault(t, {"ticker": t, "scans": []})["scans"].append("52w_high")
        merged[t]["high_breakout"] = c

    for c in r_squeeze.get("candidates", []):
        t = c["ticker"]
        merged.setdefault(t, {"ticker": t, "scans": []})["scans"].append("squeeze")
        merged[t]["squeeze"] = c

    flat_sympathy = _flatten_sympathy(r_sympathy.get("themes", []))
    for c in flat_sympathy:
        t = c["ticker"]
        merged.setdefault(t, {"ticker": t, "scans": []})["scans"].append("sympathy")
        merged[t]["sympathy"] = c

    # Compute signal_count + a composite strength score within each tier
    for t, entry in merged.items():
        entry["signal_count"] = len(entry["scans"])
        # Strength = sum of per-scanner-relative score, normalized 0-1 each
        score = 0.0
        if "high_breakout" in entry:
            # Higher volume_ratio = stronger
            score += min(entry["high_breakout"].get("volume_ratio", 1.0) / 5.0, 1.0)
        if "squeeze" in entry:
            # Lower bandwidth_pct = deeper compression = stronger.
            # Invert so 0% pct → score 1.0, 20% pct → score 0.0
            bp = entry["squeeze"].get("bandwidth_pct", 20.0)
            score += max(0.0, (20.0 - bp) / 20.0)
        if "sympathy" in entry:
            # Bigger gap_to_leader = more catch-up room = stronger
            score += min(entry["sympathy"].get("gap_to_leader", 0) / 20.0, 1.0)
        entry["composite_score"] = round(score, 2)

    # Sort: signal_count desc, then composite_score desc
    sorted_candidates = sorted(
        merged.values(),
        key=lambda e: (e["signal_count"], e["composite_score"]),
        reverse=True,
    )
    if top_n_overall and len(sorted_candidates) > top_n_overall:
        sorted_candidates = sorted_candidates[:top_n_overall]

    multi_signal = [e for e in sorted_candidates if e["signal_count"] >= 2]

    return {
        "success": True,
        "started_at": started_at,
        "finished_at": finished_at,
        "universe_size": (
            r_high.get("total_scanned")
            or r_squeeze.get("total_scanned")
            or r_sympathy.get("total_scanned")
            or 0
        ),
        "per_scanner_counts": {
            "52w_high": len(r_high.get("candidates", [])),
            "squeeze": len(r_squeeze.get("candidates", [])),
            "sympathy_laggards": len(flat_sympathy),
        },
        "multi_signal_count": len(multi_signal),
        "multi_signal_tickers": [e["ticker"] for e in multi_signal],
        "candidates": sorted_candidates,
    }
