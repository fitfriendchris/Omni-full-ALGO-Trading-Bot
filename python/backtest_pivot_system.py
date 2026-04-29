"""
Backtest Pivot System

Walk-forward backtest harness for the pivot + structure + ICT pipeline.
For each historical bar in the loaded dataset:
1. Compute pivots across multiple timeframes
2. Detect ICT patterns (mocked via passed-in detector or skipped)
3. Apply pivot_confidence_booster
4. Simulate entry/exit using next N bars (default: 50 bar window)
5. Record outcome to feature_store + backtest_results.json

Outputs:
- pivot_backtest_results.json — aggregate metrics
- structure_validation.json — pivots-on-structure vs isolated comparison
- per-symbol breakdown
- per-timeframe breakdown
- regime breakdown

Public API:
- run_backtest(bars_by_symbol_tf, ict_detector=None, ...) → BacktestResult
"""

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any
import logging

from pivot_engine import (
    Bar, calculate_pivots, score_pivot_strength,
    detect_multi_tf_confluence, identify_reversal_probability,
    find_nearest_pivot_level,
)
from market_structure_validator import validate_pivot_on_structure

log = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    symbol: str
    timeframe: str
    direction: str
    entry_idx: int
    entry_price: float
    sl: float
    tp: float
    pivot_level: float
    pivot_type: str
    pivot_distance: float
    structure_type: str
    structure_boost: int
    confluence_count: int
    reversal_probability: float
    # Outcome
    exit_idx: int = -1
    exit_price: float = 0.0
    outcome: str = "OPEN"      # WIN / LOSS / TIMEOUT
    r_multiple: float = 0.0
    bars_held: int = 0


@dataclass
class BacktestResult:
    trades:                List[BacktestTrade] = field(default_factory=list)
    total_trades:          int = 0
    wins:                  int = 0
    losses:                int = 0
    timeouts:              int = 0
    win_rate:              float = 0.0
    avg_r:                 float = 0.0
    total_r:               float = 0.0
    profit_factor:         float = 0.0
    max_drawdown_r:        float = 0.0
    by_structure:          Dict[str, Dict] = field(default_factory=dict)
    by_symbol:             Dict[str, Dict] = field(default_factory=dict)
    by_timeframe:          Dict[str, Dict] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────
# Entry & exit simulation
# ─────────────────────────────────────────────────────────────────────────

def _simulate_trade(
    bars: List[Bar],
    entry_idx: int,
    direction: str,
    entry_price: float,
    sl: float,
    tp: float,
    max_holding: int = 50,
) -> BacktestTrade:
    """Walk forward `max_holding` bars; return the first SL/TP hit or timeout."""
    end = min(entry_idx + 1 + max_holding, len(bars))
    for i in range(entry_idx + 1, end):
        b = bars[i]
        if direction == "BUY":
            # Check SL first (conservative — assume SL hits before TP if both in same bar)
            if b.low <= sl:
                return BacktestTrade(
                    symbol="", timeframe="", direction=direction,
                    entry_idx=entry_idx, entry_price=entry_price, sl=sl, tp=tp,
                    pivot_level=0, pivot_type="", pivot_distance=0,
                    structure_type="", structure_boost=0,
                    confluence_count=0, reversal_probability=0,
                    exit_idx=i, exit_price=sl, outcome="LOSS",
                    r_multiple=-1.0, bars_held=i - entry_idx,
                )
            if b.high >= tp:
                rr = (tp - entry_price) / max(0.0001, (entry_price - sl))
                return BacktestTrade(
                    symbol="", timeframe="", direction=direction,
                    entry_idx=entry_idx, entry_price=entry_price, sl=sl, tp=tp,
                    pivot_level=0, pivot_type="", pivot_distance=0,
                    structure_type="", structure_boost=0,
                    confluence_count=0, reversal_probability=0,
                    exit_idx=i, exit_price=tp, outcome="WIN",
                    r_multiple=rr, bars_held=i - entry_idx,
                )
        else:  # SELL
            if b.high >= sl:
                return BacktestTrade(
                    symbol="", timeframe="", direction=direction,
                    entry_idx=entry_idx, entry_price=entry_price, sl=sl, tp=tp,
                    pivot_level=0, pivot_type="", pivot_distance=0,
                    structure_type="", structure_boost=0,
                    confluence_count=0, reversal_probability=0,
                    exit_idx=i, exit_price=sl, outcome="LOSS",
                    r_multiple=-1.0, bars_held=i - entry_idx,
                )
            if b.low <= tp:
                rr = (entry_price - tp) / max(0.0001, (sl - entry_price))
                return BacktestTrade(
                    symbol="", timeframe="", direction=direction,
                    entry_idx=entry_idx, entry_price=entry_price, sl=sl, tp=tp,
                    pivot_level=0, pivot_type="", pivot_distance=0,
                    structure_type="", structure_boost=0,
                    confluence_count=0, reversal_probability=0,
                    exit_idx=i, exit_price=tp, outcome="WIN",
                    r_multiple=rr, bars_held=i - entry_idx,
                )

    # Timeout — close at last bar
    last = bars[end - 1]
    if direction == "BUY":
        rr = (last.close - entry_price) / max(0.0001, (entry_price - sl))
    else:
        rr = (entry_price - last.close) / max(0.0001, (sl - entry_price))
    return BacktestTrade(
        symbol="", timeframe="", direction=direction,
        entry_idx=entry_idx, entry_price=entry_price, sl=sl, tp=tp,
        pivot_level=0, pivot_type="", pivot_distance=0,
        structure_type="", structure_boost=0,
        confluence_count=0, reversal_probability=0,
        exit_idx=end - 1, exit_price=last.close, outcome="TIMEOUT",
        r_multiple=rr, bars_held=end - 1 - entry_idx,
    )


# ─────────────────────────────────────────────────────────────────────────
# Backtest engine
# ─────────────────────────────────────────────────────────────────────────

def run_backtest(
    bars_by_symbol_tf: Dict[str, Dict[str, List[Bar]]],
    ict_detector: Optional[Callable] = None,
    primary_tf: str = "M15",
    rr_target: float = 2.0,
    sl_atr_mult: float = 1.5,
    max_holding: int = 50,
    entry_alignment_pips: float = 0.0010,
    output_path: Optional[Path] = None,
) -> BacktestResult:
    """
    Args:
        bars_by_symbol_tf: {symbol: {tf: [Bar]}}
        ict_detector: optional callable(bars, tf) -> ict_patterns dict.
                      If None, structure validation is skipped.
        primary_tf: timeframe on which we generate entries
        rr_target: TP at rr_target × risk
        sl_atr_mult: SL = entry ± sl_atr_mult × ATR
        max_holding: max bars held before timeout
        entry_alignment_pips: required distance between price and pivot for entry
        output_path: where to write results json

    Returns: BacktestResult.
    """
    result = BacktestResult()

    for symbol, tf_bars in bars_by_symbol_tf.items():
        primary = tf_bars.get(primary_tf, [])
        if len(primary) < 30:
            continue
        # Burn-in: need 20+ bars before generating signals
        for i in range(20, len(primary) - max_holding - 1):
            bar = primary[i]
            window = {tf: bars[: i + 1] for tf, bars in tf_bars.items() if bars}
            # 1. Compute pivots on multiple TFs
            pivots_by_tf: Dict[str, Any] = {}
            for tf in ["M15", "H1", "H4", "D1"]:
                tf_window = window.get(tf, [])
                if len(tf_window) < 1:
                    continue
                try:
                    pivots = calculate_pivots(tf_window, symbol, tf)
                    if pivots:
                        pivots_by_tf[tf] = pivots
                except Exception:
                    continue
            if not pivots_by_tf:
                continue

            # 2. Find nearest pivot to current price
            flat = []
            for tf, pivots_by_type in pivots_by_tf.items():
                for ptype, levels in pivots_by_type.items():
                    flat.extend(levels)
            nearest, distance = find_nearest_pivot_level(
                flat, bar.close, max_distance_pips=entry_alignment_pips
            )
            if nearest is None:
                continue

            # 3. Determine direction (pivot ABOVE = SELL, BELOW = BUY)
            if nearest.level > bar.close:
                direction = "SELL"
                entry = nearest.level
            else:
                direction = "BUY"
                entry = nearest.level

            # 4. Compute SL/TP using simple ATR proxy (range of last 14 bars)
            recent = primary[max(0, i - 14): i + 1]
            atr = (sum(b.high - b.low for b in recent) / max(1, len(recent)))
            if atr <= 0:
                continue
            risk = atr * sl_atr_mult
            if direction == "BUY":
                sl = entry - risk
                tp = entry + (risk * rr_target)
            else:
                sl = entry + risk
                tp = entry - (risk * rr_target)

            # 5. Get reversal probability + structure boost
            reversal_prob = identify_reversal_probability(
                window.get(nearest.timeframe) or primary[: i + 1],
                pivot_level=nearest.level,
                direction="UP" if direction == "BUY" else "DOWN",
                atr=atr,
            )
            confluence_map = detect_multi_tf_confluence(pivots_by_tf, tolerance_pips=entry_alignment_pips)
            confluence_count = confluence_map.get(nearest.level, 0)

            structure_type, structure_boost = "NONE", 0
            if ict_detector:
                try:
                    patterns = ict_detector(window, primary_tf)
                    structure_type, structure_boost, _, _ = validate_pivot_on_structure(
                        nearest.level, patterns, symbol, nearest.timeframe, atr
                    )
                except Exception:
                    pass

            # 6. Simulate the trade
            trade = _simulate_trade(primary, i, direction, entry, sl, tp, max_holding)
            trade.symbol = symbol
            trade.timeframe = primary_tf
            trade.pivot_level = nearest.level
            trade.pivot_type = f"{nearest.pivot_type}/{nearest.level_type}"
            trade.pivot_distance = distance
            trade.structure_type = structure_type
            trade.structure_boost = structure_boost
            trade.confluence_count = confluence_count
            trade.reversal_probability = reversal_prob
            result.trades.append(trade)

    _aggregate(result)
    if output_path:
        _save_result(result, output_path)
    return result


# ─────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────

def _aggregate(result: BacktestResult) -> None:
    trades = result.trades
    result.total_trades = len(trades)
    result.wins = sum(1 for t in trades if t.outcome == "WIN")
    result.losses = sum(1 for t in trades if t.outcome == "LOSS")
    result.timeouts = sum(1 for t in trades if t.outcome == "TIMEOUT")
    if result.total_trades:
        result.win_rate = result.wins / result.total_trades
        result.avg_r = sum(t.r_multiple for t in trades) / result.total_trades
        result.total_r = sum(t.r_multiple for t in trades)
        wins_r = sum(t.r_multiple for t in trades if t.r_multiple > 0)
        loss_r = abs(sum(t.r_multiple for t in trades if t.r_multiple < 0))
        result.profit_factor = wins_r / loss_r if loss_r > 0 else 0.0

        # Drawdown
        eq = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            eq += t.r_multiple
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd
        result.max_drawdown_r = max_dd

    # Breakdowns
    for t in trades:
        for bucket, key in (
            (result.by_structure, t.structure_type or "NONE"),
            (result.by_symbol, t.symbol),
            (result.by_timeframe, t.timeframe),
        ):
            d = bucket.setdefault(key, {"trades": 0, "wins": 0, "losses": 0, "total_r": 0.0})
            d["trades"] += 1
            d["wins"]   += int(t.outcome == "WIN")
            d["losses"] += int(t.outcome == "LOSS")
            d["total_r"] += t.r_multiple
    for bucket in (result.by_structure, result.by_symbol, result.by_timeframe):
        for k, d in bucket.items():
            d["win_rate"] = round(d["wins"] / max(1, d["trades"]), 3)
            d["avg_r"]    = round(d["total_r"] / max(1, d["trades"]), 3)


def _save_result(result: BacktestResult, path: Path) -> None:
    try:
        path = Path(path)
        data = asdict(result)
        # Convert trade dataclasses
        path.write_text(json.dumps(data, indent=2, default=str))
        log.info(f"backtest results saved to {path}")
    except Exception as e:
        log.error(f"backtest save error: {e}")
