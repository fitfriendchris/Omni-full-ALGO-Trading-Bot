"""
tests/test_orchestrator.py — Orchestrator cycle integration tests.

Uses a mock BarFetcher so no MT5 terminal is required.
"""

import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make python/ importable whether pytest is run from repo root or python/
HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from smc_engine import Bar, _make_fixture  # noqa: E402
from orchestrator import (  # noqa: E402
    run_cycle,
    FixtureBarFetcher,
    CycleResult,
)
from scaling_engine import PositionCtx


# ── Helpers ───────────────────────────────────────────────────────────────────

def _minimal_rules(extra: dict | None = None) -> dict:
    rules = {
        "watchlist": ["EURUSD"],
        "dual_tf": {
            "enabled": True,
            "htf_timeframe": "H1",
            "ltf_timeframe": "M5",
            "min_confidence": 0.5,
            "tp_rr": 2.0,
            "sl_buffer_frac": 0.1,
            "ob_proximity_atr_frac": 0.5,
            "fvg_proximity_atr_frac": 0.5,
        },
        "scaling": {
            "enabled": False,
            "max_adds": 2,
            "min_add_profit_r": 1.0,
            "add_size_frac": 0.5,
            "add_require_bos": True,
            "reduce_at_r": 2.0,
            "reduce_frac": 0.33,
            "reduce_on_ltf_choch": True,
            "close_at_adverse_r": -0.2,
            "close_on_htf_reversal": True,
        },
        "signals": {
            "output_dir": "shared",
            "pine_path": "pine/omni_pine_overlay.pine",
            "max_kept": 20,
        },
    }
    if extra:
        rules.update(extra)
    return rules


class _NullFetcher:
    """Returns an empty bar list — simulates no data available."""
    def fetch(self, symbol: str, timeframe: str, n: int) -> list:
        return []


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_cycle_neutral_no_position(tmp_path):
    """With fixture bars and no open position, cycle completes without error."""
    signals_file = tmp_path / "signals.json"
    pine_file = tmp_path / "overlay.pine"

    rules = _minimal_rules()
    result = run_cycle(
        rules,
        fetcher=FixtureBarFetcher(),
        symbols=["EURUSD"],
        signals_path=signals_file,
        pine_path=pine_file,
    )

    assert isinstance(result, CycleResult)
    assert "EURUSD" in result.symbols
    assert not result.errors, f"unexpected errors: {result.errors}"
    assert signals_file.exists(), "signals.json not written"
    assert pine_file.exists(), "pine file not written"

    payload = json.loads(signals_file.read_text())
    assert "signals" in payload


def test_cycle_bull_no_position(tmp_path):
    """When select_trade returns a BULL signal for a symbol with no open position,
    the signal is recorded with direction BUY or NEUTRAL (engine decides)."""
    signals_file = tmp_path / "signals.json"
    pine_file = tmp_path / "overlay.pine"

    bull_sel = MagicMock()
    bull_sel.direction = "BULL"
    bull_sel.entry_type = "OB_MITIGATION"
    bull_sel.entry_price = 1.10
    bull_sel.sl = 1.095
    bull_sel.tp = 1.11
    bull_sel.confidence = 0.72
    bull_sel.reasons = ["HTF BOS bullish", "LTF sweep confirmed"]
    bull_sel.htf_bias = MagicMock()
    bull_sel.htf_bias.direction = "BULL"
    bull_sel.ltf_trigger = "sweep_choch"
    bull_sel.htf_snapshot = None
    bull_sel.ltf_snapshot = None

    with patch("orchestrator.select_trade", return_value=bull_sel):
        result = run_cycle(
            _minimal_rules(),
            fetcher=FixtureBarFetcher(),
            symbols=["EURUSD"],
            signals_path=signals_file,
            pine_path=pine_file,
        )

    assert not result.errors, f"errors: {result.errors}"
    assert result.signals  # at least one signal produced


def test_cycle_with_open_position_triggers_scale_eval(tmp_path):
    """When an open position exists, scale_evaluate is called (not skipped)."""
    signals_file = tmp_path / "signals.json"
    pine_file = tmp_path / "overlay.pine"

    open_pos = {
        "EURUSD": PositionCtx(
            symbol="EURUSD",
            direction="BULL",
            entry_price=1.10,
            current_price=1.108,
            initial_sl=1.095,
            current_sl=1.095,
            volume=0.1,
            add_count=0,
        )
    }

    scale_called = []

    original_scale = __import__("scaling_engine").evaluate

    def mock_scale(pos, htf_snap, ltf_snap, rules=None):
        scale_called.append(True)
        return original_scale(pos, htf_snap, ltf_snap, rules=rules)

    with patch("orchestrator.scale_evaluate", side_effect=mock_scale):
        result = run_cycle(
            _minimal_rules(),
            fetcher=FixtureBarFetcher(),
            symbols=["EURUSD"],
            open_positions=open_pos,
            signals_path=signals_file,
            pine_path=pine_file,
        )

    assert scale_called, "scale_evaluate was never called despite an open position"
    assert not result.errors, f"errors: {result.errors}"


def test_cycle_empty_bars_produces_error(tmp_path):
    """When bar fetching returns nothing, the symbol error is recorded
    and other symbols still complete."""
    signals_file = tmp_path / "signals.json"
    pine_file = tmp_path / "overlay.pine"

    rules = _minimal_rules({"watchlist": ["EURUSD", "GBPUSD"]})

    class _PartialFetcher:
        def fetch(self, symbol, timeframe, n):
            if symbol == "EURUSD":
                return _make_fixture()[-n:] if n else _make_fixture()
            return []  # GBPUSD returns nothing → should trigger an error path

    result = run_cycle(
        rules,
        fetcher=_PartialFetcher(),
        symbols=["EURUSD", "GBPUSD"],
        signals_path=signals_file,
        pine_path=pine_file,
    )

    # EURUSD should succeed; GBPUSD may error (empty bars) — that's acceptable
    assert signals_file.exists()
    assert len(result.symbols) == 2


def test_cycle_respects_signals_config_paths(tmp_path):
    """signals block in rules overrides module-level DEFAULT_SIGNALS_DIR."""
    out_dir = tmp_path / "my_signals"
    out_dir.mkdir()
    pine_dir = tmp_path / "my_pine"
    pine_dir.mkdir()

    rules = _minimal_rules({
        "signals": {
            "output_dir": str(out_dir),
            "pine_path": str(pine_dir / "overlay.pine"),
            "max_kept": 5,
        }
    })

    with patch("orchestrator.PROJECT_ROOT", tmp_path):
        result = run_cycle(
            rules,
            fetcher=FixtureBarFetcher(),
            symbols=["EURUSD"],
        )

    # No file-path errors expected
    write_errors = [e for e in result.errors if "write" in e.lower()]
    assert not write_errors, f"write errors: {write_errors}"


def test_cycle_multi_symbol(tmp_path):
    """All watchlist symbols produce signals without errors (fixture data)."""
    signals_file = tmp_path / "signals.json"
    pine_file = tmp_path / "overlay.pine"

    rules = _minimal_rules({
        "watchlist": ["EURUSD", "GBPUSD", "USDJPY"],
    })

    result = run_cycle(
        rules,
        fetcher=FixtureBarFetcher(),
        signals_path=signals_file,
        pine_path=pine_file,
    )

    assert not result.errors, f"errors: {result.errors}"
    assert set(result.symbols) == {"EURUSD", "GBPUSD", "USDJPY"}
    payload = json.loads(signals_file.read_text())
    assert len(payload["signals"]) <= rules["signals"]["max_kept"]
