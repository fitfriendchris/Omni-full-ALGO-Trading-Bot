"""
smart_trail_adapter.py — Bridge between auto_trader and smart_trailing_stop.

Why this exists: auto_trader.manage_open_trades deals in MT5-shaped dicts
(`pos`, `charts`, `rules.json`). smart_trailing_stop deals in typed
dataclasses (Position, MarketContext, TrailConfig). This adapter translates.

Integration in auto_trader.manage_open_trades (inside each pos loop):

    from smart_trail_adapter import maybe_smart_trail, is_enabled

    if is_enabled(rules):
        proposal = maybe_smart_trail(pos, charts, rules, state.last_scan_context)
        if proposal is not None:
            if proposal.should_close and ticket_str.isdigit():
                close_position(int(ticket_str))
                log.info(f"{symbol} smart_trail → close: {proposal.reason}")
                continue
            new_sl = proposal.new_sl
            # fall through to existing modify-if-moved code

Until the existing scanner populates a full MarketContext, this adapter
fabricates a minimal one from available data (bars + basic structure).
The layers that have no data simply don't fire — the profit_lock and
volatility layers still produce sensible trails from bars alone.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from smart_trailing_stop import (
    Bar, Position, MarketContext, TrailConfig, TrailProposal,
    compute_trailing_sl,
)

log = logging.getLogger(__name__)

_SCAN_CTX_PATH = Path(__file__).resolve().parent.parent / "shared" / "scan_context.json"


def is_enabled(rules: dict) -> bool:
    """Check the rules.json smart_trail.enabled flag. Safe if missing."""
    try:
        return bool(rules.get("smart_trail", {}).get("enabled", False))
    except Exception:
        return False


def _build_config(rules: dict) -> TrailConfig:
    """Load TrailConfig from rules.json with safe fallbacks."""
    st = rules.get("smart_trail", {}) or {}
    ladder = tuple(tuple(pair) for pair in st.get("profit_lock_ladder",
        [[1.0, 0.0], [1.5, 0.3], [2.0, 1.0], [3.0, 1.8], [5.0, 3.5]]))
    return TrailConfig(
        atr_period                  = int(st.get("atr_period", 14)),
        atr_mult_min                = float(st.get("atr_mult_min", 1.2)),
        atr_mult_runner             = float(st.get("atr_mult_runner", 1.8)),
        exhaustion_tighten_atr_mult = float(st.get("exhaustion_tighten_atr_mult", 0.8)),
        displacement_loosen_atr_mult= float(st.get("displacement_loosen_atr_mult", 2.2)),
        structure_buffer_pips       = float(st.get("structure_buffer_pips", 2.0)),
        liquidity_avoid_pips        = float(st.get("liquidity_avoid_pips", 3.0)),
        avoid_equal_levels          = bool(st.get("avoid_equal_levels", True)),
        profit_lock_ladder          = ladder,
        close_on_opposing_choch_once_profitable = bool(
            st.get("close_on_opposing_choch_once_profitable", True)),
        min_modify_pips             = float(st.get("min_modify_pips", 3.0)),
        min_modify_atr_frac         = float(st.get("min_modify_atr_frac", 0.15)),
    )


_MAX_TRAIL_BARS = 200  # only recent bars matter for ATR / swing detection


def _bars_from_chart(chart: dict, tf_key: str) -> list[Bar]:
    """Extract bars for a given timeframe from the MT5 export chart dict."""
    if not isinstance(chart, dict):
        return []
    raw = chart.get(tf_key) or chart.get(tf_key.lower()) or []
    raw = raw[:_MAX_TRAIL_BARS]   # cap to avoid 5k-bar processing per position
    out: list[Bar] = []
    for b in raw:
        try:
            out.append(Bar(
                time=float(b.get("time", b.get("t", 0))),
                open=float(b.get("open", b.get("o", 0))),
                high=float(b.get("high", b.get("h", 0))),
                low=float(b.get("low", b.get("l", 0))),
                close=float(b.get("close", b.get("c", 0))),
            ))
        except (TypeError, ValueError):
            continue
    return out


def _build_context(pos: dict, charts: dict, scan_context: Optional[dict]) -> MarketContext:
    """
    Compose a MarketContext from:
      - `pos` — the open-position dict from omni_data.json
      - `charts` — the charts dict from omni_data.json (per-symbol bar arrays)
      - `scan_context` — optional, populated by ict_precision on last scan,
        shape { symbol: { "last_swing_high_m15": x, "opposing_choch_h1": bool, ... } }

    Any field absent is left None; the engine simply won't fire that layer.
    """
    symbol = pos.get("symbol", "")
    chart  = charts.get(symbol, {}) if isinstance(charts, dict) else {}

    # ── scan_context injection from orchestrator's latest ICT scan ────
    # orchestrator writes a per-symbol scan blob to shared/scan_context.json
    # on every cycle. If the file exists and is fresh (< 90s), load it and
    # merge it into the adapter's context so smart_trail gets real structure
    # (swing highs / lows / CHoCH flags) instead of empty fallbacks.
    _scan_blob: dict = {}
    _scan_age_s: float = float("inf")
    try:
        if _SCAN_CTX_PATH.exists():
            _scan_raw = json.loads(_SCAN_CTX_PATH.read_text())
            _scan_ts = datetime.fromisoformat(_scan_raw.get("ts", "1970-01-01T00:00:00+00:00"))
            _scan_age_s = (datetime.now(timezone.utc) - _scan_ts).total_seconds()
            if _scan_age_s < 90:
                _scan_blob = _scan_raw.get("data", {})
    except Exception:
        pass

    # Prefer orchestrator scan context over stale auto_trader last_scan_context
    if _scan_age_s < 90 and symbol in _scan_blob:
        scan_context = _scan_blob

    bars_m15 = _bars_from_chart(chart, "M15")
    bars_h1  = _bars_from_chart(chart, "H1")

    ctx_for_symbol = (scan_context or {}).get(symbol, {}) if isinstance(scan_context, dict) else {}

    return MarketContext(
        bars_m15=bars_m15,
        bars_h1=bars_h1,
        last_swing_high_m15=ctx_for_symbol.get("last_swing_high_m15"),
        last_swing_low_m15 =ctx_for_symbol.get("last_swing_low_m15"),
        last_swing_high_h1 =ctx_for_symbol.get("last_swing_high_h1"),
        last_swing_low_h1  =ctx_for_symbol.get("last_swing_low_h1"),
        opposing_choch_h1  =bool(ctx_for_symbol.get("opposing_choch_h1", False)),
        opposing_choch_m15 =bool(ctx_for_symbol.get("opposing_choch_m15", False)),
        equal_highs        =list(ctx_for_symbol.get("equal_highs", []) or []),
        equal_lows         =list(ctx_for_symbol.get("equal_lows",  []) or []),
        session_high       =ctx_for_symbol.get("session_high"),
        session_low        =ctx_for_symbol.get("session_low"),
        pdh                =ctx_for_symbol.get("pdh"),
        pdl                =ctx_for_symbol.get("pdl"),
        exhaustion_at_level=bool(ctx_for_symbol.get("exhaustion_at_level", False)),
        displacement_with  =bool(ctx_for_symbol.get("displacement_with", False)),
    )


def _build_position(pos: dict, pip_size: float) -> Optional[Position]:
    """Translate MT5 position dict into typed Position. Returns None if unusable."""
    try:
        direction = pos.get("type", "").upper()
        if direction not in ("BUY", "SELL"):
            return None
        # MT5 exports "open_price"; legacy code may use "entry". Accept both.
        entry = float(pos.get("open_price", pos.get("entry", 0)))
        sl    = float(pos.get("sl", 0))
        cur   = float(pos.get("current_price", 0))
        if entry <= 0 or sl <= 0 or cur <= 0:
            return None
        return Position(
            direction=direction,
            entry=entry,
            current_sl=sl,
            current_price=cur,
            tp1=float(pos.get("tp", 0)),  # broker reports only one tp; our state has the ladder
            pip_size=pip_size,
            symbol=pos.get("symbol", ""),
        )
    except (TypeError, ValueError):
        return None


def maybe_smart_trail(
    pos: dict,
    charts: dict,
    rules: dict,
    scan_context: Optional[dict] = None,
) -> Optional[TrailProposal]:
    """
    Entry point from auto_trader. Returns TrailProposal or None on error/missing data.
    Caller is responsible for:
      * acting on proposal.should_close (close the position)
      * comparing proposal.new_sl to current SL and calling modify_position
      * logging proposal.reason / proposal.layers_fired
    """
    if not is_enabled(rules):
        return None

    symbol = pos.get("symbol", "")
    sym_info = (charts or {}).get(symbol, {}) if isinstance(charts, dict) else {}
    pip_size = float(sym_info.get("point", 0.0001)) * 10

    position = _build_position(pos, pip_size)
    if position is None:
        return None

    context = _build_context(pos, charts or {}, scan_context)
    cfg     = _build_config(rules)
    return compute_trailing_sl(position, context, cfg)


# ──────────────────────────────────────────────────────────────────────────────
# Smoke test: run `python smart_trail_adapter.py` for a minimal end-to-end sanity
# ──────────────────────────────────────────────────────────────────────────────

def _smoke() -> None:
    rules = {"smart_trail": {"enabled": True}}
    charts = {
        "EURUSD": {
            "point": 0.00001,
            "M15": [{"time": i, "open": 1.10+i*0.0002, "high": 1.1005+i*0.0002,
                     "low":  1.0995+i*0.0002, "close": 1.1003+i*0.0002}
                    for i in range(30)],
            "H1": [],
        }
    }
    pos = {"symbol": "EURUSD", "type": "BUY", "open_price": 1.1020,
           "sl": 1.1000, "current_price": 1.1050, "tp": 1.1080}

    scan_ctx = {"EURUSD": {"last_swing_low_m15": 1.1015, "last_swing_low_h1": 1.1005,
                           "equal_lows": [1.1012]}}

    prop = maybe_smart_trail(pos, charts, rules, scan_ctx)
    assert prop is not None and prop.new_sl > 1.1000, f"expected ratchet, got {prop}"
    print(f"[SMOKE PASS] BUY EURUSD @ +1.5R → SL {prop.new_sl:.5f} | layers={prop.layers_fired}")

    # Disabled flag path
    rules["smart_trail"]["enabled"] = False
    assert maybe_smart_trail(pos, charts, rules, scan_ctx) is None, "should no-op when disabled"
    print("[SMOKE PASS] Disabled flag → adapter returns None (safe no-op)")

    # Malformed position path
    bad = {"symbol": "EURUSD", "type": "", "open_price": 0, "sl": 0, "current_price": 0}
    rules["smart_trail"]["enabled"] = True
    assert maybe_smart_trail(bad, charts, rules, scan_ctx) is None
    print("[SMOKE PASS] Malformed position → adapter returns None (safe no-op)")


if __name__ == "__main__":
    _smoke()
