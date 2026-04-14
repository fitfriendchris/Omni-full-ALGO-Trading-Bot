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
from datetime import datetime, timedelta
import dash
from dash import dcc, html, dash_table, Input, Output, callback, ctx
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd

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

REFRESH_MS      = 3000   # poll every 3 seconds

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

def load_mt5() -> dict:
    """Load and parse omni_data.json from MT5/Midas."""
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = re.sub(r',\s*([\]}])', r'\1', f.read())
        return json.loads(raw)
    except Exception as e:
        print(f"[load_mt5] {e}")
        return {}


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

    # ── Order Block detection ─────────────────────────────────────────
    ob_type = "NONE"
    ob_h = ob_l = 0.0
    for i in range(max(0, n - 15), n - 1):
        b, bn = bars_asc[i], bars_asc[i + 1]
        rng = bn["h"] - bn["l"] or 0.0001
        impulse = abs(bn["c"] - bn["o"]) / rng
        if impulse < 0.55:
            continue
        if b["c"] < b["o"] and bn["c"] > bn["o"]:
            ob_type = "BULLISH_OB"; ob_h = b["h"]; ob_l = b["l"]; break
        if b["c"] > b["o"] and bn["c"] < bn["o"]:
            ob_type = "BEARISH_OB"; ob_h = b["h"]; ob_l = b["l"]; break

    # ── FVG detection ─────────────────────────────────────────────────
    fvg_type = "NONE"
    fvg_h = fvg_l = 0.0
    for i in range(max(0, n - 10), n - 2):
        b0, b2 = bars_asc[i], bars_asc[i + 2]
        if b0["l"] > b2["h"]:
            fvg_type = "BULLISH"; fvg_h = b0["l"]; fvg_l = b2["h"]; break
        if b0["h"] < b2["l"]:
            fvg_type = "BEARISH"; fvg_h = b2["l"]; fvg_l = b0["h"]; break

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

    return {
        "sym": sym, "direction": direction, "score": score,
        "sl": sl, "tp": tp, "rr": rr,
        "rsi": rsi, "rsi_sig": rsi_sig, "trend": trend,
        "structure": structure, "atr": atr, "cur": cur, "digits": digits,
        "ob_type": ob_type, "ob_h": ob_h, "ob_l": ob_l,
        "fvg_type": fvg_type, "fvg_h": fvg_h, "fvg_l": fvg_l,
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
        "asia_h": 0, "asia_l": 0, "swing_h": 0, "swing_l": 0,
        "ma5": 0, "ma20": 0, "reasons": ["No data — MT5 EA not running"],
    }


# ── CHART BUILDERS ─────────────────────────────────────────────────────────────

def make_candle_chart(bars_raw: list, analysis: dict, sym: str,
                      sym_info: dict, height=380) -> go.Figure:
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

    d = analysis.get("digits", 5)

    fig = go.Figure()

    # ── Volume bars (subdued) ─────────────────────────────────────────
    vol_colors = [GREEN + "55" if c >= o else RED + "55"
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
        increasing_line_color=GREEN, increasing_fillcolor=GREEN + "cc",
        decreasing_line_color=RED,   decreasing_fillcolor=RED   + "cc",
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

    # ── Order Block shading ───────────────────────────────────────────
    if analysis.get("ob_type") != "NONE" and analysis.get("ob_h") and analysis.get("ob_l"):
        ob_color = GREEN if "BULL" in analysis["ob_type"] else RED
        fig.add_hrect(
            y0=analysis["ob_l"], y1=analysis["ob_h"],
            fillcolor=ob_color + "20",
            line=dict(color=ob_color + "66", width=1, dash="dot"),
            annotation=dict(
                text=f"{'Bull' if 'BULL' in analysis['ob_type'] else 'Bear'} OB",
                font=dict(color=ob_color, size=9, family=MONO),
                xanchor="left",
            ),
        )

    # ── FVG shading ───────────────────────────────────────────────────
    if analysis.get("fvg_type") != "NONE" and analysis.get("fvg_h") and analysis.get("fvg_l"):
        fvg_color = BLUE if analysis["fvg_type"] == "BULLISH" else PURPLE
        fig.add_hrect(
            y0=analysis["fvg_l"], y1=analysis["fvg_h"],
            fillcolor=fvg_color + "18",
            line=dict(color=fvg_color + "55", width=1, dash="longdash"),
            annotation=dict(
                text=f"FVG {analysis['fvg_type'][:4]}",
                font=dict(color=fvg_color, size=9, family=MONO),
                xanchor="left",
            ),
        )

    # ── Asia range shading ────────────────────────────────────────────
    ah, al = analysis.get("asia_h", 0), analysis.get("asia_l", 0)
    if ah and al and ah != al:
        fig.add_hrect(
            y0=al, y1=ah, fillcolor=BLUE + "0c",
            line=dict(color=BLUE + "33", width=0.5),
            annotation=dict(text="Asia", font=dict(color=BLUE, size=8, family=MONO)),
        )

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
        fillcolor=GOLD + "18", name="Equity",
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
        fillcolor=RED + "18", name="Drawdown",
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
        marker_color=GOLD + "aa", marker_line=dict(color=GOLD, width=0.5),
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

def page_metals(mt5, ai_state, trader_st):
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
        bars_raw = sym_info.get("H1", [])
        bid  = p.get("bid", sym_info.get("H1", [{}])[0].get("c", 0) if sym_info.get("H1") else 0)
        ask  = p.get("ask", 0)
        sprd = p.get("spread", 0)
        a    = analyze_bars(bars_raw, sym, sym_info) if bars_raw else _empty_analysis(sym)
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

        # Full H1 chart for Gold
        card([
            sec_header("XAUUSD H1 — Live Chart"),
            dcc.Graph(
                figure=make_candle_chart(
                    charts_data.get("XAUUSD", {}).get("H1", []),
                    analyze_bars(charts_data.get("XAUUSD", {}).get("H1", []),
                                 "XAUUSD", charts_data.get("XAUUSD", {})),
                    "XAUUSD",
                    charts_data.get("XAUUSD", {}),
                    height=320,
                ),
                config={"displayModeBar": False},
            ),
        ]),
    ])


def page_markets(mt5, active_sym, active_tf):
    charts = mt5.get("charts", {})
    prices = {p["symbol"]: p for p in mt5.get("prices", [])}
    syms   = list(charts.keys())

    rows = []
    for sym in syms:
        sym_info = charts[sym]
        bars_raw = sym_info.get(active_tf, sym_info.get("H1", []))
        a = analyze_bars(bars_raw, sym, sym_info) if bars_raw else _empty_analysis(sym)
        p = prices.get(sym, {})
        bid  = p.get("bid", a["cur"])
        sprd = p.get("spread", 0)
        dc   = dir_color(a["direction"])
        rows.append({
            "Symbol": sym, "Bid": f"{bid:.{a['digits']}f}",
            "Spread": sprd, "Signal": a["direction"],
            "Score": a["score"], "RSI": a["rsi"],
            "Trend": a["trend"], "FVG": a["fvg_type"][:4] if a["fvg_type"] != "NONE" else "—",
            "OB": a["ob_type"].replace("_OB", "")[:4] if a["ob_type"] != "NONE" else "—",
            "Structure": a["structure"].replace("_", " ")[:10],
            "SL": f"{a['sl']:.{a['digits']}f}", "TP": f"{a['tp']:.{a['digits']}f}",
            "RR": a["rr"],
        })

    rows.sort(key=lambda x: x["Score"], reverse=True)

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

    return html.Div([
        html.Div([
            html.H2("◈ All Markets", style={"fontFamily": MONO, "fontWeight": "700",
                                             "fontSize": "18px", "color": TEXT, "margin": "0"}),
            html.P(f"{len(syms)} instruments | ICT signals | TF: {active_tf}",
                   style={"fontSize": "12px", "color": MUTED, "margin": "3px 0 0"}),
        ], style={"marginBottom": "12px"}),
        tf_buttons,
        card([
            sec_header("ICT Signals", f"{len([r for r in rows if r['Signal'] != 'NEUTRAL'])} active"),
            dash_table.DataTable(
                data=rows,
                columns=[{"name": c, "id": c} for c in rows[0].keys()] if rows else [],
                style_table={"overflowX": "auto"},
                style_header=TH,
                style_cell=TC,
                style_data_conditional=[
                    {"if": {"filter_query": '{Signal} = "BUY"',  "column_id": "Signal"}, "color": GREEN, "fontWeight": "700"},
                    {"if": {"filter_query": '{Signal} = "SELL"', "column_id": "Signal"}, "color": RED,   "fontWeight": "700"},
                    {"if": {"filter_query": '{Trend} = "BULLISH"', "column_id": "Trend"}, "color": GREEN},
                    {"if": {"filter_query": '{Trend} = "BEARISH"', "column_id": "Trend"}, "color": RED},
                    {"if": {"filter_query": "{Score} >= 75", "column_id": "Score"}, "color": GOLD, "fontWeight": "700"},
                    {"if": {"filter_query": "{Score} >= 55", "column_id": "Score"}, "color": BLUE},
                    {"if": {"filter_query": '{FVG} != "—"', "column_id": "FVG"}, "color": BLUE, "fontWeight": "600"},
                ],
                sort_action="native", filter_action="native", page_size=20,
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
    analysis = analyze_bars(bars_raw, active_sym, sym_info) if bars_raw else _empty_analysis(active_sym)
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
                figure=make_candle_chart(bars_raw, analysis, active_sym, sym_info, height=380),
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
        a = analyze_bars(bars_raw, sym, sym_info) if bars_raw else _empty_analysis(sym)
        p = prices_l.get(sym, {})
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
        a_a = analyze_bars(si_a.get("H1", []), sym_a, si_a)
        a_b = analyze_bars(si_b.get("H1", []), sym_b, si_b)
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

        # Trader state
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
    return datetime.utcnow().strftime("%H:%M:%S UTC")


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
        a = analyze_bars(bars, sym, sym_info)
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
     for p in ["metals","markets","chart","scanner","smt","pnl","positions","history","ai"]],
    prevent_initial_call=True,
)
def switch_page(*args):
    triggered = ctx.triggered_id
    if triggered:
        return triggered.replace("nav-", "")
    return "metals"


@callback(
    Output("active-tf", "data"),
    [Input(f"tf-btn-{tf}", "n_clicks") for tf in ["M5","M15","H1","H4","D1"]],
    prevent_initial_call=True,
)
def switch_tf(*args):
    triggered = ctx.triggered_id
    if triggered:
        return triggered.replace("tf-btn-", "")
    return "H1"


@callback(
    Output("page-content", "children"),
    Input("active-page",  "data"),
    Input("active-sym",   "data"),
    Input("active-tf",    "data"),
    Input("interval",     "n_intervals"),
)
def render_page(page, active_sym, active_tf, _):
    mt5        = load_mt5()
    ai_state   = load_ai_state()
    trader_st  = load_trader_state()

    if page == "metals":    return page_metals(mt5, ai_state, trader_st)
    if page == "markets":   return page_markets(mt5, active_sym, active_tf)
    if page == "chart":     return page_chart(mt5, active_sym, active_tf)
    if page == "scanner":   return page_scanner(mt5)
    if page == "smt":       return page_smt(mt5)
    if page == "pnl":       return page_pnl(mt5)
    if page == "positions": return page_positions(mt5)
    if page == "history":   return page_history(mt5)
    if page == "ai":        return page_ai(ai_state, trader_st, mt5)
    return page_metals(mt5, ai_state, trader_st)


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
