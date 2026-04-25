"""
data_router.py — OMNI Unified Data Source
==========================================
Provides a single interface for both MT5 and TradingView data sources.

Priority logic:
  1. MT5 (omni_data.json)  — if fresh < 30s  →  USE MT5
  2. TradingView (tv_data.json) — if fresh < 60s  →  USE TRADINGVIEW
  3. Neither available  →  return {} and log warning

All modules (ai_engine.py, dashboard.py, ict_precision.py) should
import from data_router instead of directly from mt5_connector.

Replace this pattern across your files:
  OLD:  from mt5_connector import get_account_info, get_symbol_prices
  NEW:  from data_router  import get_account_info, get_symbol_prices

ai_engine.py change:
  OLD:  data = _load_mt5()
  NEW:  from data_router import load_data
        data = load_data()
"""

import json
import os
import re
import logging
from datetime import datetime
from typing import Optional
import pandas as pd

log = logging.getLogger("DataRouter")

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_MT5_CANDIDATES = [
    # macOS Wine/MT5
    os.path.expanduser(
        "~/Library/Application Support/net.metaquotes.wine.metatrader5"
        "/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"
    ),
    # Windows
    os.path.expanduser(
        "~/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"
    ),
    # Same directory
    os.path.join(_SCRIPT_DIR, "omni_data.json"),
]

def _freshest_mt5_path(candidates):
    """Return the most recently modified path among candidates and their .tmp siblings."""
    best_path, best_mtime = candidates[-1], 0
    for p in candidates:
        for candidate in (p, p + ".tmp"):
            if os.path.exists(candidate):
                mtime = os.path.getmtime(candidate)
                if mtime > best_mtime:
                    best_path, best_mtime = candidate, mtime
    return best_path

MT5_JSON_PATH = _freshest_mt5_path(_MT5_CANDIDATES)
TV_DATA_PATH  = os.path.join(_SCRIPT_DIR, "tv_data.json")
RULES_PATH    = os.path.join(_SCRIPT_DIR, "rules.json")

MT5_STALE_SECONDS = 30
TV_STALE_SECONDS  = 60


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def _file_age(path: str) -> float:
    """Seconds since file was last modified. Returns inf if file missing."""
    try:
        return datetime.now().timestamp() - os.path.getmtime(path)
    except Exception:
        return float("inf")


def _mt5_fresh() -> bool:
    return _file_age(MT5_JSON_PATH) < MT5_STALE_SECONDS


def _tv_fresh() -> bool:
    return _file_age(TV_DATA_PATH) < TV_STALE_SECONDS


def get_active_source() -> str:
    """
    Returns the name of the currently active data source:
        "MT5"          — MT5 omni_data.json is live
        "TRADINGVIEW"  — TV webhook data is live (MT5 stale/missing)
        "NONE"         — both sources are stale
    """
    if _mt5_fresh():
        return "MT5"
    if _tv_fresh():
        return "TRADINGVIEW"
    return "NONE"


def get_source_status() -> dict:
    """
    Returns a status dict for the dashboard status bar.
    """
    mt5_age = _file_age(MT5_JSON_PATH)
    tv_age  = _file_age(TV_DATA_PATH)
    active  = get_active_source()

    return {
        "active_source":  active,
        "mt5_connected":  mt5_age < MT5_STALE_SECONDS,
        "tv_connected":   tv_age  < TV_STALE_SECONDS,
        "mt5_age_s":      round(mt5_age,  1) if mt5_age < 9999 else None,
        "tv_age_s":       round(tv_age,   1) if tv_age  < 9999 else None,
        "mt5_path":       MT5_JSON_PATH,
        "tv_path":        TV_DATA_PATH,
        "timestamp":      datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# RAW LOADERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_mt5_raw() -> dict:
    try:
        with open(MT5_JSON_PATH, "r", encoding="utf-8") as f:
            raw = re.sub(r',\s*([\]}])', r'\1', f.read())
        return json.loads(raw)
    except Exception as e:
        log.debug("MT5 load failed: %s", e)
        return {}


def _load_tv_raw() -> dict:
    try:
        with open(TV_DATA_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        log.debug("TV load failed: %s", e)
        return {}


def load_data() -> dict:
    """
    Primary data loader for ai_engine.py and ict_precision.py.
    Returns the freshest available data with source metadata injected.

    Usage:
        from data_router import load_data
        data = load_data()
        source = data.get("_source")   # "MT5" | "TRADINGVIEW" | "NONE"
    """
    if _mt5_fresh():
        data = _load_mt5_raw()
        if data:
            data["_source"] = "MT5"
            data["_source_age"] = round(_file_age(MT5_JSON_PATH), 1)
            log.debug("DataRouter: using MT5")
            return data

    if _tv_fresh():
        data = _load_tv_raw()
        if data:
            data["_source"] = "TRADINGVIEW"
            data["_source_age"] = round(_file_age(TV_DATA_PATH), 1)
            log.debug("DataRouter: using TradingView")
            return data

    log.warning("DataRouter: no live data source (MT5 age=%.0fs, TV age=%.0fs)",
                _file_age(MT5_JSON_PATH), _file_age(TV_DATA_PATH))
    return {"_source": "NONE", "_source_age": None}


def load_merged_data() -> dict:
    """
    Merges MT5 account/position data with TradingView price/structure data.
    Best of both worlds: MT5 for execution context, TV for chart structure.

    Use this when both sources are available.
    """
    mt5 = _load_mt5_raw() if os.path.exists(MT5_JSON_PATH) else {}
    tv  = _load_tv_raw()  if os.path.exists(TV_DATA_PATH)  else {}

    if not mt5 and not tv:
        return {"_source": "NONE"}

    merged = {**mt5}  # Start with MT5 as base

    # Overlay TradingView prices if TV has a symbol that MT5 doesn't have,
    # or if TV data is fresher for that symbol
    tv_prices = {p["symbol"]: p for p in tv.get("prices", [])}
    mt5_prices = {p["symbol"]: p for p in mt5.get("prices", [])}

    for sym, tv_price in tv_prices.items():
        if sym not in mt5_prices:
            # TV has a symbol MT5 doesn't track — add it
            merged.setdefault("prices", []).append(tv_price)
        else:
            # Merge ICT structure fields from TV into MT5 price entry
            for i, p in enumerate(merged.get("prices", [])):
                if p.get("symbol") == sym:
                    merged["prices"][i].update({
                        "structure":   tv_price.get("structure", "RANGING"),
                        "fvg_type":    tv_price.get("fvg_type",  "NONE"),
                        "fvg_high":    tv_price.get("fvg_high",  0),
                        "fvg_low":     tv_price.get("fvg_low",   0),
                        "ob_type":     tv_price.get("ob_type",   "NONE"),
                        "ob_high":     tv_price.get("ob_high",   0),
                        "ob_low":      tv_price.get("ob_low",    0),
                        "rsi":         tv_price.get("rsi",       50),
                        "atr":         tv_price.get("atr",       0),
                        "bias":        tv_price.get("bias",      "NEUTRAL"),
                        "_tv_overlay": True,
                    })
                    break

    merged["_source"]    = "MERGED"
    merged["_mt5_fresh"] = _mt5_fresh()
    merged["_tv_fresh"]  = _tv_fresh()
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API  (drop-in replacement for mt5_connector.py)
# ─────────────────────────────────────────────────────────────────────────────

def get_account_info() -> dict:
    """Account info always comes from MT5 (TV has none)."""
    data = _load_mt5_raw()
    if not data:
        data = _load_tv_raw()
    return data.get("account", {})


def get_symbol_prices(symbols: list) -> list:
    data = load_data()
    sym_set = set(s.upper() for s in symbols)
    return [p for p in data.get("prices", []) if p.get("symbol") in sym_set]


def get_open_positions() -> pd.DataFrame:
    data = _load_mt5_raw()
    positions = data.get("positions", [])
    if not positions:
        return pd.DataFrame(columns=[
            "ticket", "symbol", "type", "volume", "open_price",
            "current_price", "sl", "tp", "profit", "swap", "time"
        ])
    return pd.DataFrame(positions)


def get_trade_history(days: int = 30) -> pd.DataFrame:
    data = _load_mt5_raw()
    history = data.get("history", [])
    if not history:
        return pd.DataFrame()
    df = pd.DataFrame(history)
    df["time"] = pd.to_datetime(df["time"])
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    df = df[df["time"] >= cutoff].sort_values("time")
    df["cumulative_profit"] = df["profit"].cumsum()
    return df


def get_last_update() -> str:
    source = get_active_source()
    if source == "MT5":
        return _load_mt5_raw().get("timestamp", "—")
    elif source == "TRADINGVIEW":
        return _load_tv_raw().get("timestamp", "—")
    return "No data"


def is_connected() -> bool:
    return get_active_source() != "NONE"


def get_json_path() -> str:
    """Returns the active data file path (for legacy compatibility)."""
    if _mt5_fresh():
        return MT5_JSON_PATH
    return TV_DATA_PATH


# ─────────────────────────────────────────────────────────────────────────────
# RULES LOADER  (from rules.json — the 'brain' of the strategy)
# ─────────────────────────────────────────────────────────────────────────────

def load_rules() -> dict:
    """
    Load the ICT strategy rules from rules.json.
    Returns default rules if file missing.

    Edit rules.json by talking to Claude Code:
        "Add a rule: only trade XAUUSD during London and NY kill zones"
    """
    defaults = {
        "version":     "1.0",
        "watchlist":   ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "NAS100"],
        "timeframes":  ["H4", "H1", "M15"],
        "bias_rules": {
            "bullish_conditions":  ["BOS_BULLISH", "CHOCH_BULL"],
            "bearish_conditions":  ["BOS_BEARISH", "CHOCH_BEAR"],
            "neutral_conditions":  ["RANGING"],
        },
        "entry_rules": {
            "long":  ["price_in_bullish_ob", "price_in_bullish_fvg", "liquidity_sweep_low"],
            "short": ["price_in_bearish_ob", "price_in_bearish_fvg", "liquidity_sweep_high"],
        },
        "exit_rules": {
            "tp1_method": "nearest_liquidity",
            "tp2_method": "daily_high_low",
            "sl_method":  "below_ob",
        },
        "risk_rules": {
            "max_risk_per_trade_pct": 1.0,
            "min_rr_ratio":           2.0,
            "max_open_positions":     3,
            "skip_sessions":          ["ASIA"],
            "require_kill_zone":      False,
        },
        "filter_rules": {
            "min_atr":              0.0005,
            "max_spread_pips":      5.0,
            "require_htf_alignment": True,
        },
    }

    try:
        with open(RULES_PATH) as f:
            user_rules = json.load(f)
        # Deep merge: user rules override defaults
        for k, v in user_rules.items():
            if isinstance(v, dict) and k in defaults:
                defaults[k].update(v)
            else:
                defaults[k] = v
        return defaults
    except FileNotFoundError:
        return defaults
    except Exception as e:
        log.error("Rules load error: %s", e)
        return defaults


# ─────────────────────────────────────────────────────────────────────────────
# STATUS PRINT (for debugging)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    status = get_source_status()
    print("\n[OMNI Data Router — Status]")
    print(f"  Active source : {status['active_source']}")
    print(f"  MT5 connected : {status['mt5_connected']} (age: {status['mt5_age_s']}s)")
    print(f"  TV connected  : {status['tv_connected']}  (age: {status['tv_age_s']}s)")
    print(f"  MT5 path      : {status['mt5_path']}")
    print(f"  TV path       : {status['tv_path']}")

    data = load_data()
    prices = data.get("prices", [])
    print(f"\n  Prices loaded : {len(prices)} symbols")
    for p in prices[:5]:
        print(f"    {p.get('symbol'):>8}  {p.get('close', p.get('bid', 0)):.4f}")
    if len(prices) > 5:
        print(f"    ... and {len(prices)-5} more")

    rules = load_rules()
    print(f"\n  Rules loaded  : watchlist={rules['watchlist']}")
    print(f"  Risk rules    : {rules['risk_rules']}\n")
