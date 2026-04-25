"""
OMNI ICT AI Dashboard — Fully Integrated
=========================================
Reads live data from:
  • omni_data.json  — MT5/Midas account, prices, OHLC bars (written by OmniExport.mq5)
  • ai_state.json   — AI regime, params, signals (written by ai_engine.py)
  • trader_state.json — bot state, streaks, P&L (written by auto_trader.py)
  • omni_ict.db     — trade history (SQLite)

Run:  python dashboard.py
URL:  http://localhost:8050
"""

import json
import os
import re
import sqlite3
import math
from datetime import datetime, timedelta, timezone
import dash
from dash import dcc, html, dash_table, Input, Output, callback, ctx, no_update
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
try:
    import ict_precision as ict
    _HAS_ICT = True
except ImportError:
    _HAS_ICT = False

# ── PATH CONFIGURATION ────────────────────────────────────────────────────────
# Adjust these to match your machine. The dashboard auto-detects common locations.

_CANDIDATES_JSON = [
    # macOS (Wine/MT5)
    os.path.expanduser(
        "~/Library/Application Support/net.metaquotes.wine.metatrader5"
        "/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"
    ),
    # Windows direct MT5 Common Files
    os.path.expanduser(
        "~/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"
    ),
    # Fallback: same directory as this script
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "omni_data.json"),
]

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

JSON_PATH       = next((p for p in _CANDIDATES_JSON if os.path.exists(p)), _CANDIDATES_JSON[-1])
AI_STATE_PATH   = os.path.join(_SCRIPT_DIR, "ai_state.json")
TRADER_STATE    = os.path.join(_SCRIPT_DIR, "trader_state.json")
DB_PATH         = os.path.join(_SCRIPT_DIR, "omni_ict.db")

REFRESH_MS      = 5000   # poll every 5 seconds (matches EA export interval)

print(f"[OMNI] JSON_PATH:    {JSON_PATH}")
print(f"[OMNI] AI_STATE:     {AI_STATE_PATH}")
print(f"[OMNI] TRADER_STATE: {TRADER_STATE}")
print(f"[OMNI] DB_PATH:      {DB_PATH}")
print(f"[OMNI] JSON exists:  {os.path.exists(JSON_PATH)}")

# ── DESIGN TOKENS ─────────────────────────────────────────────────────────────
BG      = "#07090c"
CARD    = "#0c0f14"
CARD2   = "#11161e"
BORDER  = "#1c2333"
GOLD    = "#d4a843"
SILVER  = "#a8b8c8"
RED     = "#e84545"
GREEN   = "#2ecc71"
BLUE    = "#3b82f6"
PURPLE  = "#8b5cf6"
ORANGE  = "#f97316"
TEXT    = "#dde4ef"
MUTED   = "#4a5a72"
MONO    = "'JetBrains Mono','Courier New',monospace"
SANS    = "'Sora','Segoe UI',sans-serif"

AMD_COLORS  = {"ACCUMULATION": BLUE, "MANIPULATION": GOLD, "DISTRIBUTION": GREEN,
               "LONDON_CLOSE": PURPLE, "REBALANCE": MUTED}
SESS_COLORS = {"ASIA": BLUE, "LONDON": GOLD, "NEW_YORK": GREEN,
               "NY_CLOSE": PURPLE, "CLOSED": MUTED}

TH = {
    "backgroundColor": CARD2, "color": GOLD, "fontWeight": "600",
    "textTransform": "uppercase", "fontSize": "10px",
    "letterSpacing": "0.08em", "border": f"1px solid {BORDER}",
    "fontFamily": MONO, "padding": "8px 10px",
}
TC = {
    "backgroundColor": CARD, "color": TEXT, "border": f"1px solid {BORDER}",
    "padding": "7px 10px", "fontFamily": MONO, "fontSize": "11px",
}

# ── DATA LOADERS ──────────────────────────────────────────────────────────────

_mt5_cache: dict = {"mtime": 0.0, "data": {}}
_analysis_cache: dict = {"mtime": 0.0, "results": {}}

# Maximum bars to send to Plotly charts (keeps browser fast)
MAX_CHART_BARS = 200

def load_mt5() -> dict:
    """Load and parse omni_data.json from MT5/Midas (cached by file mtime)."""
    try:
        mtime = os.path.getmtime(JSON_PATH)
        if mtime == _mt5_cache["mtime"] and _mt5_cache["data"]:
            return _mt5_cache["data"]
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = re.sub(r',\s*([\]}])', r'\1', f.read())
        data = json.loads(raw)
        _mt5_cache["mtime"] = mtime
        _mt5_cache["data"] = data
        # Invalidate analysis cache when data changes
        _analysis_cache["mtime"] = 0.0
        _analysis_cache["results"] = {}
        return data
    except Exception as e:
        print(f"[load_mt5] {e}")
        return _mt5_cache.get("data", {})


def _cached_analyze(bars: list, sym: str, sym_info: dict, tf: str) -> dict:
    """analyze_bars with per-symbol+tf caching (invalidated when JSON changes)."""
    mtime = _mt5_cache["mtime"]
    if _analysis_cache["mtime"] != mtime:
        _analysis_cache["mtime"] = mtime
        _analysis_cache["results"] = {}
    key = f"{sym}_{tf}"
    if key in _analysis_cache["results"]:
        return _analysis_cache["results"][key]
    result = analyze_bars(bars, sym, sym_info) if bars else _empty_analysis(sym)
    _analysis_cache["results"][key] = result
    return result


def _trim_bars(bars: list, limit: int = MAX_CHART_BARS) -> list:
    """Trim bar list to last `limit` entries for chart rendering."""
    if len(bars) <= limit:
        return bars
    return bars[-limit:] if bars and isinstance(bars[0], dict) and "t" in bars[0] else bars[:limit]


def load_ai_state() -> dict:
    try:
        with open(AI_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def load_trader_state() -> dict:
    try:
        with open(TRADER_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_db_trades(limit=50) -> pd.DataFrame:
    """Load recent closed trades from SQLite."""
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        con = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(
            f"""SELECT trade_id, symbol, trade_direction, open_time, close_time,
                       entry_price, stop_loss_price, take_profit_price, exit_price,
                       pnl_dollars, pnl_percent, r_multiple, outcome, exit_reason, session
                FROM trades ORDER BY trade_id DESC LIMIT {limit}""",
            con,
        )
        con.close()
        return df
    except Exception as e:
        print(f"[load_db_trades] {e}")
        return pd.DataFrame()


def mt5_connected() -> bool:
    try:
        age = datetime.now().timestamp() - os.path.getmtime(JSON_PATH)
        return age < 30
    except Exception:
        return False


# ── ICT ANALYSIS ENGINE (runs on real bar data from MT5) ─────────────────────

def analyze_bars(bars: list, sym: str, sym_info: dict) -> dict:
    """
    Full ICT analysis on real OHLC bars from omni_data.json charts section.
    bars: list of {t, o, h, l, c, v} dicts — newest first (MT5 format).
    Returns analysis dict compatible with dashboard display.
    """
    if len(bars) < 20:
        return _empty_analysis(sym)

    # MT5 exports newest-first; reverse so index 0 = oldest
    bars_asc = list(reversed(bars))
    n = len(bars_asc)
    point = sym_info.get("point", 0.0001)
    digits = len(str(sym_info.get("tick_size", 0.00001)).rstrip("0").split(".")[-1])
    digits = max(2, min(digits, 5))

    # ── Swing highs / lows (last 20 bars) ────────────────────────────
    recent = bars_asc[max(0, n - 20):]
    prev20 = bars_asc[max(0, n - 40):max(0, n - 20)]

    swing_h = max(b["h"] for b in recent)
    swing_l = min(b["l"] for b in recent)
    prev_h  = max((b["h"] for b in prev20), default=swing_h)
    prev_l  = min((b["l"] for b in prev20), default=swing_l)

    cur = bars_asc[-1]["c"]

    # ── BOS / CHoCH ───────────────────────────────────────────────────
    structure = "RANGING"
    if cur > prev_h:
        structure = "BOS_BULLISH"
    elif cur < prev_l:
        structure = "BOS_BEARISH"
    elif cur > swing_h * 0.9995:
        structure = "CHOCH_BULL"
    elif cur < swing_l * 1.0005:
        structure = "CHOCH_BEAR"

    # ── Order Block detection — collect up to 3 (most recent first) ──
    obs = []
    for i in range(n - 2, max(0, n - 30), -1):
        b, bn = bars_asc[i], bars_asc[i + 1] if i + 1 < n else bars_asc[i]
        if i + 1 >= n:
            continue
        rng = bn["h"] - bn["l"] or 0.0001
        impulse = abs(bn["c"] - bn["o"]) / rng
        if impulse < 0.50:
            continue
        if b["c"] < b["o"] and bn["c"] > bn["o"]:
            obs.append({"type": "BULLISH_OB", "h": b["h"], "l": b["l"], "age": n - 1 - i})
        elif b["c"] > b["o"] and bn["c"] < bn["o"]:
            obs.append({"type": "BEARISH_OB", "h": b["h"], "l": b["l"], "age": n - 1 - i})
        if len(obs) >= 3:
            break
    ob_type = obs[0]["type"] if obs else "NONE"
    ob_h    = obs[0]["h"]    if obs else 0.0
    ob_l    = obs[0]["l"]    if obs else 0.0

    # ── FVG detection — collect up to 5 (most recent first) ──────────
    fvgs = []
    for i in range(n - 3, max(0, n - 25), -1):
        if i + 2 >= n:
            continue
        b0, b2 = bars_asc[i + 2], bars_asc[i]
        if b0["l"] > b2["h"]:
            ce = (b0["l"] + b2["h"]) / 2
            fvgs.append({"type": "BULLISH", "h": b0["l"], "l": b2["h"], "ce": ce, "age": n - 1 - i})
        elif b0["h"] < b2["l"]:
            ce = (b0["h"] + b2["l"]) / 2
            fvgs.append({"type": "BEARISH", "h": b2["l"], "l": b0["h"], "ce": ce, "age": n - 1 - i})
        if len(fvgs) >= 5:
            break
    fvg_type = fvgs[0]["type"] if fvgs else "NONE"
    fvg_h    = fvgs[0]["h"]    if fvgs else 0.0
    fvg_l    = fvgs[0]["l"]    if fvgs else 0.0

    # ── OB + FVG confluence check (FVG inside or overlapping OB zone) ─
    ob_fvg_conf = "NONE"
    for ob in obs:
        for fvg in fvgs:
            if ob["type"][:4] == fvg["type"][:4]:  # same direction
                overlap = min(ob["h"], fvg["h"]) - max(ob["l"], fvg["l"])
                if overlap > 0:
                    ob_fvg_conf = "BULL" if ob["type"] == "BULLISH_OB" else "BEAR"
                    break
        if ob_fvg_conf != "NONE":
            break

    # ── Pattern detection ─────────────────────────────────────────────
    patterns = []
    if n >= 20:
        highs = [b["h"] for b in bars_asc]
        lows  = [b["l"] for b in bars_asc]
        # Double Top: two recent peaks within 0.4% of each other
        peak_idxs = [i for i in range(2, n - 2)
                     if highs[i] >= highs[i-1] and highs[i] >= highs[i+1]
                     and highs[i] >= highs[i-2] and highs[i] >= highs[i+2]]
        if len(peak_idxs) >= 2:
            p1, p2 = peak_idxs[-2], peak_idxs[-1]
            if abs(highs[p1] - highs[p2]) / max(highs[p1], 0.0001) < 0.004:
                patterns.append({"pattern": "Double Top", "level": max(highs[p1], highs[p2]),
                                  "direction": "BEARISH", "strength": 70})
        # Double Bottom: two recent troughs within 0.4% of each other
        trough_idxs = [i for i in range(2, n - 2)
                       if lows[i] <= lows[i-1] and lows[i] <= lows[i+1]
                       and lows[i] <= lows[i-2] and lows[i] <= lows[i+2]]
        if len(trough_idxs) >= 2:
            t1, t2 = trough_idxs[-2], trough_idxs[-1]
            if abs(lows[t1] - lows[t2]) / max(lows[t1], 0.0001) < 0.004:
                patterns.append({"pattern": "Double Bottom", "level": min(lows[t1], lows[t2]),
                                  "direction": "BULLISH", "strength": 70})
        # Head & Shoulders (bearish): 3 peaks, middle is highest
        if len(peak_idxs) >= 3:
            ls, hd, rs = peak_idxs[-3], peak_idxs[-2], peak_idxs[-1]
            if highs[hd] > highs[ls] and highs[hd] > highs[rs]:
                neckline = min(lows[ls:rs+1])
                patterns.append({"pattern": "Head & Shoulders", "level": neckline,
                                  "direction": "BEARISH", "strength": 80})
        # Inverse H&S (bullish): 3 troughs, middle is lowest
        if len(trough_idxs) >= 3:
            ls, hd, rs = trough_idxs[-3], trough_idxs[-2], trough_idxs[-1]
            if lows[hd] < lows[ls] and lows[hd] < lows[rs]:
                neckline = max(highs[ls:rs+1])
                patterns.append({"pattern": "Inv H&S", "level": neckline,
                                  "direction": "BULLISH", "strength": 80})

    # ── MA calculations ───────────────────────────────────────────────
    closes = [b["c"] for b in bars_asc]
    ma20 = sum(closes[-20:]) / 20 if n >= 20 else cur
    ma5  = sum(closes[-5:])  / 5  if n >= 5  else cur
    trend = "BULLISH" if ma5 > ma20 else "BEARISH" if ma5 < ma20 else "NEUTRAL"

    # ── RSI (14) ──────────────────────────────────────────────────────
    gains = losses = 0.0
    for i in range(max(1, n - 14), n):
        d = bars_asc[i]["c"] - bars_asc[i - 1]["c"]
        if d > 0: gains += d
        else: losses -= d
    rs = gains / (losses or 0.0001)
    rsi = round(100 - 100 / (1 + rs), 1)
    rsi_sig = "OVERBOUGHT" if rsi > 65 else "OVERSOLD" if rsi < 35 else "NEUTRAL"

    # ── Asia range (approximate: bars -8 to -4 from end) ─────────────
    asia_slice = bars_asc[max(0, n - 8):max(0, n - 4)]
    asia_h = max((b["h"] for b in asia_slice), default=0)
    asia_l = min((b["l"] for b in asia_slice), default=0)

    # ── ATR (14) ──────────────────────────────────────────────────────
    atr = sum(b["h"] - b["l"] for b in bars_asc[max(0, n - 14):]) / min(14, n)

    # ── Scoring ───────────────────────────────────────────────────────
    bull = bear = 30
    if trend == "BULLISH": bull += 20
    if trend == "BEARISH": bear += 20
    if fvg_type == "BULLISH": bull += 25
    if fvg_type == "BEARISH": bear += 25
    if ob_type == "BULLISH_OB":
        prox = abs(cur - ob_l) / max(ob_h - ob_l, 0.0001)
        bull += 30 if prox < 0.3 else 10
    if ob_type == "BEARISH_OB":
        prox = abs(cur - ob_h) / max(ob_h - ob_l, 0.0001)
        bear += 30 if prox < 0.3 else 10
    if rsi_sig == "OVERSOLD": bull += 15
    if rsi_sig == "OVERBOUGHT": bear += 15
    if "BULL" in structure: bull += 15
    if "BEAR" in structure: bear += 15
    if asia_h > 0 and cur > asia_h: bull += 10
    if asia_l > 0 and cur < asia_l: bear += 10

    direction = "BUY" if bull > bear else "SELL" if bear > bull else "NEUTRAL"
    score = min(bull if direction == "BUY" else bear, 100)

    sl = round(cur - atr * 1.5, digits) if direction == "BUY" else round(cur + atr * 1.5, digits)
    tp = round(cur + atr * 3.0, digits) if direction == "BUY" else round(cur - atr * 3.0, digits)
    rr = round(abs(tp - cur) / max(abs(sl - cur), 0.0001), 2)

    reasons = []
    if ob_type != "NONE":
        reasons.append(f"{'Bullish' if 'BULL' in ob_type else 'Bearish'} OB: {ob_l:.{digits}f}–{ob_h:.{digits}f}")
    if fvg_type != "NONE":
        reasons.append(f"{fvg_type} FVG: {fvg_l:.{digits}f}–{fvg_h:.{digits}f}")
    if structure != "RANGING":
        reasons.append(structure.replace("_", " "))
    if rsi_sig != "NEUTRAL":
        reasons.append(f"RSI {rsi_sig.lower()} ({rsi})")
    reasons.append(f"Trend: {trend} | MA5={ma5:.{digits}f} MA20={ma20:.{digits}f}")
    if asia_h > 0:
        reasons.append(f"Asia range: {asia_l:.{digits}f}–{asia_h:.{digits}f}")

    # Add OB+FVG confluence bonus to score
    if ob_fvg_conf == ("BULL" if direction == "BUY" else "BEAR"):
        score = min(score + 10, 100)

    return {
        "sym": sym, "direction": direction, "score": score,
        "sl": sl, "tp": tp, "rr": rr,
        "rsi": rsi, "rsi_sig": rsi_sig, "trend": trend,
        "structure": structure, "atr": atr, "cur": cur, "digits": digits,
        "ob_type": ob_type, "ob_h": ob_h, "ob_l": ob_l,
        "fvg_type": fvg_type, "fvg_h": fvg_h, "fvg_l": fvg_l,
        "obs": obs, "fvgs": fvgs, "ob_fvg_conf": ob_fvg_conf,
        "patterns": patterns,
        "asia_h": asia_h, "asia_l": asia_l,
        "swing_h": swing_h, "swing_l": swing_l,
        "ma5": ma5, "ma20": ma20, "reasons": reasons,
    }


def _empty_analysis(sym):
    return {
        "sym": sym, "direction": "NEUTRAL", "score": 0,
        "sl": 0, "tp": 0, "rr": 0, "rsi": 50, "rsi_sig": "NEUTRAL",
        "trend": "NEUTRAL", "structure": "RANGING", "atr": 0,
        "cur": 0, "digits": 5, "ob_type": "NONE", "ob_h": 0, "ob_l": 0,
        "fvg_type": "NONE", "fvg_h": 0, "fvg_l": 0,
        "obs": [], "fvgs": [], "ob_fvg_conf": "NONE", "patterns": [],
        "asia_h": 0, "asia_l": 0, "swing_h": 0, "swing_l": 0,
        "ma5": 0, "ma20": 0, "reasons": ["No data — MT5 EA not running"],
    }


def _analysis_from_prices(p: dict) -> dict:
    """Build a lightweight analysis dict from the prices array (no OHLCV bars needed)."""
    sym  = p.get("symbol", "")
    bid  = p.get("bid", 0)
    rsi  = p.get("rsi", 50)
    trend = p.get("trend", "NEUTRAL")
    fvg  = p.get("fvg_type", "NONE")
    ob   = p.get("ob_type",  "NONE")
    struct = p.get("structure", "RANGING")
    spd  = p.get("spread", 0)
    # Determine digits from symbol name
    if "XAU" in sym or "XAG" in sym:
        digits = 2
    elif "JPY" in sym or "US30" in sym or "US500" in sym or "TECH" in sym:
        digits = 2
    else:
        digits = 5
    # Quick directional score
    bull = bear = 30
    if trend == "BULLISH":   bull += 20
    elif trend == "BEARISH": bear += 20
    if fvg == "BULLISH":     bull += 25
    elif fvg == "BEARISH":   bear += 25
    if ob == "BULLISH_OB":   bull += 20
    elif ob == "BEARISH_OB": bear += 20
    if rsi < 30:  bull += 15
    elif rsi > 70: bear += 15
    if "BOS_BULL" in struct:  bull += 15
    elif "BOS_BEAR" in struct: bear += 15
    if "CHOCH_POTENTIAL_BULL" in struct: bull += 10
    elif "CHOCH_POTENTIAL_BEAR" in struct: bear += 10
    direction = "BUY" if bull > bear else "SELL" if bear > bull else "NEUTRAL"
    score = min(bull if direction == "BUY" else bear, 100)
    # OB+FVG confluence
    ob_fvg_conf = "NONE"
    if ob == "BULLISH_OB" and fvg == "BULLISH": ob_fvg_conf = "BULL"
    elif ob == "BEARISH_OB" and fvg == "BEARISH": ob_fvg_conf = "BEAR"
    return {
        "sym": sym, "direction": direction, "score": score,
        "sl": 0, "tp": 0, "rr": 0,
        "rsi": round(rsi, 1), "rsi_sig": "OVERSOLD" if rsi < 30 else "OVERBOUGHT" if rsi > 70 else "NEUTRAL",
        "trend": trend, "structure": struct, "atr": 0, "cur": bid, "digits": digits,
        "ob_type": ob, "ob_h": p.get("ob_high", 0), "ob_l": p.get("ob_low", 0),
        "fvg_type": fvg, "fvg_h": p.get("fvg_high", 0), "fvg_l": p.get("fvg_low", 0),
        "obs": [{"type": ob, "h": p.get("ob_high",0), "l": p.get("ob_low",0), "age": 1}] if ob != "NONE" else [],
        "fvgs": [{"type": fvg, "h": p.get("fvg_high",0), "l": p.get("fvg_low",0), "ce": (p.get("fvg_high",0)+p.get("fvg_low",0))/2, "age": 1}] if fvg != "NONE" else [],
        "ob_fvg_conf": ob_fvg_conf,
        "patterns": [],
        "asia_h": p.get("asia_high", 0), "asia_l": p.get("asia_low", 0),
        "swing_h": p.get("swing_high", 0), "swing_l": p.get("swing_low", 0),
        "ma5": 0, "ma20": 0, "spread": spd,
        "reasons": [f"Trend: {trend}", f"RSI: {rsi:.1f}", f"OB: {ob}", f"FVG: {fvg}", f"Structure: {struct}"],
    }


# ── CHART BUILDERS ─────────────────────────────────────────────────────────────

def make_candle_chart(bars_raw: list, analysis: dict = None, sym: str = "",
                      sym_info: dict = None, height=380,
                      entry=None, sl=None, tp1=None, tp2=None) -> go.Figure:
    """
    Build a full candlestick chart from real MT5 bar data.
    bars_raw is newest-first from omni_data.json charts[sym][tf].
    """
    if not bars_raw:
        fig = go.Figure()
        fig.add_annotation(text="No bar data — is OmniExport EA running?",
                           x=0.5, y=0.5, xref="paper", yref="paper",
                           font=dict(color=MUTED, size=12, family=MONO), showarrow=False)
        fig.update_layout(**_chart_layout(height=height))
        return fig

    bars = list(reversed(bars_raw))  # oldest first for correct time ordering
    times = [b["t"] for b in bars]
    opens  = [b["o"] for b in bars]
    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]
    vols   = [b.get("v", 0) for b in bars]

    analysis = analysis or {}
    d = analysis.get("digits", 5)

    fig = go.Figure()

    # ── Volume bars (subdued) ─────────────────────────────────────────
    vol_colors = ["rgba(46,204,113,0.33)" if c >= o else "rgba(232,69,69,0.33)"
                  for c, o in zip(closes, opens)]
    fig.add_trace(go.Bar(
        x=times, y=vols, name="Volume",
        marker_color=vol_colors,
        yaxis="y2", showlegend=False,
        hovertemplate="Vol: %{y:,.0f}<extra></extra>",
    ))

    # ── Candlesticks ──────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=times, open=opens, high=highs, low=lows, close=closes,
        name=sym,
        increasing_line_color=GREEN, increasing_fillcolor="rgba(46,204,113,0.80)",
        decreasing_line_color=RED,   decreasing_fillcolor="rgba(232,69,69,0.80)",
        line_width=1,
        hovertemplate=(
            f"<b>%{{x}}</b><br>"
            f"O: %{{open:.{d}f}}  H: %{{high:.{d}f}}<br>"
            f"L: %{{low:.{d}f}}  C: %{{close:.{d}f}}<extra></extra>"
        ),
    ))

    # ── MA20 ──────────────────────────────────────────────────────────
    if len(closes) >= 20:
        ma20 = pd.Series(closes).rolling(20).mean().tolist()
        fig.add_trace(go.Scatter(
            x=times, y=ma20, name="MA20",
            line=dict(color=GOLD, width=1.5, dash="solid"),
            hovertemplate=f"MA20: %{{y:.{d}f}}<extra></extra>",
        ))

    if len(closes) >= 50:
        ma50 = pd.Series(closes).rolling(50).mean().tolist()
        fig.add_trace(go.Scatter(
            x=times, y=ma50, name="MA50",
            line=dict(color=SILVER, width=1, dash="dot"),
            hovertemplate=f"MA50: %{{y:.{d}f}}<extra></extra>",
        ))

    # ── Key levels from sym_info ──────────────────────────────────────
    pdh = sym_info.get("pdh", 0)
    pdl = sym_info.get("pdl", 0)
    pwh = sym_info.get("pwh", 0)
    pwl = sym_info.get("pwl", 0)

    def _hline(price, color, label, dash="dot"):
        if price and price > 0:
            fig.add_hline(y=price, line=dict(color=color, width=1, dash=dash),
                          annotation=dict(text=label, font=dict(color=color, size=9,
                                          family=MONO), xanchor="left"))

    _hline(pdh, GOLD,   f"PDH {pdh:.{d}f}", "dash")
    _hline(pdl, GOLD,   f"PDL {pdl:.{d}f}", "dash")
    _hline(pwh, SILVER, f"PWH {pwh:.{d}f}", "longdash")
    _hline(pwl, SILVER, f"PWL {pwl:.{d}f}", "longdash")

    # ── ICT analysis levels ───────────────────────────────────────────
    cur = analysis.get("cur", 0)
    sl  = analysis.get("sl", 0)
    tp  = analysis.get("tp", 0)
    direction = analysis.get("direction", "NEUTRAL")

    if direction != "NEUTRAL" and sl and tp:
        _hline(cur, GOLD,  f"Entry {cur:.{d}f}", "solid")
        _hline(sl,  RED,   f"SL {sl:.{d}f}", "dot")
        _hline(tp,  GREEN, f"TP {tp:.{d}f}", "dot")

    # ── Multiple Order Block shading (up to 3, fading for older) ─────
    obs = analysis.get("obs", [])
    if not obs and analysis.get("ob_type") and analysis.get("ob_type") != "NONE":
        obs = [{"type": analysis.get("ob_type",""), "h": analysis.get("ob_h", 0),
                "l": analysis.get("ob_l", 0), "age": 1}]
    # OB rgba fills: strong→medium→faint
    _ob_fills  = ["rgba(46,204,113,0.19)", "rgba(46,204,113,0.09)", "rgba(46,204,113,0.05)"]
    _rb_fills  = ["rgba(232,69,69,0.19)",  "rgba(232,69,69,0.09)",  "rgba(232,69,69,0.05)"]
    _ob_lines  = ["rgba(46,204,113,0.40)", "rgba(46,204,113,0.20)", "rgba(46,204,113,0.13)"]
    _rb_lines  = ["rgba(232,69,69,0.40)",  "rgba(232,69,69,0.20)",  "rgba(232,69,69,0.13)"]
    for i, ob in enumerate(obs[:3]):
        is_bull = "BULL" in ob.get("type", "")
        ob_color = GREEN if is_bull else RED
        fill_c = _ob_fills[i] if is_bull else _rb_fills[i]
        line_c = _ob_lines[i] if is_bull else _rb_lines[i]
        lbl = f"{'Bull' if is_bull else 'Bear'} OB"
        if i > 0:
            lbl += f" ({ob.get('age',1)}b)"
        fig.add_hrect(
            y0=ob["l"], y1=ob["h"],
            fillcolor=fill_c,
            line=dict(color=line_c, width=1.2 if i == 0 else 0.6, dash="dot"),
            annotation=dict(
                text=lbl,
                font=dict(color=ob_color, size=9 if i == 0 else 8, family=MONO),
                xanchor="left",
            ),
        )

    # ── Multiple FVG shading (up to 5, fading for older) ─────────────
    fvgs = analysis.get("fvgs", [])
    if not fvgs and analysis.get("fvg_type") and analysis.get("fvg_type") != "NONE":
        fvgs = [{"type": analysis.get("fvg_type",""), "h": analysis.get("fvg_h", 0),
                 "l": analysis.get("fvg_l", 0),
                 "ce": (analysis.get("fvg_h",0) + analysis.get("fvg_l",0)) / 2, "age": 1}]
    _fvg_bull_fills = ["rgba(59,130,246,0.15)","rgba(59,130,246,0.09)","rgba(59,130,246,0.06)",
                       "rgba(59,130,246,0.04)","rgba(59,130,246,0.02)"]
    _fvg_bear_fills = ["rgba(139,92,246,0.15)","rgba(139,92,246,0.09)","rgba(139,92,246,0.06)",
                       "rgba(139,92,246,0.04)","rgba(139,92,246,0.02)"]
    _fvg_bull_lines = ["rgba(59,130,246,0.40)","rgba(59,130,246,0.25)","rgba(59,130,246,0.14)",
                       "rgba(59,130,246,0.09)","rgba(59,130,246,0.06)"]
    _fvg_bear_lines = ["rgba(139,92,246,0.40)","rgba(139,92,246,0.25)","rgba(139,92,246,0.14)",
                       "rgba(139,92,246,0.09)","rgba(139,92,246,0.06)"]
    for i, fvg in enumerate(fvgs[:5]):
        is_bull_fvg = fvg.get("type","") == "BULLISH"
        fvg_color = BLUE if is_bull_fvg else PURPLE
        fill_c = _fvg_bull_fills[i] if is_bull_fvg else _fvg_bear_fills[i]
        line_c = _fvg_bull_lines[i] if is_bull_fvg else _fvg_bear_lines[i]
        lbl = f"FVG {fvg.get('type','')[:4]}"
        if i > 0:
            lbl += f" ({fvg.get('age',1)}b)"
        fig.add_hrect(
            y0=fvg["l"], y1=fvg["h"],
            fillcolor=fill_c,
            line=dict(color=line_c, width=1 if i == 0 else 0.5, dash="longdash"),
            annotation=dict(
                text=lbl,
                font=dict(color=fvg_color, size=9 if i == 0 else 7, family=MONO),
                xanchor="left",
            ),
        )
        # CE equilibrium line for the primary FVG
        if i == 0 and fvg.get("ce"):
            _hline(fvg["ce"], fvg_color,
                   f"CE {fvg['ce']:.{d}f}", "dot")

    # ── OB + FVG Confluence zone (special highlight) ──────────────────
    conf = analysis.get("ob_fvg_conf", "NONE")
    if conf in ("BULL", "BEAR"):
        c_want_ob  = "BULL" if conf == "BULL" else "BEAR"
        c_want_fvg = "BULLISH" if conf == "BULL" else "BEARISH"
        conf_color = GREEN if conf == "BULL" else RED
        conf_fill  = "rgba(46,204,113,0.25)" if conf == "BULL" else "rgba(232,69,69,0.25)"
        conf_line  = "rgba(46,204,113,0.73)" if conf == "BULL" else "rgba(232,69,69,0.73)"
        for ob in obs:
            if c_want_ob in ob.get("type",""):
                for fvg in fvgs:
                    if fvg.get("type") == c_want_fvg:
                        ol = max(ob["l"], fvg["l"])
                        oh = min(ob["h"], fvg["h"])
                        if oh > ol:
                            fig.add_hrect(
                                y0=ol, y1=oh,
                                fillcolor=conf_fill,
                                line=dict(color=conf_line, width=2, dash="solid"),
                                annotation=dict(
                                    text=f"⚡ OB+FVG {conf}",
                                    font=dict(color=conf_color, size=10, family=MONO),
                                    xanchor="left",
                                ),
                            )
                        break
                break

    # ── Pattern levels (Double Top/Bottom, H&S, Inv H&S) ─────────────
    for pat in analysis.get("patterns", []):
        lvl = pat.get("level", 0)
        if not lvl:
            continue
        pat_dir   = pat.get("direction", "NEUTRAL")
        pat_color = RED if pat_dir == "BEARISH" else GREEN
        pat_name  = pat.get("pattern", "")
        strength  = pat.get("strength", 0)
        _hline(lvl, f"rgba({46 if pat_color == GREEN else 232},{204 if pat_color == GREEN else 69},{113 if pat_color == GREEN else 69},0.80)",
               f"{pat_name} {strength}%", "dashdot")

    # ── Asia range shading ────────────────────────────────────────────
    ah, al = analysis.get("asia_h", 0), analysis.get("asia_l", 0)
    if ah and al and ah != al:
        fig.add_hrect(
            y0=al, y1=ah, fillcolor="rgba(59,130,246,0.05)",
            line=dict(color="rgba(59,130,246,0.20)", width=0.5),
            annotation=dict(text="Asia", font=dict(color=BLUE, size=8, family=MONO)),
        )

    # ── Active trade levels (entry / SL / TPs) ───────────────────────
    if entry is not None:
        fig.add_hline(y=entry, line_color="#00BFFF", line_dash="dash", line_width=1.5,
                      annotation_text="ENTRY", annotation_position="right",
                      annotation_font_color="#00BFFF", annotation_font_size=10)
    if sl is not None:
        fig.add_hline(y=sl, line_color="#FF4444", line_dash="dot", line_width=1.5,
                      annotation_text="SL", annotation_position="right",
                      annotation_font_color="#FF4444", annotation_font_size=10)
    if tp1 is not None:
        fig.add_hline(y=tp1, line_color="#00FF88", line_dash="dot", line_width=1.5,
                      annotation_text="TP1", annotation_position="right",
                      annotation_font_color="#00FF88", annotation_font_size=10)
    if tp2 is not None:
        fig.add_hline(y=tp2, line_color="#00FF44", line_dash="dot", line_width=1.5,
                      annotation_text="TP2", annotation_position="right",
                      annotation_font_color="#00FF44", annotation_font_size=10)

    layout = _chart_layout(height=height)
    layout.update(
        yaxis2=dict(
            overlaying="y", side="right", showgrid=False,
            showticklabels=False, fixedrange=True, range=[0, max(vols or [1]) * 5],
        ),
        legend=dict(
            bgcolor="rgba(0,0,0,0)", font=dict(color=MUTED, size=10),
            orientation="h", x=0, y=1.02,
        ),
        xaxis_rangeslider_visible=False,
    )
    fig.update_layout(**layout)
    return fig


def make_equity_curve(history_df: pd.DataFrame, height=200) -> go.Figure:
    fig = go.Figure()
    if history_df.empty or "cumulative_profit" not in history_df.columns:
        fig.add_annotation(text="No trade history", x=0.5, y=0.5,
                           xref="paper", yref="paper",
                           font=dict(color=MUTED, size=12, family=MONO), showarrow=False)
        fig.update_layout(**_chart_layout(height=height))
        return fig
    fig.add_trace(go.Scatter(
        x=history_df["time"], y=history_df["cumulative_profit"],
        fill="tozeroy", line=dict(color=GOLD, width=2),
        fillcolor="rgba(212,168,67,0.09)", name="Equity",
        hovertemplate="%{x}<br>Cum P&L: $%{y:+,.2f}<extra></extra>",
    ))
    fig.update_layout(**_chart_layout(height=height))
    return fig


def make_drawdown_chart(history_df: pd.DataFrame, height=160) -> go.Figure:
    fig = go.Figure()
    if history_df.empty or "cumulative_profit" not in history_df.columns:
        fig.add_annotation(text="No data", x=0.5, y=0.5,
                           xref="paper", yref="paper",
                           font=dict(color=MUTED, size=11, family=MONO), showarrow=False)
        fig.update_layout(**_chart_layout(height=height))
        return fig
    cp = history_df["cumulative_profit"]
    dd = cp - cp.cummax()
    fig.add_trace(go.Scatter(
        x=history_df["time"], y=dd,
        fill="tozeroy", line=dict(color=RED, width=1.5),
        fillcolor="rgba(232,69,69,0.09)", name="Drawdown",
        hovertemplate="%{x}<br>DD: $%{y:+,.2f}<extra></extra>",
    ))
    fig.update_layout(**_chart_layout(height=height))
    return fig


def make_pnl_by_session(db_df: pd.DataFrame, height=180) -> go.Figure:
    fig = go.Figure()
    if db_df.empty or "session" not in db_df.columns:
        fig.add_annotation(text="No DB data", x=0.5, y=0.5, xref="paper", yref="paper",
                           font=dict(color=MUTED, size=11, family=MONO), showarrow=False)
        fig.update_layout(**_chart_layout(height=height))
        return fig
    grp = db_df.groupby("session")["pnl_dollars"].sum().reset_index()
    colors = [GREEN if v >= 0 else RED for v in grp["pnl_dollars"]]
    fig.add_trace(go.Bar(
        x=grp["session"], y=grp["pnl_dollars"],
        marker_color=colors,
        hovertemplate="%{x}: $%{y:+,.2f}<extra></extra>",
    ))
    fig.update_layout(**_chart_layout(height=height, title="P&L by Session"))
    return fig


def make_rr_histogram(db_df: pd.DataFrame, height=180) -> go.Figure:
    fig = go.Figure()
    if db_df.empty or "r_multiple" not in db_df.columns:
        fig.add_annotation(text="No DB data", x=0.5, y=0.5, xref="paper", yref="paper",
                           font=dict(color=MUTED, size=11, family=MONO), showarrow=False)
        fig.update_layout(**_chart_layout(height=height))
        return fig
    vals = db_df["r_multiple"].dropna()
    fig.add_trace(go.Histogram(
        x=vals, nbinsx=20,
        marker_color="rgba(255,214,0,0.67)", marker_line=dict(color=GOLD, width=0.5),
        hovertemplate="R: %{x:.2f}  Count: %{y}<extra></extra>",
    ))
    fig.add_vline(x=0, line=dict(color=RED, width=1, dash="dash"))
    fig.update_layout(**_chart_layout(height=height, title="R Multiple Distribution"))
    return fig


def _chart_layout(height=280, title=""):
    l = dict(
        height=height,
        margin=dict(l=8, r=8, t=30 if title else 10, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor=BG,
        font=dict(family=MONO, color=MUTED, size=11),
        xaxis=dict(gridcolor=BORDER, zerolinecolor=BORDER, showgrid=True),
        yaxis=dict(gridcolor=BORDER, zerolinecolor=BORDER, showgrid=True),
    )
    if title:
        l["title"] = dict(text=title, font=dict(color=TEXT, size=12), x=0.01)
    return l


# ── UI HELPERS ────────────────────────────────────────────────────────────────

def card(children, style=None):
    s = {
        "background": CARD, "border": f"1px solid {BORDER}",
        "borderRadius": "10px", "padding": "16px", "marginBottom": "12px",
    }
    if style:
        s.update(style)
    return html.Div(children, style=s)


def badge(text, color=GOLD):
    return html.Span(text, style={
        "background": color + "22", "color": color,
        "border": f"1px solid {color}44", "borderRadius": "4px",
        "padding": "2px 8px", "fontSize": "10px",
        "fontFamily": MONO, "fontWeight": "600",
    })


def stat_box(label, value, color=TEXT, sub=""):
    return html.Div([
        html.Div(label, style={
            "fontFamily": MONO, "fontSize": "9px", "color": MUTED,
            "textTransform": "uppercase", "letterSpacing": "0.12em",
        }),
        html.Div(str(value), style={
            "fontFamily": MONO, "fontSize": "16px", "fontWeight": "700",
            "color": color, "margin": "3px 0 1px",
        }),
        html.Div(sub, style={"fontFamily": SANS, "fontSize": "10px", "color": MUTED}) if sub else html.Span(),
    ], style={"textAlign": "center", "flex": "1", "padding": "0 8px"})


def sec_header(title, badge_text=""):
    return html.Div([
        html.Span(title, style={
            "fontFamily": SANS, "fontWeight": "700", "fontSize": "11px",
            "letterSpacing": "0.12em", "textTransform": "uppercase", "color": TEXT,
        }),
        badge(badge_text) if badge_text else html.Span(),
    ], style={"display": "flex", "alignItems": "center", "gap": "8px", "marginBottom": "10px"})


def dir_color(d):
    return GREEN if d == "BUY" else RED if d == "SELL" else MUTED


def score_color(s):
    return GOLD if s >= 75 else BLUE if s >= 55 else MUTED


# ── SIDEBAR ───────────────────────────────────────────────────────────────────

def build_sidebar(active_page):
    pages = [
        ("metals",    "🥇", "Metals"),
        ("markets",   "◈",  "All Markets"),
        ("chart",     "▦",  "Live Chart"),
        ("scanner",   "⌖",  "ICT Scanner"),
        ("smt",       "⚡", "SMT Divergence"),
        ("pnl",       "📊", "P&L & Equity"),
        ("positions", "📋", "Positions"),
        ("history",   "📜", "Trade History"),
        ("ai",        "⬡",  "AI Engine"),
        ("signals",   "🎯", "Signals"),
    ]

    def nav_item(page_id, icon, label):
        active = active_page == page_id
        return html.Div([
            html.Span(icon, style={"fontSize": "14px"}),
            html.Span(label, style={"fontFamily": SANS, "fontSize": "12px", "fontWeight": "600"}),
        ], id=f"nav-{page_id}", n_clicks=0, style={
            "display": "flex", "alignItems": "center", "gap": "10px",
            "padding": "9px 14px", "borderRadius": "8px", "cursor": "pointer",
            "marginBottom": "3px",
            "color": GOLD if active else MUTED,
            "background": GOLD + "18" if active else "transparent",
            "border": f"1px solid {GOLD}44" if active else "1px solid transparent",
        })

    return html.Div([
        # Logo
        html.Div([
            html.Div([
                html.Div("◈", style={"color": GOLD, "fontSize": "22px"}),
                html.Div([
                    html.Div("OMNI", style={"fontFamily": MONO, "fontWeight": "700",
                                            "fontSize": "15px", "color": TEXT, "letterSpacing": "0.2em"}),
                    html.Div("ICT PRO", style={"fontFamily": SANS, "fontSize": "9px",
                                               "color": GOLD, "letterSpacing": "0.3em"}),
                ]),
            ], style={"display": "flex", "alignItems": "center", "gap": "10px"}),
        ], style={"padding": "18px 16px 14px", "borderBottom": f"1px solid {BORDER}", "marginBottom": "10px"}),

        # Account mini-card
        html.Div(id="sidebar-acct", style={"padding": "0 10px", "marginBottom": "8px"}),

        # Session info
        html.Div(id="sidebar-session", style={"padding": "0 10px", "marginBottom": "10px"}),

        # Nav
        html.Div([
            html.Div("MARKETS", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED,
                                        "letterSpacing": "0.15em", "padding": "4px 16px 4px",
                                        "marginBottom": "3px"}),
            *[nav_item(pid, icon, label) for pid, icon, label in pages[:4]],
            html.Div("ANALYSIS", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED,
                                         "letterSpacing": "0.15em", "padding": "10px 16px 4px",
                                         "marginBottom": "3px"}),
            *[nav_item(pid, icon, label) for pid, icon, label in pages[4:]],
        ], style={"padding": "0 8px", "flex": "1"}),

        # Status dot + clock
        html.Div([
            html.Div(id="mt5-dot"),
            html.Div(id="clock", style={"fontFamily": MONO, "fontSize": "9px",
                                        "color": MUTED, "textAlign": "center", "marginTop": "4px"}),
        ], style={"padding": "12px 16px", "borderTop": f"1px solid {BORDER}",
                  "position": "absolute", "bottom": "0", "width": "100%", "left": "0",
                  "background": CARD}),
    ], style={
        "width": "200px", "minWidth": "200px", "height": "100vh",
        "position": "fixed", "left": "0", "top": "0",
        "background": CARD, "borderRight": f"1px solid {BORDER}",
        "overflowY": "auto", "zIndex": "50", "display": "flex",
        "flexDirection": "column", "paddingBottom": "60px",
    })


# ── DASH APP ──────────────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    title="OMNI ICT Dashboard",
    update_title=None,
    external_stylesheets=[
        "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700"
        "&family=Sora:wght@400;500;600;700&display=swap"
    ],
    suppress_callback_exceptions=True,
)

app.layout = html.Div([
    dcc.Interval(id="interval", interval=REFRESH_MS, n_intervals=0),
    dcc.Store(id="active-page", data="metals"),
    dcc.Store(id="active-sym",  data="XAUUSD"),
    dcc.Store(id="active-tf",   data="H1"),

    # Sidebar
    html.Div(id="sidebar-container"),

    # Main content
    html.Div([
        # Alert bar
        html.Div(id="alert-bar"),
        # Page content
        html.Div(id="page-content", style={"padding": "20px 24px"}),
    ], style={"marginLeft": "200px", "minHeight": "100vh"}),

], style={"backgroundColor": BG, "minHeight": "100vh",
          "fontFamily": SANS, "color": TEXT})


# ── PAGE RENDERERS ─────────────────────────────────────────────────────────────

def page_metals(mt5, ai_state, trader_st, active_sym="XAUUSD", active_tf="H1"):
    charts_data = mt5.get("charts", {})
    prices_list = mt5.get("prices", [])
    prices_map  = {p["symbol"]: p for p in prices_list}
    session     = mt5.get("session", "—")
    amd         = mt5.get("amd_phase", "—")
    account     = mt5.get("account", {})
    cur_str     = account.get("currency", "USD")

    def metal_hero(sym, color, icon):
        p    = prices_map.get(sym, {})
        sym_info = charts_data.get(sym, {})
        bars_raw = sym_info.get(active_tf, sym_info.get("H1", []))
        bid  = p.get("bid", bars_raw[0].get("c", 0) if bars_raw else 0)
        ask  = p.get("ask", 0)
        sprd = p.get("spread", 0)
        a    = _cached_analyze(bars_raw, sym, sym_info, active_tf)
        dc   = dir_color(a["direction"])
        d    = a["digits"]
        fmt  = f"{{:.{d}f}}"

        return html.Div([
            html.Div([
                html.Div([
                    html.Span(icon, style={"fontSize": "26px"}),
                    html.Div([
                        html.Div(sym, style={"fontFamily": MONO, "fontWeight": "700",
                                             "fontSize": "18px", "color": color}),
                        html.Div("Gold / USD" if sym == "XAUUSD" else "Silver / USD",
                                 style={"fontFamily": SANS, "fontSize": "11px", "color": MUTED}),
                    ]),
                ], style={"display": "flex", "alignItems": "center", "gap": "12px"}),
                html.Div([
                    html.Div(fmt.format(bid) if bid else "—",
                             style={"fontFamily": MONO, "fontSize": "24px",
                                    "fontWeight": "700", "color": color, "textAlign": "right"}),
                    html.Div(f"Sprd {sprd}",
                             style={"fontFamily": MONO, "fontSize": "10px",
                                    "color": MUTED, "textAlign": "right"}),
                ]),
            ], style={"display": "flex", "justifyContent": "space-between",
                      "alignItems": "flex-start", "marginBottom": "12px"}),

            # Score bar
            html.Div([
                html.Div([
                    html.Span(a["direction"], style={"fontFamily": MONO, "fontSize": "12px",
                                                     "fontWeight": "700", "color": dc}),
                    html.Span(f"  {a['score']}/100", style={"fontFamily": MONO,
                                                              "fontSize": "11px", "color": GOLD}),
                ], style={"display": "flex", "justifyContent": "space-between", "marginBottom": "5px"}),
                html.Div(style={"height": "4px", "borderRadius": "2px", "background": BORDER},
                         children=[html.Div(style={"height": "4px", "borderRadius": "2px",
                                                    "width": f"{a['score']}%",
                                                    "background": f"linear-gradient(90deg,{dc},{color})"})]),
            ], style={"marginBottom": "10px"}),

            # ICT stats row
            html.Div([
                *[html.Div([
                    html.Div(lbl, style={"fontFamily": MONO, "fontSize": "9px",
                                         "color": MUTED, "textTransform": "uppercase",
                                         "letterSpacing": "0.1em"}),
                    html.Div(val, style={"fontFamily": MONO, "fontSize": "11px",
                                         "fontWeight": "600", "color": vc}),
                ], style={"textAlign": "center"})
                    for lbl, val, vc in [
                        ("Trend",   a["trend"],   GREEN if a["trend"] == "BULLISH" else RED if a["trend"] == "BEARISH" else MUTED),
                        ("RSI",     str(a["rsi"]), GREEN if a["rsi"] < 35 else RED if a["rsi"] > 65 else MUTED),
                        ("OB",      a["ob_type"].replace("_OB", "").replace("NONE", "—"),
                                    GREEN if "BULL" in a["ob_type"] else RED if "BEAR" in a["ob_type"] else MUTED),
                        ("FVG",     a["fvg_type"][:4] if a["fvg_type"] != "NONE" else "—", BLUE if a["fvg_type"] != "NONE" else MUTED),
                        ("Struct",  a["structure"].replace("_", " ")[:9],
                                    GREEN if "BULL" in a["structure"] else RED if "BEAR" in a["structure"] else MUTED),
                    ]
                ],
            ], style={"display": "flex", "justifyContent": "space-between",
                      "background": CARD2, "borderRadius": "8px",
                      "padding": "10px", "marginBottom": "10px"}),

            # Asia range + Entry plan
            html.Div([
                html.Span(f"Asia: {a['asia_l']:.{d}f} — {a['asia_h']:.{d}f}",
                          style={"fontFamily": MONO, "fontSize": "10px", "color": BLUE}),
            ], style={"marginBottom": "6px"}) if a["asia_h"] else html.Div(),
            html.Div([
                html.Span(f"Entry {fmt.format(a['cur'])}  ",
                          style={"fontFamily": MONO, "fontSize": "10px", "color": MUTED}),
                html.Span(f"SL {fmt.format(a['sl'])}  ",
                          style={"fontFamily": MONO, "fontSize": "10px", "color": RED}),
                html.Span(f"TP {fmt.format(a['tp'])}  RR {a['rr']}",
                          style={"fontFamily": MONO, "fontSize": "10px", "color": GREEN}),
            ], style={"marginBottom": "8px"}),
            html.Div([html.Div(r, style={"fontFamily": SANS, "fontSize": "10px",
                                          "color": MUTED, "marginBottom": "3px"})
                      for r in a["reasons"][:4]]),
        ], style={
            "background": f"linear-gradient(135deg,{CARD} 0%,{color}08 100%)",
            "border": f"1px solid {color}44", "borderRadius": "12px",
            "padding": "18px", "flex": "1", "minWidth": "280px",
        })

    # Account row
    acct_vals = [
        ("Balance",     f"{account.get('balance',0):,.2f} {cur_str}", GOLD),
        ("Equity",      f"{account.get('equity',0):,.2f} {cur_str}", GOLD),
        ("Free Margin", f"{account.get('free_margin',0):,.2f}", TEXT),
        ("Open P&L",    f"{account.get('profit',0):+,.2f}",
                         GREEN if account.get("profit", 0) >= 0 else RED),
        ("Leverage",    f"1:{account.get('leverage',1)}", MUTED),
        ("Session",     session, SESS_COLORS.get(session, MUTED)),
    ]

    # TF + symbol controls for the main chart
    def _btn(label, btn_id, active_val, current_val, color=GOLD):
        is_active = current_val == active_val
        return html.Button(label, id=btn_id, n_clicks=0, style={
            "background": color + "22" if is_active else CARD2,
            "color": color if is_active else MUTED,
            "border": f"1px solid {color+'44' if is_active else BORDER}",
            "borderRadius": "6px", "padding": "5px 12px",
            "fontFamily": MONO, "fontSize": "11px", "cursor": "pointer",
        })

    tf_controls = html.Div([
        html.Span("TF:", style={"fontFamily": MONO, "fontSize": "10px",
                                 "color": MUTED, "marginRight": "6px"}),
        *[_btn(tf, f"tf-btn-{tf}", tf, active_tf)
          for tf in ["M1", "M5", "M15", "H1", "H4", "D1", "W1"]],
    ], style={"display": "flex", "alignItems": "center", "gap": "6px", "flexWrap": "wrap"})

    sym_controls = html.Div([
        html.Span("Chart:", style={"fontFamily": MONO, "fontSize": "10px",
                                    "color": MUTED, "marginRight": "6px"}),
        *[_btn(s, f"sym-btn-{s}", s, active_sym, GOLD if s == "XAUUSD" else SILVER)
          for s in ["XAUUSD", "XAGUSD"]],
    ], style={"display": "flex", "alignItems": "center", "gap": "6px"})

    # Build main chart with active sym + tf
    chart_sym_info = charts_data.get(active_sym, {})
    chart_bars_full = chart_sym_info.get(active_tf, [])
    chart_analysis = _cached_analyze(chart_bars_full, active_sym, chart_sym_info, active_tf)
    bar_count = len(chart_bars_full)
    chart_bars = _trim_bars(chart_bars_full)

    return html.Div([
        html.Div([
            html.H2("🥇 Metals Dashboard", style={"fontFamily": SANS, "fontWeight": "700",
                                                   "fontSize": "20px", "color": GOLD, "margin": "0"}),
            html.P(f"Gold & Silver — ICT Smart Money | {amd} | {mt5.get('gmt_time','')[:16]}",
                   style={"fontFamily": SANS, "fontSize": "12px", "color": MUTED, "margin": "3px 0 0"}),
        ], style={"marginBottom": "14px"}),

        # Account stats bar
        html.Div([
            *[html.Div([
                html.Div(l, style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED,
                                    "textTransform": "uppercase", "letterSpacing": "0.1em"}),
                html.Div(v, style={"fontFamily": MONO, "fontSize": "13px",
                                    "fontWeight": "700", "color": c, "marginTop": "2px"}),
            ], style={"textAlign": "center", "flex": "1"})
              for l, v, c in acct_vals
            ],
        ], style={"display": "flex", "background": CARD, "border": f"1px solid {BORDER}",
                  "borderRadius": "10px", "padding": "14px", "marginBottom": "14px",
                  "alignItems": "center", "gap": "4px", "flexWrap": "wrap"}),

        # Metal hero cards
        html.Div([
            metal_hero("XAUUSD", GOLD, "🥇"),
            metal_hero("XAGUSD", SILVER, "🥈"),
        ], style={"display": "flex", "gap": "14px", "flexWrap": "wrap", "marginBottom": "14px"}),

        # Interactive chart — responds to TF + symbol switcher
        card([
            html.Div([
                html.Div([
                    sym_controls,
                    tf_controls,
                ], style={"display": "flex", "gap": "16px", "alignItems": "center",
                          "flexWrap": "wrap", "marginBottom": "10px"}),
                html.Div([
                    html.Span(f"{active_sym} / {active_tf}",
                              style={"fontFamily": MONO, "fontSize": "12px",
                                     "color": GOLD, "fontWeight": "700"}),
                    html.Span(f"  {bar_count} bars",
                              style={"fontFamily": MONO, "fontSize": "10px", "color": MUTED}),
                    html.Span(f"  {chart_analysis['direction']}",
                              style={"fontFamily": MONO, "fontSize": "11px",
                                     "color": dir_color(chart_analysis['direction']),
                                     "fontWeight": "700", "marginLeft": "10px"}),
                    html.Span(f"  {chart_analysis['score']}/100",
                              style={"fontFamily": MONO, "fontSize": "10px",
                                     "color": score_color(chart_analysis['score'])}),
                ]),
            ]),
            dcc.Graph(
                figure=make_candle_chart(
                    chart_bars, chart_analysis, active_sym, chart_sym_info, height=420,
                ),
                config={"displayModeBar": True,
                        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                        "scrollZoom": True},
            ),
        ]),
    ])


def page_markets(mt5, active_sym, active_tf):
    charts      = mt5.get("charts", {})
    prices_list = mt5.get("prices", [])
    prices      = {p["symbol"]: p for p in prices_list}

    # ── Build analysis for ALL symbols ───────────────────────────────
    rows = []
    seen = set()

    # Chart symbols → full OHLCV analysis
    for sym in charts.keys():
        seen.add(sym)
        sym_info = charts[sym]
        tf_used = active_tf if active_tf in sym_info else "H1"
        bars_raw = sym_info.get(tf_used, [])
        a = _cached_analyze(bars_raw, sym, sym_info, tf_used)
        p = prices.get(sym, {})
        bid   = p.get("bid", a["cur"])
        sprd  = p.get("spread", 0)
        conf  = a["ob_fvg_conf"]
        pats  = ", ".join(pt["pattern"] for pt in a.get("patterns", [])[:2]) or "—"
        rows.append({
            "Symbol": sym, "Bid": f"{bid:.{a['digits']}f}",
            "Spread": sprd, "Signal": a["direction"],
            "Score": a["score"], "RSI": a["rsi"],
            "Trend": a["trend"],
            "FVG": a["fvg_type"][:4] if a["fvg_type"] != "NONE" else "—",
            "OB": a["ob_type"].replace("_OB","")[:4] if a["ob_type"] != "NONE" else "—",
            "Confluence": f"⚡ {conf}" if conf != "NONE" else "—",
            "Patterns": pats,
            "Structure": a["structure"].replace("_"," ")[:10],
            "RR": a["rr"],
            "Data": "FULL",
        })

    # Prices-only symbols → lightweight analysis
    for p in prices_list:
        sym = p.get("symbol", "")
        if not sym or sym in seen:
            continue
        a = _analysis_from_prices(p)
        conf = a["ob_fvg_conf"]
        rows.append({
            "Symbol": sym, "Bid": f"{a['cur']:.{a['digits']}f}",
            "Spread": p.get("spread", 0), "Signal": a["direction"],
            "Score": a["score"], "RSI": a["rsi"],
            "Trend": a["trend"],
            "FVG": a["fvg_type"][:4] if a["fvg_type"] != "NONE" else "—",
            "OB": a["ob_type"].replace("_OB","")[:4] if a["ob_type"] != "NONE" else "—",
            "Confluence": f"⚡ {conf}" if conf != "NONE" else "—",
            "Patterns": "—",
            "Structure": a["structure"].replace("_"," ")[:10],
            "RR": "—",
            "Data": "LITE",
        })

    rows.sort(key=lambda x: x["Score"], reverse=True)

    # ── Mini pairs grid (compact cards for all symbols) ───────────────
    def mini_pair_card(r):
        sig = r["Signal"]
        dc  = dir_color(sig)
        sc  = score_color(r["Score"])
        conf_show = r["Confluence"] != "—"
        return html.Div([
            html.Div(r["Symbol"], style={"fontFamily": MONO, "fontWeight": "700",
                                          "fontSize": "11px", "color": TEXT,
                                          "marginBottom": "3px"}),
            html.Div(r["Bid"], style={"fontFamily": MONO, "fontSize": "12px",
                                      "fontWeight": "700", "color": dc}),
            html.Div([
                html.Span(sig, style={"fontFamily": MONO, "fontSize": "9px",
                                       "color": dc, "fontWeight": "700"}),
                html.Span(f" {r['Score']}", style={"fontFamily": MONO, "fontSize": "9px",
                                                    "color": sc}),
            ], style={"display": "flex", "gap": "4px", "marginTop": "2px"}),
            html.Div([
                html.Span(f"RSI {r['RSI']}", style={"fontFamily": MONO, "fontSize": "8px",
                                                      "color": MUTED}),
                html.Span(" ⚡" if conf_show else "", style={"color": GOLD, "fontSize": "9px"}),
            ], style={"display": "flex", "gap": "4px", "marginTop": "1px"}),
            # Score bar
            html.Div(style={"marginTop": "4px", "height": "2px",
                             "borderRadius": "1px", "background": BORDER},
                     children=[html.Div(style={
                         "height": "2px", "borderRadius": "1px",
                         "width": f"{r['Score']}%",
                         "background": f"linear-gradient(90deg,{dc},{sc})",
                     })]),
        ], style={
            "background": CARD2,
            "border": f"1px solid {GOLD+'55' if conf_show else BORDER}",
            "borderRadius": "8px", "padding": "10px 12px",
            "minWidth": "110px", "flex": "1",
        })

    grid = html.Div(
        [mini_pair_card(r) for r in rows],
        style={
            "display": "flex", "flexWrap": "wrap", "gap": "8px",
            "marginBottom": "14px",
        }
    )

    tf_buttons = html.Div([
        html.Span("Timeframe: ", style={"fontFamily": MONO, "fontSize": "10px",
                                         "color": MUTED, "marginRight": "8px"}),
        *[html.Button(tf, id=f"tf-btn-{tf}", n_clicks=0, style={
            "background": GOLD + "22" if tf == active_tf else CARD2,
            "color": GOLD if tf == active_tf else MUTED,
            "border": f"1px solid {GOLD+'44' if tf == active_tf else BORDER}",
            "borderRadius": "6px", "padding": "5px 11px",
            "fontFamily": MONO, "fontSize": "11px", "cursor": "pointer", "marginRight": "6px",
        }) for tf in ["M5", "M15", "H1", "H4", "D1"]],
    ], style={"display": "flex", "alignItems": "center", "marginBottom": "12px", "flexWrap": "wrap"})

    n_conf = len([r for r in rows if r["Confluence"] != "—"])
    n_sig  = len([r for r in rows if r["Signal"] != "NEUTRAL"])

    return html.Div([
        html.Div([
            html.H2("◈ All Markets", style={"fontFamily": MONO, "fontWeight": "700",
                                             "fontSize": "18px", "color": TEXT, "margin": "0"}),
            html.P(f"{len(rows)} instruments | {n_sig} signals | {n_conf} OB+FVG confluences | TF: {active_tf}",
                   style={"fontSize": "12px", "color": MUTED, "margin": "3px 0 0"}),
        ], style={"marginBottom": "12px"}),
        tf_buttons,

        # Pairs overview grid
        card([
            sec_header("Pairs Overview", f"{len(rows)} instruments"),
            grid,
        ]),

        # Full signal table
        card([
            sec_header("ICT Signals", f"{n_sig} active"),
            dash_table.DataTable(
                data=rows,
                columns=[{"name": c, "id": c} for c in rows[0].keys()] if rows else [],
                style_table={"overflowX": "auto"},
                style_header=TH,
                style_cell=TC,
                style_data_conditional=[
                    {"if": {"filter_query": '{Signal} = "BUY"',  "column_id": "Signal"},
                     "color": GREEN, "fontWeight": "700"},
                    {"if": {"filter_query": '{Signal} = "SELL"', "column_id": "Signal"},
                     "color": RED,   "fontWeight": "700"},
                    {"if": {"filter_query": '{Trend} = "BULLISH"', "column_id": "Trend"},
                     "color": GREEN},
                    {"if": {"filter_query": '{Trend} = "BEARISH"', "column_id": "Trend"},
                     "color": RED},
                    {"if": {"filter_query": "{Score} >= 75", "column_id": "Score"},
                     "color": GOLD, "fontWeight": "700"},
                    {"if": {"filter_query": "{Score} >= 55", "column_id": "Score"},
                     "color": BLUE},
                    {"if": {"filter_query": '{FVG} != "—"', "column_id": "FVG"},
                     "color": BLUE, "fontWeight": "600"},
                    {"if": {"filter_query": '{Confluence} != "—"', "column_id": "Confluence"},
                     "color": GOLD, "fontWeight": "700"},
                    {"if": {"filter_query": '{Patterns} != "—"', "column_id": "Patterns"},
                     "color": ORANGE, "fontWeight": "600"},
                    {"if": {"filter_query": '{Data} = "LITE"', "column_id": "Data"},
                     "color": MUTED},
                    {"if": {"filter_query": '{Data} = "FULL"', "column_id": "Data"},
                     "color": GREEN},
                ],
                sort_action="native", filter_action="native", page_size=30,
                row_selectable="single",
            ),
        ]),
    ])


def page_chart(mt5, active_sym, active_tf):
    charts   = mt5.get("charts", {})
    prices_l = {p["symbol"]: p for p in mt5.get("prices", [])}
    syms     = list(charts.keys())
    sym_info = charts.get(active_sym, {})
    bars_raw = sym_info.get(active_tf, [])
    analysis = _cached_analyze(bars_raw, active_sym, sym_info, active_tf)
    p        = prices_l.get(active_sym, {})
    bid      = p.get("bid", analysis["cur"])
    d        = analysis["digits"]

    sym_dropdown = dcc.Dropdown(
        id="chart-sym-select",
        options=[{"label": s, "value": s} for s in syms],
        value=active_sym, clearable=False,
        style={"background": CARD2, "color": TEXT, "border": f"1px solid {BORDER}",
               "borderRadius": "6px", "fontFamily": MONO, "fontSize": "12px",
               "width": "140px"},
    )
    tf_btns = [
        html.Button(tf, id=f"tf-btn-{tf}", n_clicks=0, style={
            "background": GOLD + "22" if tf == active_tf else CARD2,
            "color": GOLD if tf == active_tf else MUTED,
            "border": f"1px solid {GOLD+'44' if tf == active_tf else BORDER}",
            "borderRadius": "6px", "padding": "5px 12px",
            "fontFamily": MONO, "fontSize": "11px", "cursor": "pointer",
        }) for tf in ["M5", "M15", "H1", "H4", "D1"]
    ]

    dc = dir_color(analysis["direction"])

    return html.Div([
        # Controls
        html.Div([
            html.Div([sym_dropdown, *tf_btns],
                     style={"display": "flex", "gap": "8px", "alignItems": "center", "flexWrap": "wrap"}),
        ], style={"display": "flex", "justifyContent": "space-between",
                  "alignItems": "center", "marginBottom": "12px", "flexWrap": "wrap", "gap": "10px"}),

        # Price header
        html.Div([
            html.Div([
                html.Div(f"{bid:.{d}f}", style={"fontFamily": MONO, "fontWeight": "700",
                                                  "fontSize": "22px", "color": TEXT}),
                html.Div(f"{active_sym} / {active_tf}",
                         style={"fontSize": "11px", "color": MUTED}),
            ]),
            html.Div(style={"height": "40px", "width": "1px", "background": BORDER}),
            *[html.Div([
                html.Div(lbl, style={"fontFamily": MONO, "fontSize": "9px",
                                     "color": MUTED, "textTransform": "uppercase"}),
                html.Div(val, style={"fontFamily": MONO, "fontSize": "12px",
                                     "fontWeight": "700", "color": vc, "marginTop": "2px"}),
            ], style={"textAlign": "center"}) for lbl, val, vc in [
                ("Signal",    analysis["direction"],                    dc),
                ("Score",     f"{analysis['score']}/100",              score_color(analysis["score"])),
                ("RSI",       str(analysis["rsi"]),                    GREEN if analysis["rsi"] < 35 else RED if analysis["rsi"] > 65 else MUTED),
                ("RR",        f"{analysis['rr']}:1",                   GREEN if analysis["rr"] >= 2 else GOLD),
                ("Structure", analysis["structure"].replace("_"," ")[:9], BLUE),
                ("OB",        analysis["ob_type"].replace("_OB","").replace("NONE","—"), GREEN if "BULL" in analysis["ob_type"] else RED if "BEAR" in analysis["ob_type"] else MUTED),
                ("FVG",       analysis["fvg_type"][:4] if analysis["fvg_type"] != "NONE" else "—", BLUE if analysis["fvg_type"] != "NONE" else MUTED),
            ]],
        ], style={"display": "flex", "alignItems": "center", "gap": "16px", "flexWrap": "wrap",
                  "padding": "12px 16px", "background": CARD,
                  "borderRadius": "10px", "marginBottom": "12px",
                  "border": f"1px solid {BORDER}"}),

        # Chart
        card([
            dcc.Graph(
                figure=make_candle_chart(_trim_bars(bars_raw), analysis, active_sym, sym_info, height=380),
                config={"displayModeBar": True,
                        "modeBarButtonsToRemove": ["lasso2d", "select2d"]},
            ),
        ], style={"padding": "10px"}),

        # Analysis panels
        html.Div([
            # Trade setup
            card([
                sec_header("Trade Setup"),
                *[html.Div([
                    html.Span(lbl, style={"fontFamily": MONO, "fontSize": "10px", "color": MUTED}),
                    html.Span(val, style={"fontFamily": MONO, "fontSize": "10px",
                                          "fontWeight": "700", "color": vc}),
                ], style={"display": "flex", "justifyContent": "space-between",
                           "padding": "4px 0", "borderBottom": f"1px solid {BORDER}"})
                   for lbl, val, vc in [
                       ("Entry",  f"{bid:.{d}f}",              GOLD),
                       ("SL",     f"{analysis['sl']:.{d}f}",   RED),
                       ("TP",     f"{analysis['tp']:.{d}f}",   GREEN),
                       ("RR",     f"{analysis['rr']}:1",        GREEN if analysis['rr'] >= 2 else GOLD),
                       ("ATR",    f"{analysis['atr']:.{d}f}",  MUTED),
                       ("Trend",  analysis["trend"],            GREEN if analysis["trend"] == "BULLISH" else RED if analysis["trend"] == "BEARISH" else MUTED),
                       ("PDH",    f"{sym_info.get('pdh',0):.{d}f}", SILVER),
                       ("PDL",    f"{sym_info.get('pdl',0):.{d}f}", SILVER),
                   ]
                ],
            ]),

            # Signal reasons
            card([
                sec_header("ICT Confluence"),
                *[html.Div([
                    html.Span("▸ ", style={"color": dc, "fontSize": "8px"}),
                    html.Span(r, style={"fontFamily": MONO, "fontSize": "10px",
                                         "color": MUTED, "lineHeight": "1.5"}),
                ], style={"display": "flex", "gap": "8px", "padding": "4px 0",
                           "borderBottom": f"1px solid {BORDER}"})
                   for r in analysis["reasons"]
                ],
                html.Div([
                    html.Span(f"{analysis['direction']} — ",
                              style={"fontFamily": MONO, "fontSize": "11px",
                                     "fontWeight": "700", "color": dc}),
                    html.Span(
                        "HIGH CONFIDENCE" if analysis["score"] >= 75 else
                        "MEDIUM CONFIDENCE" if analysis["score"] >= 55 else "LOW",
                        style={"fontFamily": MONO, "fontSize": "10px", "color": dc},
                    ),
                ], style={"marginTop": "10px", "padding": "8px 10px",
                          "background": dc + "12", "borderRadius": "6px",
                          "border": f"1px solid {dc}33"}),
            ]),

            # Multi-TF bar counts
            card([
                sec_header("Bar Data"),
                *[html.Div([
                    html.Span(tf, style={"fontFamily": MONO, "fontSize": "10px", "color": MUTED, "minWidth": "30px"}),
                    html.Span(f"{len(sym_info.get(tf, []))} bars",
                              style={"fontFamily": MONO, "fontSize": "10px",
                                     "color": GREEN if sym_info.get(tf) else RED}),
                ], style={"display": "flex", "gap": "12px", "padding": "3px 0"})
                   for tf in ["D1", "H4", "H1", "M15", "M5"]
                ],
            ]),
        ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr 1fr", "gap": "10px"}),
    ])


def page_scanner(mt5):
    charts    = mt5.get("charts", {})
    session   = mt5.get("session", "—")
    amd       = mt5.get("amd_phase", "—")
    prices_l  = {p["symbol"]: p for p in mt5.get("prices", [])}
    amd_col   = AMD_COLORS.get(amd, MUTED)

    all_setups = []
    for sym, sym_info in charts.items():
        bars_raw = sym_info.get("H1", sym_info.get("H4", []))
        a = _cached_analyze(bars_raw, sym, sym_info, "H1")
        p = prices_l.get(sym, {})
        a = dict(a)  # copy so we don't mutate cache
        a["bid"] = p.get("bid", a["cur"])
        a["spread"] = p.get("spread", 0)
        all_setups.append(a)

    all_setups.sort(key=lambda x: x["score"], reverse=True)
    high_conf = [s for s in all_setups if s["score"] >= 55 and s["direction"] != "NEUTRAL"]

    # Top setup cards
    def setup_card(a):
        dc = dir_color(a["direction"])
        d  = a["digits"]
        return html.Div([
            html.Div([
                html.Span(a["sym"], style={"fontFamily": MONO, "fontWeight": "700",
                                            "fontSize": "13px", "color": TEXT, "minWidth": "80px"}),
                html.Span(a["direction"], style={"fontFamily": MONO, "fontSize": "11px",
                                                  "color": dc, "fontWeight": "700", "marginLeft": "8px"}),
            ], style={"minWidth": "140px", "display": "flex", "alignItems": "center"}),
            html.Div([
                html.Div(style={"height": "3px", "borderRadius": "2px",
                                "width": f"{a['score']}%",
                                "background": f"linear-gradient(90deg,{dc},{GOLD})",
                                "marginBottom": "2px"}),
                html.Span(f"{a['score']}/100", style={"fontFamily": MONO, "fontSize": "9px", "color": GOLD}),
            ], style={"flex": "1", "margin": "0 12px"}),
            html.Div([
                html.Span(f"E:{a['bid']:.{d}f} ",
                          style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED}),
                html.Span(f"SL:{a['sl']:.{d}f} ",
                          style={"fontFamily": MONO, "fontSize": "9px", "color": RED}),
                html.Span(f"TP:{a['tp']:.{d}f}",
                          style={"fontFamily": MONO, "fontSize": "9px", "color": GREEN}),
            ], style={"minWidth": "210px"}),
            html.Div([
                html.Div(r, style={"fontSize": "9px", "color": MUTED, "fontFamily": SANS})
                for r in a["reasons"][:2]
            ], style={"flex": "1"}),
        ], style={"display": "flex", "alignItems": "center", "padding": "10px 0",
                  "borderBottom": f"1px solid {BORDER}", "gap": "8px"})

    tbl_rows = [{
        "Symbol": a["sym"], "Signal": a["direction"],
        "Score": a["score"], "Entry": f"{a['bid']:.{a['digits']}f}",
        "SL": f"{a['sl']:.{a['digits']}f}", "TP": f"{a['tp']:.{a['digits']}f}",
        "RR": a["rr"], "RSI": a["rsi"], "Trend": a["trend"],
        "OB": a["ob_type"].replace("_OB","").replace("NONE","—"),
        "FVG": a["fvg_type"][:4] if a["fvg_type"] != "NONE" else "—",
        "Structure": a["structure"].replace("_"," ")[:10],
    } for a in all_setups]

    return html.Div([
        html.Div([
            html.H2("⌖ ICT Scanner", style={"fontFamily": SANS, "fontWeight": "700",
                                              "fontSize": "18px", "color": TEXT, "margin": "0"}),
            html.Div([
                html.Span(f"⏱ {amd}", style={"fontFamily": MONO, "fontSize": "11px",
                                               "color": amd_col, "fontWeight": "700"}),
                html.Span(f"  {session}  {mt5.get('gmt_time','')[:16]}",
                          style={"fontFamily": MONO, "fontSize": "10px", "color": MUTED}),
            ], style={"marginTop": "4px"}),
        ], style={"marginBottom": "14px"}),

        card([
            sec_header("High Confidence Setups", f"{len(high_conf)} signals"),
            html.Div([setup_card(a) for a in high_conf[:6]] if high_conf
                     else [html.P("No high-confidence setups at this time.",
                                  style={"color": MUTED, "fontFamily": MONO, "fontSize": "12px"})]),
        ]),

        card([
            sec_header("All Signals", str(len(tbl_rows))),
            dash_table.DataTable(
                data=tbl_rows,
                columns=[{"name": c, "id": c} for c in tbl_rows[0].keys()] if tbl_rows else [],
                style_table={"overflowX": "auto"},
                style_header=TH, style_cell=TC,
                style_data_conditional=[
                    {"if": {"filter_query": '{Signal} = "BUY"',  "column_id": "Signal"}, "color": GREEN, "fontWeight": "700"},
                    {"if": {"filter_query": '{Signal} = "SELL"', "column_id": "Signal"}, "color": RED,   "fontWeight": "700"},
                    {"if": {"filter_query": "{Score} >= 75",     "column_id": "Score"},  "color": GOLD,  "fontWeight": "700"},
                    {"if": {"filter_query": "{Score} >= 55",     "column_id": "Score"},  "color": BLUE},
                    {"if": {"filter_query": '{Trend} = "BULLISH"', "column_id": "Trend"}, "color": GREEN},
                    {"if": {"filter_query": '{Trend} = "BEARISH"', "column_id": "Trend"}, "color": RED},
                    {"if": {"filter_query": '{FVG} != "—"', "column_id": "FVG"}, "color": BLUE, "fontWeight": "600"},
                ],
                sort_action="native", filter_action="native", page_size=20,
            ) if tbl_rows else html.P("No data", style={"color": MUTED}),
        ]),
    ])


def page_pnl(mt5):
    history_df = pd.DataFrame()
    try:
        hist = mt5.get("history", [])
        if hist:
            history_df = pd.DataFrame(hist)
            history_df["time"] = pd.to_datetime(history_df["time"])
            history_df = history_df.sort_values("time")
            history_df["cumulative_profit"] = history_df["profit"].cumsum()
    except Exception:
        pass

    db_df = load_db_trades(200)
    total_pnl = history_df["profit"].sum() if not history_df.empty else 0
    n_trades  = len(history_df) if not history_df.empty else 0

    return html.Div([
        html.H2("📊 P&L & Equity", style={"fontFamily": SANS, "fontWeight": "700",
                                            "fontSize": "18px", "color": TEXT,
                                            "margin": "0 0 14px"}),
        # Summary
        html.Div([
            stat_box("Total P&L", f"${total_pnl:+,.2f}",
                     GREEN if total_pnl >= 0 else RED, "All history"),
            html.Div(style={"width": "1px", "background": BORDER}),
            stat_box("Trades", str(n_trades), TEXT, "MT5 history"),
            html.Div(style={"width": "1px", "background": BORDER}),
            stat_box("DB Trades", str(len(db_df)), GOLD, "SQLite"),
        ], style={"display": "flex", "background": CARD, "border": f"1px solid {BORDER}",
                  "borderRadius": "10px", "padding": "14px", "marginBottom": "14px",
                  "alignItems": "center"}),

        card([
            sec_header("Equity Curve", "MT5 history"),
            dcc.Graph(figure=make_equity_curve(history_df), config={"displayModeBar": False}),
        ]),
        card([
            sec_header("Drawdown"),
            dcc.Graph(figure=make_drawdown_chart(history_df), config={"displayModeBar": False}),
        ]),
        html.Div([
            card([sec_header("P&L by Session"), dcc.Graph(figure=make_pnl_by_session(db_df), config={"displayModeBar": False})]),
            card([sec_header("R Multiple Distribution"), dcc.Graph(figure=make_rr_histogram(db_df), config={"displayModeBar": False})]),
        ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "12px"}),
    ])


def _bot_trades_card():
    """Card showing bot-tracked active_trades from trader_state.json."""
    trader_st    = load_trader_state()
    active_trades = trader_st.get("active_trades", {})
    if not active_trades:
        return card([
            sec_header("Bot-Tracked Trades", "0 open"),
            html.P("No bot-tracked trades.", style={"color": MUTED, "fontFamily": MONO, "fontSize": "12px"}),
        ])
    cols = ["symbol", "direction", "entry_price", "sl_price", "tp1_price", "volume", "opened_at"]
    rows = []
    for tid, t in active_trades.items():
        row = {"ticket": tid}
        for c in cols:
            row[c] = t.get(c, "")
        rows.append(row)
    all_cols = ["ticket"] + cols
    return card([
        sec_header("Bot-Tracked Trades", f"{len(rows)} open"),
        dash_table.DataTable(
            data=rows,
            columns=[{"name": c.replace("_", " ").upper(), "id": c} for c in all_cols],
            style_table={"overflowX": "auto"},
            style_header=TH, style_cell=TC,
            style_data_conditional=[
                {"if": {"filter_query": '{direction} = "BUY"',  "column_id": "direction"}, "color": GREEN},
                {"if": {"filter_query": '{direction} = "SELL"', "column_id": "direction"}, "color": RED},
            ],
            sort_action="native", page_size=20,
        ),
    ])


def page_positions(mt5):
    positions = mt5.get("positions", [])
    account   = mt5.get("account", {})
    cur       = account.get("currency", "USD")
    total_pnl = sum(p.get("profit", 0) for p in positions)

    return html.Div([
        html.H2("📋 Open Positions", style={"fontFamily": SANS, "fontWeight": "700",
                                             "fontSize": "18px", "color": TEXT, "margin": "0 0 14px"}),
        html.Div([
            stat_box("Open Positions", len(positions), GOLD),
            html.Div(style={"width": "1px", "background": BORDER}),
            stat_box("Floating P&L", f"${total_pnl:+,.2f}",
                     GREEN if total_pnl >= 0 else RED),
            html.Div(style={"width": "1px", "background": BORDER}),
            stat_box("Balance", f"{account.get('balance', 0):,.2f} {cur}", GOLD),
            html.Div(style={"width": "1px", "background": BORDER}),
            stat_box("Equity",  f"{account.get('equity',  0):,.2f} {cur}", GOLD),
            html.Div(style={"width": "1px", "background": BORDER}),
            stat_box("Margin Level", f"{account.get('margin_level', 0):.1f}%",
                     GREEN if account.get("margin_level", 100) > 200 else RED),
        ], style={"display": "flex", "background": CARD, "border": f"1px solid {BORDER}",
                  "borderRadius": "10px", "padding": "14px", "marginBottom": "14px",
                  "alignItems": "center", "flexWrap": "wrap"}),

        card([
            sec_header("Live Positions", f"{len(positions)} open"),
            dash_table.DataTable(
                data=positions,
                columns=[{"name": c.replace("_", " ").upper(), "id": c}
                         for c in ["ticket", "symbol", "type", "volume", "open_price",
                                   "current_price", "sl", "tp", "profit", "swap", "time"]
                         if positions and c in positions[0]
                         ] if positions else [],
                style_table={"overflowX": "auto"},
                style_header=TH, style_cell=TC,
                style_data_conditional=[
                    {"if": {"filter_query": '{type} = "BUY"',  "column_id": "type"}, "color": GREEN},
                    {"if": {"filter_query": '{type} = "SELL"', "column_id": "type"}, "color": RED},
                    {"if": {"filter_query": "{profit} > 0",    "column_id": "profit"}, "color": GREEN},
                    {"if": {"filter_query": "{profit} < 0",    "column_id": "profit"}, "color": RED},
                ],
                sort_action="native", page_size=20,
            ) if positions else html.P("No open positions.",
                                       style={"color": MUTED, "fontFamily": MONO, "fontSize": "12px"}),
        ]),
        _bot_trades_card(),
    ])


def page_history(mt5):
    hist = mt5.get("history", [])
    db_df = load_db_trades(100)

    hist_df = pd.DataFrame()
    if hist:
        hist_df = pd.DataFrame(hist)
        hist_df["time"] = hist_df["time"].astype(str)

    return html.Div([
        html.H2("📜 Trade History", style={"fontFamily": SANS, "fontWeight": "700",
                                            "fontSize": "18px", "color": TEXT, "margin": "0 0 14px"}),
        html.Div([
            card([
                sec_header("MT5 Deal History", f"{len(hist_df)} deals"),
                dash_table.DataTable(
                    data=hist_df.to_dict("records") if not hist_df.empty else [],
                    columns=[{"name": c.replace("_", " ").upper(), "id": c}
                             for c in hist_df.columns] if not hist_df.empty else [],
                    style_table={"overflowX": "auto"},
                    style_header=TH, style_cell=TC,
                    style_data_conditional=[
                        {"if": {"filter_query": "{profit} > 0", "column_id": "profit"}, "color": GREEN},
                        {"if": {"filter_query": "{profit} < 0", "column_id": "profit"}, "color": RED},
                        {"if": {"filter_query": '{type} = "BUY"',  "column_id": "type"}, "color": GREEN},
                        {"if": {"filter_query": '{type} = "SELL"', "column_id": "type"}, "color": RED},
                    ],
                    sort_action="native", filter_action="native", page_size=15,
                ) if not hist_df.empty else html.P("No MT5 history.",
                                                   style={"color": MUTED, "fontFamily": MONO}),
            ]),
            card([
                sec_header("DB Trade Log", f"{len(db_df)} trades"),
                dash_table.DataTable(
                    data=db_df.to_dict("records") if not db_df.empty else [],
                    columns=[{"name": c.replace("_", " ").upper(), "id": c}
                             for c in db_df.columns] if not db_df.empty else [],
                    style_table={"overflowX": "auto"},
                    style_header=TH, style_cell=TC,
                    style_data_conditional=[
                        {"if": {"filter_query": '{outcome} = "WIN"',  "column_id": "outcome"}, "color": GREEN, "fontWeight": "700"},
                        {"if": {"filter_query": '{outcome} = "LOSS"', "column_id": "outcome"}, "color": RED,   "fontWeight": "700"},
                        {"if": {"filter_query": "{pnl_dollars} > 0",  "column_id": "pnl_dollars"}, "color": GREEN},
                        {"if": {"filter_query": "{pnl_dollars} < 0",  "column_id": "pnl_dollars"}, "color": RED},
                        {"if": {"filter_query": "{r_multiple} > 0",   "column_id": "r_multiple"},  "color": GREEN},
                        {"if": {"filter_query": "{r_multiple} < 0",   "column_id": "r_multiple"},  "color": RED},
                    ],
                    sort_action="native", filter_action="native", page_size=15,
                ) if not db_df.empty else html.P("No DB trades yet. Trades are logged here automatically.",
                                                  style={"color": MUTED, "fontFamily": MONO}),
            ]),
        ]),
    ])


def page_smt(mt5):
    charts   = mt5.get("charts", {})
    prices_l = {p["symbol"]: p for p in mt5.get("prices", [])}
    amd      = mt5.get("amd_phase", "—")

    SMT_PAIRS = [
        ("XAUUSD", "XAGUSD", "bullish", "Metals"),
        ("EURUSD", "GBPUSD", "bullish", "Cable/Fiber"),
        ("AUDUSD", "NZDUSD", "bullish", "Commodity FX"),
        ("EURUSD", "USDCHF", "bearish", "EUR inverse CHF"),
        ("GBPUSD", "USDCAD", "bearish", "GBP inverse CAD"),
    ]

    smt_cards = []
    for sym_a, sym_b, corr, group in SMT_PAIRS:
        si_a = charts.get(sym_a, {})
        si_b = charts.get(sym_b, {})
        if not si_a or not si_b:
            continue
        a_a = _cached_analyze(si_a.get("H1", []), sym_a, si_a, "H1")
        a_b = _cached_analyze(si_b.get("H1", []), sym_b, si_b, "H1")
        pa = prices_l.get(sym_a, {})
        pb = prices_l.get(sym_b, {})

        diverging = False
        div_msg   = ""
        if corr == "bullish":
            if a_a["trend"] == "BULLISH" and a_b["trend"] == "BEARISH":
                diverging = True
                div_msg = f"{sym_a} BULLISH but {sym_b} BEARISH — SMT divergence. Favor {sym_a}."
            elif a_a["trend"] == "BEARISH" and a_b["trend"] == "BULLISH":
                diverging = True
                div_msg = f"{sym_b} BULLISH but {sym_a} BEARISH — SMT divergence. Favor {sym_b}."
            if a_a["rsi"] < 35 and a_b["rsi"] > 50:
                diverging = True
                div_msg += f" RSI: {sym_a} oversold ({a_a['rsi']}) vs {sym_b} neutral ({a_b['rsi']})."
        elif corr == "bearish":
            if a_a["trend"] == "BULLISH" and a_b["trend"] == "BULLISH":
                diverging = True
                div_msg = f"{sym_a} and {sym_b} both BULLISH — inverse pair alignment. Watch for reversal."

        border_col = GOLD if diverging else BORDER
        smt_cards.append(html.Div([
            html.Div([
                html.Span(sym_a, style={"fontFamily": MONO, "fontWeight": "700", "color": BLUE}),
                html.Span(" ⚡ ", style={"color": GOLD}),
                html.Span(sym_b, style={"fontFamily": MONO, "fontWeight": "700", "color": PURPLE}),
                badge("DIVERGING", GOLD) if diverging else badge(group, MUTED),
            ], style={"display": "flex", "alignItems": "center", "gap": "8px", "marginBottom": "8px"}),

            html.Div([
                html.Div([
                    html.Div(sym_a, style={"fontFamily": MONO, "fontSize": "10px", "color": MUTED}),
                    html.Div(f"{pa.get('bid', a_a['cur']):.{a_a['digits']}f}",
                             style={"fontFamily": MONO, "fontWeight": "700", "fontSize": "13px",
                                    "color": dir_color(a_a["direction"])}),
                    html.Div(f"RSI {a_a['rsi']} | {a_a['trend']}",
                             style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED}),
                ]),
                html.Div("vs", style={"color": MUTED, "fontFamily": MONO, "fontSize": "11px",
                                      "alignSelf": "center"}),
                html.Div([
                    html.Div(sym_b, style={"fontFamily": MONO, "fontSize": "10px", "color": MUTED}),
                    html.Div(f"{pb.get('bid', a_b['cur']):.{a_b['digits']}f}",
                             style={"fontFamily": MONO, "fontWeight": "700", "fontSize": "13px",
                                    "color": dir_color(a_b["direction"])}),
                    html.Div(f"RSI {a_b['rsi']} | {a_b['trend']}",
                             style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED}),
                ]),
            ], style={"display": "flex", "justifyContent": "space-around",
                      "marginBottom": "8px"}),

            html.P(div_msg if div_msg else f"Correlated pair — both should move {corr}. No divergence.",
                   style={"fontFamily": SANS, "fontSize": "11px",
                          "color": GOLD if diverging else MUTED, "margin": "0"}),
        ], style={
            "background": CARD2 if diverging else CARD,
            "border": f"1px solid {border_col}",
            "borderRadius": "10px", "padding": "14px", "marginBottom": "10px",
        }))

    return html.Div([
        html.H2("⚡ SMT Divergence", style={"fontFamily": SANS, "fontWeight": "700",
                                             "fontSize": "18px", "color": TEXT,
                                             "margin": "0 0 14px"}),
        html.Div(smt_cards) if smt_cards
        else html.P("No chart data available.", style={"color": MUTED, "fontFamily": MONO}),
    ])


def page_signals(mt5, trader_st):
    top_setups    = trader_st.get("top_setups", [])
    active_trades = trader_st.get("active_trades", {})
    session       = mt5.get("session", "—")
    amd_phase     = mt5.get("amd_phase", "—")

    # ── Status bar ────────────────────────────────────────────────────
    risk_mode  = trader_st.get("risk_mode", "—")
    freq_mode  = trader_st.get("freq_mode", "—")
    open_cnt   = len(active_trades)
    max_open   = trader_st.get("max_open", "—")
    min_conf   = trader_st.get("min_conf", "—")
    min_rr     = trader_st.get("min_rr", "—")

    sess_color = GOLD if session in ("LONDON", "NEW_YORK", "NY_CLOSE") else MUTED

    status_bar = html.Div([
        stat_box("Session",    session,   sess_color),
        html.Div(style={"width": "1px", "background": BORDER}),
        stat_box("AMD Phase",  amd_phase, GOLD),
        html.Div(style={"width": "1px", "background": BORDER}),
        stat_box("Risk Mode",  risk_mode, ORANGE),
        html.Div(style={"width": "1px", "background": BORDER}),
        stat_box("Freq Mode",  freq_mode, BLUE),
        html.Div(style={"width": "1px", "background": BORDER}),
        stat_box("Open Trades", f"{open_cnt}/{max_open}", GREEN if open_cnt < max_open else RED),
        html.Div(style={"width": "1px", "background": BORDER}),
        stat_box("Min Conf",   f"{min_conf}%", MUTED),
        html.Div(style={"width": "1px", "background": BORDER}),
        stat_box("Min RR",     f"{min_rr}R", MUTED),
    ], style={"display": "flex", "background": CARD, "border": f"1px solid {BORDER}",
              "borderRadius": "10px", "padding": "14px", "marginBottom": "14px",
              "alignItems": "center", "flexWrap": "wrap"})

    # ── Active Trades section ─────────────────────────────────────────
    active_section = []
    if active_trades:
        trade_cards = []
        for tid, t in active_trades.items():
            sym_label = t.get("symbol", str(tid))
            direction = t.get("direction", "—")
            dir_color = GREEN if direction == "BUY" else RED
            pnl = t.get("pnl", t.get("unrealized_pnl", 0))
            pnl_color = GREEN if pnl >= 0 else RED
            trade_cards.append(html.Div([
                html.Div([
                    html.Span(direction, style={"background": dir_color + "22", "color": dir_color,
                                                "border": f"1px solid {dir_color}44",
                                                "borderRadius": "4px", "padding": "2px 8px",
                                                "fontSize": "11px", "fontFamily": MONO, "fontWeight": "700"}),
                    html.Span(sym_label, style={"fontFamily": MONO, "fontWeight": "700",
                                                "fontSize": "14px", "color": TEXT, "marginLeft": "8px"}),
                    html.Span(f"P&L: {pnl:+.2f}", style={"fontFamily": MONO, "fontSize": "12px",
                                                           "color": pnl_color, "marginLeft": "auto"}),
                ], style={"display": "flex", "alignItems": "center", "marginBottom": "6px"}),
                html.Div([
                    html.Span(f"Entry: {t.get('entry_price', '—')}", style={"fontFamily": MONO, "fontSize": "11px", "color": "#00BFFF", "marginRight": "10px"}),
                    html.Span(f"SL: {t.get('sl_price', '—')}", style={"fontFamily": MONO, "fontSize": "11px", "color": "#FF4444", "marginRight": "10px"}),
                    html.Span(f"TP1: {t.get('tp1_price', '—')}", style={"fontFamily": MONO, "fontSize": "11px", "color": "#00FF88", "marginRight": "10px"}),
                    html.Span(f"TP2: {t.get('tp2_price', '—')}", style={"fontFamily": MONO, "fontSize": "11px", "color": "#00FF44"}),
                ], style={"display": "flex", "flexWrap": "wrap", "gap": "4px"}),
            ], style={"background": CARD2, "border": f"1px solid {BORDER}", "borderRadius": "8px",
                      "padding": "10px 14px", "marginBottom": "8px"}))
        active_section = [
            sec_header("🟢 Active Trades", f"{len(active_trades)} open"),
            html.Div(trade_cards),
        ]

    # ── Live Pattern Detection ───────────────────────────────────────
    pattern_section = []
    if _HAS_ICT and mt5.get("charts"):
        PATTERN_SYMBOLS = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]
        all_patterns = []
        for sym in PATTERN_SYMBOLS:
            charts = mt5.get("charts", {}).get(sym, {})
            h1_raw = charts.get("H1", [])[-30:]
            m15_raw = charts.get("M15", [])[-30:]
            h1_bars = ict._parse_bars(h1_raw) if h1_raw else []
            m15_bars = ict._parse_bars(m15_raw) if m15_raw else []
            if h1_bars:
                found = ict.detect_all_patterns(h1_bars, short_bars=m15_bars)
                for p in found:
                    p["_symbol"] = sym
                all_patterns.extend(found)

        if all_patterns:
            # Group by pattern type
            pat_categories = {
                "Classic Reversal": ["DOUBLE_TOP", "DOUBLE_BOTTOM", "HEAD_SHOULDERS",
                                     "INVERSE_HEAD_SHOULDERS", "TRIPLE_TOP", "TRIPLE_BOTTOM",
                                     "RISING_WEDGE", "FALLING_WEDGE"],
                "Continuation": ["BULL_FLAG", "BEAR_FLAG", "PENNANT",
                                 "ASCENDING_TRIANGLE", "DESCENDING_TRIANGLE",
                                 "RECTANGLE_BREAKOUT", "RECTANGLE_BREAKDOWN"],
                "Bilateral": ["SYMMETRICAL_TRIANGLE"],
                "Candlestick": ["BULLISH_ENGULFING", "BEARISH_ENGULFING",
                                "BULLISH_PIN_BAR", "BEARISH_PIN_BAR", "DOJI",
                                "MORNING_STAR", "EVENING_STAR", "INSIDE_BAR",
                                "TWEEZER_TOP", "TWEEZER_BOTTOM",
                                "THREE_WHITE_SOLDIERS", "THREE_BLACK_CROWS"],
            }
            pattern_rows = []
            for p in sorted(all_patterns, key=lambda x: -x.get("confidence_bonus", 0)):
                pname = p.get("pattern", "")
                pdir = p.get("direction", "")
                bonus = p.get("confidence_bonus", 0)
                sym = p.get("_symbol", "")
                dir_c = GREEN if pdir == "BUY" else RED
                # Determine category
                base_name = pname.replace("_M15", "")
                cat = "Other"
                for c, names in pat_categories.items():
                    if base_name in names:
                        cat = c
                        break
                tf_label = " (M15)" if "_M15" in pname else " (H1)"
                pattern_rows.append(html.Div([
                    html.Span(sym, style={"fontFamily": MONO, "fontWeight": "700",
                                          "fontSize": "11px", "color": TEXT, "minWidth": "65px"}),
                    html.Span(pdir, style={"background": dir_c + "22", "color": dir_c,
                                           "border": f"1px solid {dir_c}44", "borderRadius": "3px",
                                           "padding": "1px 6px", "fontSize": "10px",
                                           "fontFamily": MONO, "fontWeight": "700", "minWidth": "40px",
                                           "textAlign": "center"}),
                    html.Span(base_name.replace("_", " ") + tf_label, style={
                        "fontFamily": MONO, "fontSize": "11px", "color": GOLD, "flex": "1"}),
                    html.Span(cat, style={"fontFamily": MONO, "fontSize": "9px",
                                          "color": MUTED, "minWidth": "80px"}),
                    html.Span(f"+{bonus}", style={"fontFamily": MONO, "fontSize": "11px",
                                                   "color": GREEN, "fontWeight": "600",
                                                   "minWidth": "30px", "textAlign": "right"}),
                ], style={"display": "flex", "alignItems": "center", "gap": "8px",
                          "padding": "4px 10px",
                          "borderBottom": f"1px solid {BORDER}"}))

            pattern_section = [card([
                sec_header("Live Pattern Detection", f"{len(all_patterns)} patterns across {len(PATTERN_SYMBOLS)} symbols"),
                html.Div([
                    html.Div([
                        html.Span("Symbol", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED, "minWidth": "65px"}),
                        html.Span("Dir", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED, "minWidth": "40px", "textAlign": "center"}),
                        html.Span("Pattern", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED, "flex": "1"}),
                        html.Span("Category", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED, "minWidth": "80px"}),
                        html.Span("Bonus", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED, "minWidth": "30px", "textAlign": "right"}),
                    ], style={"display": "flex", "alignItems": "center", "gap": "8px",
                              "padding": "4px 10px", "borderBottom": f"1px solid {BORDER}",
                              "marginBottom": "2px"}),
                    *pattern_rows,
                ], style={"maxHeight": "350px", "overflowY": "auto"}),
            ])]

    # ── Signal cards ─────────────────────────────────────────────────
    def _grade_color(g):
        return {
            "A+": PURPLE, "A": BLUE, "B+": GOLD,
        }.get(g, MUTED)

    def _conf_color(c):
        if c >= 70:   return GREEN
        if c >= 55:   return GOLD
        return RED

    signal_cards = []
    for setup in top_setups[:8]:
        sym       = setup.get("symbol", "")
        direction = setup.get("direction", "—")
        grade     = setup.get("grade", "—")
        etype     = setup.get("entry_type", "")
        conf      = setup.get("confidence", 0)
        rr        = setup.get("rr_ratio", 0)
        entry_p   = setup.get("entry_price", 0)
        sl_p      = setup.get("sl_price", 0)
        tp1_p     = setup.get("tp1_price", 0)
        tp2_p     = setup.get("tp2_price", 0)
        sess_s    = setup.get("session", "")
        phase     = setup.get("amd_phase", "")
        tf_bias   = setup.get("tf_bias", "")
        reasons   = setup.get("reasons", [])[:6]
        inval     = setup.get("invalidation", 0)

        dir_color   = GREEN if direction == "BUY" else RED
        conf_color  = _conf_color(conf)
        grade_color = _grade_color(grade)
        rr_color    = GREEN if rr >= 2 else GOLD if rr >= 1.5 else RED

        # Mini candle chart (last 100 bars only — full 5000 would freeze the browser)
        bars_for_sym = mt5.get("charts", {}).get(sym, {}).get("H1", [])[-100:]
        if bars_for_sym and isinstance(bars_for_sym, list):
            chart_elem = dcc.Graph(
                figure=make_candle_chart(
                    bars_for_sym, None, sym=sym, sym_info={},
                    height=200,
                    entry=entry_p if entry_p else None,
                    sl=sl_p if sl_p else None,
                    tp1=tp1_p if tp1_p else None,
                    tp2=tp2_p if tp2_p else None,
                ),
                config={"displayModeBar": False},
                style={"marginTop": "10px"},
            )
        else:
            chart_elem = html.Div("No H1 bar data available",
                                  style={"textAlign": "center", "color": MUTED,
                                         "fontFamily": MONO, "fontSize": "11px",
                                         "padding": "20px 0", "marginTop": "10px"})

        # Confidence bar
        conf_bar = html.Div([
            html.Div(style={
                "width": f"{conf}%", "height": "6px",
                "background": conf_color, "borderRadius": "3px",
                "transition": "width 0.3s",
            }),
        ], style={"background": BORDER, "borderRadius": "3px", "height": "6px",
                  "flex": "1", "marginRight": "8px"})

        signal_cards.append(card([
            # Header row
            html.Div([
                html.Span(direction, style={
                    "background": dir_color + "22", "color": dir_color,
                    "border": f"1px solid {dir_color}44", "borderRadius": "4px",
                    "padding": "3px 10px", "fontSize": "12px",
                    "fontFamily": MONO, "fontWeight": "700",
                }),
                html.Span(sym, style={"fontFamily": MONO, "fontWeight": "700",
                                      "fontSize": "16px", "color": TEXT, "marginLeft": "8px"}),
                html.Span(grade, style={
                    "background": grade_color + "22", "color": grade_color,
                    "border": f"1px solid {grade_color}44", "borderRadius": "4px",
                    "padding": "2px 7px", "fontSize": "10px",
                    "fontFamily": MONO, "fontWeight": "600", "marginLeft": "8px",
                }),
                html.Span(etype, style={"fontFamily": MONO, "fontSize": "10px",
                                        "color": MUTED, "marginLeft": "8px"}),
            ], style={"display": "flex", "alignItems": "center", "marginBottom": "10px",
                      "flexWrap": "wrap", "gap": "4px"}),
            # Confidence bar
            html.Div([
                conf_bar,
                html.Span(f"{conf}%", style={"fontFamily": MONO, "fontSize": "11px",
                                              "color": conf_color, "fontWeight": "600",
                                              "minWidth": "36px", "textAlign": "right"}),
            ], style={"display": "flex", "alignItems": "center", "marginBottom": "10px"}),
            # Key metrics
            html.Div([
                html.Div([html.Div("ENTRY", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED}),
                          html.Div(f"{entry_p:.5g}", style={"fontFamily": MONO, "fontSize": "12px", "color": "#00BFFF"})]),
                html.Div([html.Div("SL", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED}),
                          html.Div(f"{sl_p:.5g}", style={"fontFamily": MONO, "fontSize": "12px", "color": "#FF4444"})]),
                html.Div([html.Div("TP1", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED}),
                          html.Div(f"{tp1_p:.5g}", style={"fontFamily": MONO, "fontSize": "12px", "color": "#00FF88"})]),
                html.Div([html.Div("TP2", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED}),
                          html.Div(f"{tp2_p:.5g}", style={"fontFamily": MONO, "fontSize": "12px", "color": "#00FF44"})]),
                html.Div([html.Div("RR", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED}),
                          html.Div(f"{rr:.2f}R", style={"fontFamily": MONO, "fontSize": "12px", "color": rr_color, "fontWeight": "700"})]),
            ], style={"display": "flex", "gap": "16px", "marginBottom": "8px", "flexWrap": "wrap"}),
            # Session / Phase
            html.Div([
                html.Span(sess_s, style={"background": SESS_COLORS.get(sess_s, MUTED) + "22",
                                         "color": SESS_COLORS.get(sess_s, MUTED),
                                         "border": f"1px solid {SESS_COLORS.get(sess_s, MUTED)}44",
                                         "borderRadius": "4px", "padding": "2px 7px",
                                         "fontSize": "10px", "fontFamily": MONO, "marginRight": "6px"}),
                html.Span(phase, style={"background": AMD_COLORS.get(phase, MUTED) + "22",
                                        "color": AMD_COLORS.get(phase, MUTED),
                                        "border": f"1px solid {AMD_COLORS.get(phase, MUTED)}44",
                                        "borderRadius": "4px", "padding": "2px 7px",
                                        "fontSize": "10px", "fontFamily": MONO}),
            ], style={"marginBottom": "8px"}) if sess_s or phase else html.Span(),
            # TF Bias
            html.Div(f"TF Bias: {tf_bias}", style={"fontFamily": MONO, "fontSize": "11px",
                                                     "color": MUTED, "marginBottom": "6px"}) if tf_bias else html.Span(),
            # Reasons
            html.Ul([
                html.Li(r, style={"fontFamily": MONO, "fontSize": "11px", "color": MUTED,
                                  "marginBottom": "2px"})
                for r in reasons
            ], style={"paddingLeft": "16px", "margin": "0 0 6px"}) if reasons else html.Span(),
            # Invalidation
            html.Div(f"⚠ Invalidation: {inval:.5g}", style={"fontFamily": MONO, "fontSize": "10px",
                                                               "color": RED, "marginTop": "4px"}) if inval else html.Span(),
            # Mini chart
            chart_elem,
        ]))

    if not top_setups:
        signals_content = html.Div("No setups detected — scanner running...",
                                   style={"textAlign": "center", "color": MUTED,
                                          "fontFamily": MONO, "fontSize": "13px", "padding": "40px"})
    else:
        signals_content = html.Div(signal_cards, style={
            "display": "grid",
            "gridTemplateColumns": "repeat(auto-fill, minmax(380px, 1fr))",
            "gap": "16px",
        })

    return html.Div([
        html.H2("🎯 Signals", style={"fontFamily": SANS, "fontWeight": "700",
                                      "fontSize": "18px", "color": TEXT, "margin": "0 0 14px"}),
        status_bar,
        *([card(active_section)] if active_section else []),
        *pattern_section,
        card([
            sec_header("Pending Signals", f"{len(top_setups)} detected"),
            signals_content,
        ]),
    ])


def page_ai(ai_state, trader_st, mt5):
    regime  = ai_state.get("regime", {})
    params  = ai_state.get("params", {})
    signals = ai_state.get("signals", [])
    insight = ai_state.get("insight", "AI engine not running. Start ai_engine.py.")
    ts      = ai_state.get("timestamp", "")[:16].replace("T", " ")

    # Colors
    def rcol(p):
        return {"TRENDING": GREEN, "RANGING": BLUE, "VOLATILE": RED,
                "QUIET": MUTED, "REVERSAL": GOLD, "BREAKOUT": GREEN}.get(p, MUTED)
    def bcol(b):
        return {MUTED: MUTED, "BULLISH": GREEN, "BEARISH": RED, "NEUTRAL": MUTED}.get(b, MUTED)

    # Trader state stats
    win_rate = (trader_st.get("winning_trades", 0) /
                max(trader_st.get("total_trades", 1), 1) * 100)
    halted   = trader_st.get("trading_halted", False)

    def mini_card(label, val, color):
        return html.Div([
            html.Div(label, style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED,
                                    "textTransform": "uppercase", "letterSpacing": "0.08em",
                                    "marginBottom": "2px"}),
            html.Span(str(val), style={"fontFamily": MONO, "fontSize": "14px",
                                        "fontWeight": "700", "color": color}),
        ], style={"background": CARD, "border": f"1px solid {BORDER}",
                  "borderTop": f"2px solid {color}44", "borderRadius": "6px",
                  "padding": "10px 14px", "minWidth": "110px"})

    param_grid = html.Div([
        html.Div([
            html.P(lbl, style={"margin": "0 0 2px", "fontSize": "9px", "textTransform": "uppercase",
                               "letterSpacing": "0.08em", "color": MUTED, "fontFamily": SANS}),
            html.Span(str(val), style={"fontFamily": MONO, "fontSize": "13px",
                                        "color": GOLD, "fontWeight": "600"}),
        ], style={"background": CARD2, "borderRadius": "4px", "padding": "8px 10px",
                  "border": f"1px solid {BORDER}"})
        for lbl, val in [
            ("Risk %",    f"{params.get('base_risk_pct', 2):.1f}%"),
            ("Min RR",    f"{params.get('min_rr', 1.5):.1f}"),
            ("Min Conf",  f"{params.get('min_confidence', 55)}%"),
            ("Max Open",  str(params.get("max_open", 3))),
            ("Daily Lim", f"{params.get('daily_loss_limit', 6):.1f}%"),
            ("TP1 Split", f"{int(params.get('tp1_pct', 0.5)*100)}%"),
            ("Priority",  ", ".join(params.get("priority_setups", [])[:2]) or "All"),
            ("Skip Sess", ", ".join(params.get("skip_session", [])) or "None"),
        ]
    ], style={"display": "grid", "gridTemplateColumns": "repeat(auto-fill,minmax(140px,1fr))", "gap": "8px"})

    def sig_row(s):
        v  = s.get("ai_verdict", "")
        vc = {"STRONG_BUY": GREEN, "BUY": GREEN, "NEUTRAL": MUTED,
              "AVOID": GOLD, "SELL": RED, "STRONG_SELL": RED}.get(v, MUTED)
        dc = dir_color(s.get("direction", "NEUTRAL"))
        return html.Div([
            html.Span(f"{s.get('symbol','')} {s.get('direction','')}",
                      style={"fontFamily": MONO, "fontSize": "11px", "color": dc,
                             "fontWeight": "600", "minWidth": "100px"}),
            html.Span(s.get("entry_type", ""),
                      style={"fontFamily": MONO, "fontSize": "10px", "color": MUTED, "minWidth": "70px"}),
            html.Span(v, style={"fontFamily": MONO, "fontSize": "11px",
                                 "color": vc, "fontWeight": "700", "minWidth": "90px"}),
            html.Span(f"{s.get('ai_confidence', 0)}%",
                      style={"fontFamily": MONO, "fontSize": "10px", "color": PURPLE, "minWidth": "45px"}),
            html.Span(s.get("ai_reasoning", "")[:80],
                      style={"fontFamily": MONO, "fontSize": "10px", "color": MUTED, "flex": "1"}),
        ], style={"display": "flex", "gap": "12px", "alignItems": "center",
                  "padding": "7px 0", "borderBottom": f"1px solid {BORDER}"})

    return html.Div([
        html.Div([
            html.H2([html.Span("⬡ ", style={"color": PURPLE}),
                     html.Span("AI Engine", style={"color": TEXT})],
                    style={"fontFamily": SANS, "fontWeight": "700", "fontSize": "18px", "margin": "0"}),
            html.P(f"Market regime analysis — {ts} UTC",
                   style={"fontFamily": SANS, "fontSize": "12px", "color": MUTED, "margin": "3px 0 0"}),
        ], style={"marginBottom": "14px"}),

        # ── Trading Mode Panel ────────────────────────────────────────
        html.Div([
            html.Div("TRADING MODES", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED,
                                              "letterSpacing": "0.15em", "marginBottom": "10px"}),
            html.Div([
                # Risk mode badge
                html.Div([
                    html.Div("RISK MODE", style={"fontFamily": MONO, "fontSize": "9px",
                                                  "color": MUTED, "letterSpacing": "0.1em",
                                                  "marginBottom": "4px"}),
                    html.Div(trader_st.get("risk_mode", "—"), style={
                        "fontFamily": MONO, "fontSize": "16px", "fontWeight": "700",
                        "color": RED if trader_st.get("risk_mode") == "HIGH"
                                 else GOLD if trader_st.get("risk_mode") == "MODERATE"
                                 else GREEN,
                    }),
                    html.Div(f"Base {trader_st.get('base_risk','?')}%  "
                             f"Max {trader_st.get('max_risk','?')}%  "
                             f"Min {trader_st.get('min_risk','?')}%",
                             style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED,
                                    "marginTop": "3px"}),
                    html.Div(f"Daily limit {trader_st.get('daily_limit','?')}%  "
                             f"Max DD {trader_st.get('max_dd','?')}%",
                             style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED}),
                ], style={"background": CARD2, "border": f"1px solid {BORDER}",
                          "borderRadius": "8px", "padding": "12px 16px", "flex": "1"}),

                # Freq mode badge
                html.Div([
                    html.Div("FREQUENCY MODE", style={"fontFamily": MONO, "fontSize": "9px",
                                                       "color": MUTED, "letterSpacing": "0.1em",
                                                       "marginBottom": "4px"}),
                    html.Div(trader_st.get("freq_mode", "—"), style={
                        "fontFamily": MONO, "fontSize": "16px", "fontWeight": "700",
                        "color": RED if trader_st.get("freq_mode") == "AGGRESSIVE"
                                 else GOLD if trader_st.get("freq_mode") == "NORMAL"
                                 else GREEN,
                    }),
                    html.Div(f"Conf≥{trader_st.get('min_conf','?')}  "
                             f"RR≥{trader_st.get('min_rr','?')}  "
                             f"Max {trader_st.get('max_open','?')} open",
                             style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED,
                                    "marginTop": "3px"}),
                ], style={"background": CARD2, "border": f"1px solid {BORDER}",
                          "borderRadius": "8px", "padding": "12px 16px", "flex": "1"}),

                # How to change hint
                html.Div([
                    html.Div("CHANGE MODES", style={"fontFamily": MONO, "fontSize": "9px",
                                                     "color": MUTED, "letterSpacing": "0.1em",
                                                     "marginBottom": "6px"}),
                    *[html.Div(line, style={"fontFamily": MONO, "fontSize": "9px",
                                            "color": MUTED, "marginBottom": "2px"})
                      for line in [
                          "python auto_trader.py",
                          "  --risk LOW|MODERATE|HIGH",
                          "  --freq CONSERVATIVE|NORMAL|AGGRESSIVE",
                          "",
                          "Or set env vars:",
                          "  OMNI_RISK_MODE=HIGH",
                          "  OMNI_FREQ_MODE=AGGRESSIVE",
                      ]],
                ], style={"background": CARD2, "border": f"1px solid {BORDER}",
                          "borderRadius": "8px", "padding": "12px 16px", "flex": "1.5"}),
            ], style={"display": "flex", "gap": "10px", "flexWrap": "wrap"}),
        ], style={"background": CARD, "border": f"1px solid {BORDER}", "borderRadius": "10px",
                  "padding": "14px", "marginBottom": "12px"}),

        # ── Bot State Stats ───────────────────────────────────────────
        html.Div([
            html.Div("BOT STATE", style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED,
                                          "letterSpacing": "0.15em", "marginBottom": "8px"}),
            html.Div([
                mini_card("Risk %",   f"{trader_st.get('current_risk_pct', 2):.1f}%", GOLD),
                mini_card("Total Trades", trader_st.get("total_trades", 0), TEXT),
                mini_card("Win Rate", f"{win_rate:.0f}%", GREEN if win_rate >= 55 else GOLD if win_rate >= 45 else RED),
                mini_card("W Streak", trader_st.get("win_streak", 0),  GREEN),
                mini_card("L Streak", trader_st.get("loss_streak", 0), RED),
                mini_card("Total P&L", f"${trader_st.get('total_profit', 0):+,.2f}",
                          GREEN if trader_st.get("total_profit", 0) >= 0 else RED),
                mini_card("Max DD",   f"{trader_st.get('peak_drawdown', 0):.1f}%", MUTED),
                mini_card("Halted",   "YES" if halted else "NO", RED if halted else GREEN),
            ], style={"display": "flex", "gap": "10px", "flexWrap": "wrap"}),
            html.P(trader_st.get("halt_reason", ""),
                   style={"fontFamily": MONO, "fontSize": "10px", "color": RED,
                          "marginTop": "6px"}) if halted else html.Span(),
        ], style={"background": CARD, "border": f"1px solid {BORDER}", "borderRadius": "10px",
                  "padding": "14px", "marginBottom": "12px"}),

        # Regime
        card([
            sec_header("Market Regime"),
            html.Div([
                mini_card("Phase",      regime.get("phase", "—"),      rcol(regime.get("phase", ""))),
                mini_card("Bias",       regime.get("bias", "—"),       bcol(regime.get("bias", ""))),
                mini_card("AMD Stage",  regime.get("amd_stage", "—"),  ORANGE),
                mini_card("Volatility", regime.get("volatility", "—"), BLUE),
                mini_card("Session",    regime.get("session", "—"),    MUTED),
                mini_card("Confidence", f"{regime.get('confidence', 0)}%", PURPLE),
                *(([mini_card("Killzone", "ACTIVE ⚡", GREEN)]) if regime.get("killzone_active") else []),
                *(([mini_card("SMT Div",  "FORMING ⚠", ORANGE)]) if regime.get("smt_divergence") else []),
            ], style={"display": "flex", "gap": "10px", "flexWrap": "wrap"}),
        ]),

        # Briefing
        card([
            sec_header("AI Briefing"),
            html.Pre(insight, style={"fontFamily": MONO, "fontSize": "11px", "color": TEXT,
                                      "lineHeight": "1.7", "whiteSpace": "pre-wrap", "margin": "0",
                                      "borderLeft": f"2px solid {PURPLE}",
                                      "paddingLeft": "12px"}),
        ]),

        # Params
        card([
            sec_header("Adapted Parameters", f"rationale: {params.get('ai_rationale','')[:50]}"),
            param_grid,
        ]),

        # Signals
        card([
            sec_header("Signal Evaluation", f"{len(signals)} symbols"),
            html.Div([sig_row(s) for s in reversed(signals[-10:])] if signals
                     else [html.P("No signals yet. Start ai_engine.py.",
                                  style={"color": MUTED, "fontFamily": MONO, "fontSize": "12px"})]),
        ]),
    ])


# ── CALLBACKS ─────────────────────────────────────────────────────────────────

@callback(Output("sidebar-container", "children"),
          Input("active-page", "data"))
def update_sidebar(page):
    return build_sidebar(page)


@callback(Output("clock", "children"),
          Input("interval", "n_intervals"))
def update_clock(_):
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


@callback(
    Output("sidebar-acct",    "children"),
    Output("sidebar-session", "children"),
    Output("mt5-dot",         "children"),
    Input("interval", "n_intervals"),
)
def update_sidebar_live(_):
    mt5  = load_mt5()
    acct = mt5.get("account", {})
    cur  = acct.get("currency", "USD")
    pnl  = acct.get("profit", 0)
    sess = mt5.get("session", "—")
    amd  = mt5.get("amd_phase", "—")
    sc   = SESS_COLORS.get(sess, MUTED)
    ac   = AMD_COLORS.get(amd, MUTED)
    ok   = mt5_connected()

    acct_div = html.Div([
        html.Div(acct.get("name", "")[:18] if acct else "Not connected",
                 style={"fontFamily": SANS, "fontSize": "11px", "color": MUTED, "marginBottom": "3px"}),
        html.Div(f"{acct.get('balance',0):,.2f} {cur}",
                 style={"fontFamily": MONO, "fontSize": "13px", "fontWeight": "700", "color": GOLD}),
        html.Div(f"P&L {pnl:+,.2f}",
                 style={"fontFamily": MONO, "fontSize": "11px",
                        "color": GREEN if pnl >= 0 else RED, "marginTop": "2px"}),
    ], style={"background": CARD2, "borderRadius": "8px", "padding": "10px 12px",
              "border": f"1px solid {BORDER}"})

    sess_div = html.Div([
        html.Div([html.Span("⏱ "),
                  html.Span(amd, style={"fontFamily": MONO, "fontSize": "10px",
                                         "color": ac, "fontWeight": "700"})],
                 style={"marginBottom": "3px"}),
        html.Div([html.Span("🌐 "),
                  html.Span(sess, style={"fontFamily": MONO, "fontSize": "10px",
                                          "color": sc, "fontWeight": "700"})],
                 style={"marginBottom": "1px"}),
        html.Div(mt5.get("gmt_time", "")[:16],
                 style={"fontFamily": MONO, "fontSize": "9px", "color": MUTED}),
    ], style={"background": ac + "11", "border": f"1px solid {ac}33",
              "borderRadius": "8px", "padding": "8px 12px"})

    dot = html.Span(
        "● LIVE" if ok else "● OFFLINE",
        style={"fontFamily": MONO, "fontSize": "10px",
               "color": GREEN if ok else RED, "textAlign": "center", "display": "block"},
    )

    return acct_div, sess_div, dot


@callback(Output("alert-bar", "children"),
          Input("interval", "n_intervals"))
def update_alerts(_):
    mt5    = load_mt5()
    charts = mt5.get("charts", {})
    alerts = []
    for sym, sym_info in charts.items():
        bars = sym_info.get("H1", [])
        if not bars:
            continue
        a = _cached_analyze(bars, sym, sym_info, "H1")
        if a["score"] >= 75 and a["direction"] != "NEUTRAL":
            dc = dir_color(a["direction"])
            alerts.append(html.Div([
                html.Span(a["direction"], style={"background": dc + "22", "color": dc,
                                                   "fontFamily": MONO, "fontSize": "9px",
                                                   "fontWeight": "700", "padding": "2px 6px",
                                                   "borderRadius": "4px", "marginRight": "6px"}),
                html.Span(sym, style={"color": GOLD, "fontFamily": MONO, "fontSize": "10px",
                                       "fontWeight": "700"}),
                html.Span(f" {a['score']}/100", style={"color": MUTED, "fontSize": "9px",
                                                         "fontFamily": MONO}),
            ], style={"whiteSpace": "nowrap", "display": "flex", "alignItems": "center"}))
    if not alerts:
        return html.Div()
    return html.Div(
        [html.Span("◈ SIGNALS: ", style={"fontFamily": MONO, "fontSize": "9px",
                                           "color": GOLD, "fontWeight": "700",
                                           "marginRight": "12px"})] + alerts,
        style={"background": GOLD + "12", "borderBottom": f"1px solid {GOLD}33",
               "padding": "7px 20px", "display": "flex", "gap": "14px",
               "overflowX": "auto", "alignItems": "center", "flexWrap": "wrap"},
    )


@callback(
    Output("active-page", "data"),
    [Input(f"nav-{p}", "n_clicks")
     for p in ["metals","markets","chart","scanner","smt","pnl","positions","history","ai","signals"]],
    prevent_initial_call=True,
)
def switch_page(*args):
    triggered = ctx.triggered_id
    triggered_val = ctx.triggered[0]["value"] if ctx.triggered else 0
    if triggered and triggered_val and triggered_val > 0:
        return triggered.replace("nav-", "")
    raise PreventUpdate


@callback(
    Output("active-tf", "data"),
    [Input(f"tf-btn-{tf}", "n_clicks") for tf in ["M1","M5","M15","H1","H4","D1","W1"]],
    prevent_initial_call=True,
)
def switch_tf(*args):
    triggered = ctx.triggered_id
    triggered_val = ctx.triggered[0]["value"] if ctx.triggered else 0
    if triggered and triggered_val and triggered_val > 0:
        return triggered.replace("tf-btn-", "")
    raise PreventUpdate


@callback(
    Output("active-sym", "data"),
    [Input(f"sym-btn-{s}", "n_clicks") for s in ["XAUUSD", "XAGUSD"]],
    prevent_initial_call=True,
)
def switch_sym(*args):
    triggered = ctx.triggered_id
    triggered_val = ctx.triggered[0]["value"] if ctx.triggered else 0
    if triggered and triggered_val and triggered_val > 0:
        return triggered.replace("sym-btn-", "")
    raise PreventUpdate


@callback(
    Output("page-content", "children"),
    Input("active-page",  "data"),
    Input("active-sym",   "data"),
    Input("active-tf",    "data"),
    Input("interval",     "n_intervals"),
)
def render_page(page, active_sym, active_tf, _):
    try:
        mt5        = load_mt5()
        ai_state   = load_ai_state()
        trader_st  = load_trader_state()

        if page == "metals":    return page_metals(mt5, ai_state, trader_st, active_sym, active_tf)
        if page == "markets":   return page_markets(mt5, active_sym, active_tf)
        if page == "chart":     return page_chart(mt5, active_sym, active_tf)
        if page == "scanner":   return page_scanner(mt5)
        if page == "smt":       return page_smt(mt5)
        if page == "pnl":       return page_pnl(mt5)
        if page == "positions": return page_positions(mt5)
        if page == "history":   return page_history(mt5)
        if page == "ai":        return page_ai(ai_state, trader_st, mt5)
        if page == "signals":   return page_signals(mt5, trader_st)
        return page_metals(mt5, ai_state, trader_st)
    except Exception as _err:
        import traceback
        tb = traceback.format_exc()
        return html.Div([
            html.H3("⚠ Page Error", style={"color": "#FF4444", "fontFamily": "monospace"}),
            html.Pre(tb, style={"color": "#ff9999", "fontFamily": "monospace",
                                "fontSize": "12px", "whiteSpace": "pre-wrap",
                                "background": "#1a1a1a", "padding": "16px",
                                "borderRadius": "8px", "border": "1px solid #FF4444"}),
        ], style={"padding": "20px"})


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════════╗
║              OMNI ICT AI Dashboard — Integrated                  ║
╠══════════════════════════════════════════════════════════════════╣
║  Data sources:                                                   ║
║  • omni_data.json  — MT5/Midas live feed (OmniExport.mq5)       ║
║  • ai_state.json   — AI regime (ai_engine.py)                   ║
║  • trader_state.json — Bot state (auto_trader.py)               ║
║  • omni_ict.db     — Trade history (SQLite)                     ║
╠══════════════════════════════════════════════════════════════════╣
║  Features:                                                       ║
║  • Real candlestick charts from MT5 bar data                    ║
║  • ICT OB, FVG, BOS/CHoCH analysis on real OHLC                 ║
║  • Live account balance, equity, positions                      ║
║  • SMT divergence across correlated pairs                       ║
║  • AI regime + adapted parameters                               ║
║  • Full trade history (MT5 deals + SQLite DB)                   ║
╠══════════════════════════════════════════════════════════════════╣
║  → http://localhost:8050                                         ║
╚══════════════════════════════════════════════════════════════════╝
""")
    app.run(debug=False, host="0.0.0.0", port=8050)
