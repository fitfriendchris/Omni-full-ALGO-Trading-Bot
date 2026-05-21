"""
smart_trail_adapter.py — Bridge between auto_trader / swarm and smart_trailing_stop V2.

Translates MT5 position dicts + chart data + rules.json → typed Position/MarketContext.
V2 adds: live spread injection, equity awareness, adaptive ATR multipliers.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from smart_trailing_stop import (
    Bar, Position, MarketContext, TrailConfig, TrailProposal,
    compute_trailing_sl,
)

log = logging.getLogger(__name__)

_SCAN_CTX_PATH = Path(__file__).resolve().parent.parent / "shared" / "scan_context.json"


def is_enabled(rules: dict) -> bool:
    try:
        return bool(rules.get("smart_trail", {}).get("enabled", True))  # default ON now
    except Exception:
        return True


def _build_config(rules: dict, symbol: str = "") -> TrailConfig:
    st = rules.get("smart_trail", {}) or {}
    ladder_vals = st.get("profit_lock_ladder",
        [[1.0, 0.0], [1.5, 0.25], [2.0, 0.50], [3.0, 0.60], [5.0, 0.75]])
    ladder = tuple(tuple(pair) for pair in ladder_vals)
    kwargs = dict(
        atr_period                  = int(st.get("atr_period", 14)),
        atr_mult_min                = float(st.get("atr_mult_min", 1.5)),
        atr_mult_compress           = float(st.get("atr_mult_compress", 0.6)),
        atr_mult_expand             = float(st.get("atr_mult_expand", 2.5)),
        atr_mult_runner             = float(st.get("atr_mult_runner", 2.5)),
        structure_buffer_pips       = float(st.get("structure_buffer_pips", 3.0)),
        liquidity_avoid_pips        = float(st.get("liquidity_avoid_pips", 3.0)),
        avoid_equal_levels          = bool(st.get("avoid_equal_levels", True)),
        profit_lock_ladder          = ladder,
        tight_equity_threshold      = float(st.get("tight_equity_threshold", 5.0)),
        tight_mult_compress         = float(st.get("tight_mult_compress", 0.8)),
        spread_atr_frac             = float(st.get("spread_atr_frac", 0.15)),
        close_on_opposing_choch_once_profitable = bool(
            st.get("close_on_opposing_choch_once_profitable", True)),
        min_modify_pips             = float(st.get("min_modify_pips", 3.0)),
        min_modify_atr_frac         = float(st.get("min_modify_atr_frac", 0.15)),
    )
    # Symbol-specific overrides from rules.json
    sym_key = symbol.upper().replace("/", "") + "_ADJUSTMENTS"
    sym_st = st.get(sym_key.lower()) or st.get(sym_key)
    if isinstance(sym_st, dict):
        for key in ("atr_period", "atr_mult_min", "atr_mult_runner"):
            if key in sym_st:
                val = sym_st[key]
                if key in ("atr_mult_min", "atr_mult_runner", "atr_mult_compress",
                           "atr_mult_expand", "structure_buffer_pips", "liquidity_avoid_pips",
                           "tight_equity_threshold", "tight_mult_compress", "spread_atr_frac",
                           "min_modify_pips", "min_modify_atr_frac"):
                    val = float(val)
                elif key == "atr_period":
                    val = int(val)
                kwargs[key] = val
    return TrailConfig(**kwargs)


_MAX_TRAIL_BARS = 200


def _bars_from_chart(chart: dict, tf_key: str) -> list[Bar]:
    if not isinstance(chart, dict):
        return []
    raw = chart.get(tf_key) or chart.get(tf_key.lower()) or []
    raw = raw[:_MAX_TRAIL_BARS]
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


def _pip_size_for(symbol: str) -> float:
    """Return pip display size (1 pip = 0.01 for JPY pairs, 0.0001 for most FX, 0.01 for metals)."""
    s = symbol.upper()
    if "JPY" in s:
        return 0.01
    if s in ("XAUUSD", "GOLD"):
        return 0.01  # metals: 1 pip = $0.01 for gold
    if s in ("XAGUSD", "SILVER"):
        return 0.001
    return 0.0001


def _tick_size_for(symbol: str, charts: dict) -> float:
    """Read broker tick size from MT5 data."""
    chart = charts.get(symbol, {}) if isinstance(charts, dict) else {}
    tick = chart.get("tick_size")
    if tick is not None:
        return float(tick)
    s = symbol.upper()
    if s in ("XAUUSD", "GOLD"):
        return 0.01
    if s in ("XAGUSD", "SILVER"):
        return 0.001
    if "JPY" in s:
        return 0.001  # JPY pairs often 3 decimals
    return 1e-05


def _build_context(pos: dict, charts: dict, scan_ctx: Optional[dict], account_info: dict) -> MarketContext:
    symbol = pos.get("symbol", "")
    chart = charts.get(symbol, {}) if isinstance(charts, dict) else {}

    _scan_blob: dict = {}
    _scan_age = float("inf")
    try:
        if _SCAN_CTX_PATH.exists():
            raw = json.loads(_SCAN_CTX_PATH.read_text())
            ts = datetime.fromisoformat(raw.get("ts", "1970-01-01T00:00:00+00:00"))
            _scan_age = (datetime.now(timezone.utc) - ts).total_seconds()
            if _scan_age < 90:
                _scan_blob = raw.get("data", {})
    except Exception:
        pass

    if _scan_age < 90 and symbol in _scan_blob:
        scan_ctx = _scan_blob

    bars_m15 = _bars_from_chart(chart, "M15")
    bars_h1  = _bars_from_chart(chart, "H1")

    ctx_for = (scan_ctx or {}).get(symbol, {}) if isinstance(scan_ctx, dict) else {}

    return MarketContext(
        bars_m15=bars_m15,
        bars_h1=bars_h1,
        last_swing_high_m15=ctx_for.get("last_swing_high_m15"),
        last_swing_low_m15 =ctx_for.get("last_swing_low_m15"),
        last_swing_high_h1 =ctx_for.get("last_swing_high_h1"),
        last_swing_low_h1  =ctx_for.get("last_swing_low_h1"),
        opposing_choch_h1  =bool(ctx_for.get("opposing_choch_h1", False)),
        opposing_choch_m15 =bool(ctx_for.get("opposing_choch_m15", False)),
        equal_highs        =list(ctx_for.get("equal_highs", []) or []),
        equal_lows         =list(ctx_for.get("equal_lows",  []) or []),
        session_high       =ctx_for.get("session_high"),
        session_low        =ctx_for.get("session_low"),
        pdh                =ctx_for.get("pdh"),
        pdl                =ctx_for.get("pdl"),
        exhaustion_at_level=bool(ctx_for.get("exhaustion_at_level", False)),
        displacement_with  =bool(ctx_for.get("displacement_with", False)),
    )


def _symbol_spread(symbol: str, charts: dict) -> float:
    """Read live spread from MT5 data, fallback to conservative."""
    chart = charts.get(symbol, {}) if isinstance(charts, dict) else {}
    spread = chart.get("spread")
    if spread is not None:
        # spread in points for metals, pips for FX
        return float(spread)
    tick_size = chart.get("tick_size", _pip_size_for(symbol))
    return tick_size * 20  # conservative fallback


def _build_position(pos: dict, charts: dict, account_info: dict) -> Optional[Position]:
    try:
        direction = pos.get("type", "").upper()
        if direction not in ("BUY", "SELL"):
            return None
        entry = float(pos.get("open_price", pos.get("entry", 0)))
        sl = float(pos.get("sl", 0))
        cur = float(pos.get("current_price", 0))
        if entry <= 0 or sl <= 0 or cur <= 0:
            return None
        symbol = pos.get("symbol", "")
        pip = _pip_size_for(symbol)
        tick = _tick_size_for(symbol, charts)
        spread = _symbol_spread(symbol, charts)
        # Convert MT5 spread (points) to PRICE units
        spread_price = spread * tick

        equity = float(account_info.get("equity", 0)) if isinstance(account_info, dict) else 0.0
        highest_r = float(pos.get("highest_r_seen", pos.get("max_profit_r", 0)))

        return Position(
            direction=direction,
            entry=entry,
            current_sl=sl,
            current_price=cur,
            equity=equity,
            tp1=float(pos.get("tp", 0)),
            pip_size=pip,
            spread=spread_price,
            symbol=symbol,
            highest_r_seen=highest_r,
        )
    except (TypeError, ValueError):
        return None


def maybe_smart_trail(
    pos: dict,
    charts: dict,
    rules: dict,
    scan_context: Optional[dict] = None,
    account_info: Optional[dict] = None,
) -> Optional[TrailProposal]:
    if not is_enabled(rules):
        return None

    symbol = pos.get("symbol", "")
    mt5_pos = _build_position(pos, charts, account_info or {})
    if mt5_pos is None:
        log.debug("smart_trail: could not build Position for %s", symbol)
        return None

    ctx = _build_context(pos, charts, scan_context, account_info or {})
    cfg = _build_config(rules, symbol)

    proposal = compute_trailing_sl(mt5_pos, ctx, cfg)

    # Log every trail decision (helps debugging)
    if proposal.layers_fired and "hold" not in proposal.reason.lower():
        log.info("smart_trail %s: new_sl=%s layers=%s",
                 symbol, proposal.new_sl, ",".join(proposal.layers_fired))

    return proposal
