"""
ict_engine.py — ICT / SMT / AMD signal analyzer
Reads omni_data.json and generates scored trading setups
"""

import json
import re
import os
import pandas as pd
from datetime import datetime

try:
    from config import cfg
    JSON_PATH = cfg.JSON_PATH
except ImportError:
    from pathlib import Path as _Path
    JSON_PATH = str(
        _Path.home() / "Library/Application Support"
        / "net.metaquotes.wine.metatrader5/drive_c/users/user"
        / "AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"
    )

# ── SMT Correlation pairs ─────────────────────────────────────────────────────
# If correlated pairs diverge at a key level = SMT signal
SMT_PAIRS = [
    ("EURUSD", "GBPUSD", "bullish"),   # Both USD weakness pairs
    ("AUDUSD", "NZDUSD", "bullish"),   # Both commodity currencies
    ("XAUUSD", "XAGUSD", "bullish"),   # Metals
    ("EURUSD", "USDCHF", "bearish"),   # Inverse correlation
    ("GBPUSD", "USDCAD", "bearish"),   # Inverse correlation
]

# ICT Kill Zones (GMT hours)
KILL_ZONES = {
    "ASIA_KZ":      (22, 0),
    "LONDON_OPEN":  (7, 9),
    "LONDON_CLOSE": (11, 13),
    "NY_OPEN":      (12, 14),
    "NY_CLOSE":     (19, 21),
}


def _load():
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = re.sub(r',\s*([\]}])', r'\1', f.read())
        return json.loads(raw)
    except Exception as e:
        print(f"ict_engine error: {e}")
        return {}


def get_raw_data():
    return _load()


def get_account_info():
    return _load().get("account", {})


def get_open_positions():
    data = _load().get("positions", [])
    if not data:
        return pd.DataFrame(columns=[
            "ticket", "symbol", "type", "volume", "open_price",
            "current_price", "sl", "tp", "profit", "swap", "time"
        ])
    return pd.DataFrame(data)


def get_trade_history(days=30):
    data = _load().get("history", [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["time"] = pd.to_datetime(df["time"])
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    df = df[df["time"] >= cutoff].sort_values("time")
    df["cumulative_profit"] = df["profit"].cumsum()
    return df


def get_symbol_prices(symbols):
    prices = _load().get("prices", [])
    sym_set = set(symbols)
    return [p for p in prices if p["symbol"] in sym_set]


def get_last_update():
    return _load().get("timestamp", "—")


def get_session_info():
    data = _load()
    return {
        "session":   data.get("session", "—"),
        "amd_phase": data.get("amd_phase", "—"),
        "gmt_time":  data.get("gmt_time", "—"),
    }


def is_connected():
    try:
        age = datetime.now().timestamp() - os.path.getmtime(JSON_PATH)
        return age < 30
    except Exception:
        return False


def get_json_path():
    return JSON_PATH


# ── ICT Setup Scorer ──────────────────────────────────────────────────────────

def _score_setup(p, amd_phase, session):
    """
    Score a symbol from 0-100 based on ICT confluence.
    Returns dict with score, signal direction, reasons.
    """
    score = 0
    direction = "NEUTRAL"
    reasons = []
    bull_score = 0
    bear_score = 0

    bid     = p.get("bid", 0)
    rsi     = p.get("rsi", 50)
    trend   = p.get("trend", "NEUTRAL")
    rsi_sig = p.get("rsi_signal", "NEUTRAL")
    fvg     = p.get("fvg_type", "NONE")
    ob      = p.get("ob_type", "NONE")
    struct  = p.get("structure", "RANGING")
    asia_h  = p.get("asia_high", 0)
    asia_l  = p.get("asia_low", 0)
    swing_h = p.get("swing_high", 0)
    swing_l = p.get("swing_low", 0)
    ma20    = p.get("ma20", 0)
    ma50    = p.get("ma50", 0)
    ma200   = p.get("ma200", 0)

    # ── AMD Phase alignment ───────────────────────────────────────────
    if amd_phase == "DISTRIBUTION":
        # NY session - look for directional moves
        if trend == "BULLISH":
            bull_score += 20
            reasons.append("🕐 NY Distribution → Bullish trend")
        elif trend == "BEARISH":
            bear_score += 20
            reasons.append("🕐 NY Distribution → Bearish trend")
    elif amd_phase == "MANIPULATION":
        # London open - look for stop hunts / reversals
        if rsi_sig == "OVERSOLD":
            bull_score += 15
            reasons.append("🕐 London Manipulation + RSI Oversold = Potential Bull reversal")
        elif rsi_sig == "OVERBOUGHT":
            bear_score += 15
            reasons.append("🕐 London Manipulation + RSI Overbought = Potential Bear reversal")
    elif amd_phase == "ACCUMULATION":
        # Asia - building positions, note range
        if asia_h > 0 and asia_l > 0:
            reasons.append(f"🌙 Asia Range: {asia_l:.5g} — {asia_h:.5g}")

    # ── Session kill zone bonus ───────────────────────────────────────
    if session in ["LONDON", "NEW_YORK"]:
        bull_score += 5
        bear_score += 5
        reasons.append(f"⚡ Active Kill Zone: {session}")

    # ── Fair Value Gap ────────────────────────────────────────────────
    if fvg == "BULLISH":
        bull_score += 25
        reasons.append("📊 Bullish FVG present (imbalance to fill)")
    elif fvg == "BEARISH":
        bear_score += 25
        reasons.append("📊 Bearish FVG present (imbalance to fill)")

    # ── Order Block ───────────────────────────────────────────────────
    ob_h = p.get("ob_high", 0)
    ob_l = p.get("ob_low", 0)
    if ob == "BULLISH_OB" and ob_l > 0:
        # Price near bullish OB = buy opportunity
        proximity = abs(bid - ob_l) / (ob_h - ob_l + 0.0001)
        if proximity < 0.3:
            bull_score += 30
            reasons.append(f"🟩 Price at Bullish Order Block ({ob_l:.5g}—{ob_h:.5g})")
        else:
            bull_score += 10
            reasons.append(f"🟩 Bullish Order Block nearby")
    elif ob == "BEARISH_OB" and ob_h > 0:
        proximity = abs(bid - ob_h) / (ob_h - ob_l + 0.0001)
        if proximity < 0.3:
            bear_score += 30
            reasons.append(f"🟥 Price at Bearish Order Block ({ob_l:.5g}—{ob_h:.5g})")
        else:
            bear_score += 10
            reasons.append(f"🟥 Bearish Order Block nearby")

    # ── Market Structure ──────────────────────────────────────────────
    if struct == "BOS_BULLISH":
        bull_score += 20
        reasons.append(f"📈 BOS Bullish — broke swing high {swing_h:.5g}")
    elif struct == "BOS_BEARISH":
        bear_score += 20
        reasons.append(f"📉 BOS Bearish — broke swing low {swing_l:.5g}")
    elif struct == "CHOCH_POTENTIAL_BULL":
        bull_score += 15
        reasons.append("🔄 CHoCH Potential — Bullish reversal forming")
    elif struct == "CHOCH_POTENTIAL_BEAR":
        bear_score += 15
        reasons.append("🔄 CHoCH Potential — Bearish reversal forming")

    # ── RSI confluence ────────────────────────────────────────────────
    if rsi_sig == "OVERSOLD":
        bull_score += 15
        reasons.append(f"📉 RSI Oversold ({rsi:.0f}) — potential reversal")
    elif rsi_sig == "OVERBOUGHT":
        bear_score += 15
        reasons.append(f"📈 RSI Overbought ({rsi:.0f}) — potential reversal")

    # ── MA alignment ──────────────────────────────────────────────────
    if trend == "BULLISH":
        bull_score += 10
        reasons.append("📊 MA stack Bullish (20>50>200)")
    elif trend == "BEARISH":
        bear_score += 10
        reasons.append("📊 MA stack Bearish (20<50<200)")

    # ── Asia range breakout ───────────────────────────────────────────
    if asia_h > 0 and asia_l > 0:
        if bid > asia_h:
            bull_score += 15
            reasons.append(f"🌙 Asia High Breakout ({asia_h:.5g})")
        elif bid < asia_l:
            bear_score += 15
            reasons.append(f"🌙 Asia Low Breakout ({asia_l:.5g})")

    # Determine direction and final score
    if bull_score > bear_score:
        direction = "BUY"
        score = min(bull_score, 100)
    elif bear_score > bull_score:
        direction = "SELL"
        score = min(bear_score, 100)
    else:
        score = 0

    # Calculate SL and TP based on ICT logic
    sl = tp = 0
    point = 0.0001 if "JPY" not in p.get("symbol","") else 0.01
    atr_approx = abs(p.get("swing_high", bid) - p.get("swing_low", bid)) * 0.1

    if direction == "BUY":
        sl = round(bid - max(atr_approx, point * 20), 5)
        tp = round(bid + max(atr_approx * 2, point * 40), 5)
    elif direction == "SELL":
        sl = round(bid + max(atr_approx, point * 20), 5)
        tp = round(bid - max(atr_approx * 2, point * 40), 5)

    return {
        "symbol":    p.get("symbol"),
        "score":     score,
        "direction": direction,
        "bid":       bid,
        "sl":        sl,
        "tp":        tp,
        "rsi":       rsi,
        "trend":     trend,
        "fvg":       fvg,
        "ob":        ob,
        "structure": struct,
        "amd_phase": amd_phase,
        "session":   session,
        "reasons":   reasons,
    }


def _check_smt(prices_dict, amd_phase):
    """Detect SMT divergence between correlated pairs."""
    smt_signals = []
    for sym_a, sym_b, correlation in SMT_PAIRS:
        if sym_a not in prices_dict or sym_b not in prices_dict:
            continue
        a = prices_dict[sym_a]
        b = prices_dict[sym_b]
        rsi_a = a.get("rsi", 50)
        rsi_b = b.get("rsi", 50)
        trend_a = a.get("trend", "NEUTRAL")
        trend_b = b.get("trend", "NEUTRAL")

        # SMT: both should move together, if they diverge = signal
        if correlation == "bullish":
            # Both should be bullish or bearish together
            if trend_a == "BULLISH" and trend_b == "BEARISH":
                smt_signals.append({
                    "pair_a": sym_a, "pair_b": sym_b,
                    "type": "SMT_DIVERGENCE",
                    "detail": f"{sym_a} Bullish but {sym_b} Bearish — SMT divergence. Favor {sym_a} direction.",
                    "rsi_a": rsi_a, "rsi_b": rsi_b,
                    "amd_phase": amd_phase,
                })
            elif trend_a == "BEARISH" and trend_b == "BULLISH":
                smt_signals.append({
                    "pair_a": sym_a, "pair_b": sym_b,
                    "type": "SMT_DIVERGENCE",
                    "detail": f"{sym_b} Bullish but {sym_a} Bearish — SMT divergence. Favor {sym_b} direction.",
                    "rsi_a": rsi_a, "rsi_b": rsi_b,
                    "amd_phase": amd_phase,
                })
            # RSI divergence: one oversold, one not
            if rsi_a < 35 and rsi_b > 45:
                smt_signals.append({
                    "pair_a": sym_a, "pair_b": sym_b,
                    "type": "RSI_SMT",
                    "detail": f"{sym_a} RSI oversold ({rsi_a:.0f}) but {sym_b} neutral ({rsi_b:.0f}) — {sym_a} may bounce",
                    "rsi_a": rsi_a, "rsi_b": rsi_b,
                    "amd_phase": amd_phase,
                })
        elif correlation == "bearish":
            # Should move inversely
            if trend_a == "BULLISH" and trend_b == "BULLISH":
                smt_signals.append({
                    "pair_a": sym_a, "pair_b": sym_b,
                    "type": "INVERSE_SMT",
                    "detail": f"{sym_a} and {sym_b} both Bullish — inverse pairs aligned. Watch for reversal.",
                    "rsi_a": rsi_a, "rsi_b": rsi_b,
                    "amd_phase": amd_phase,
                })

    return smt_signals


def get_ict_scanner():
    """Main function: returns scored setups + SMT signals."""
    data = _load()
    if not data:
        return [], [], {}, []

    prices  = data.get("prices", [])
    session = data.get("session", "—")
    amd     = data.get("amd_phase", "—")

    prices_dict = {p["symbol"]: p for p in prices}

    # Score every symbol
    setups = []
    for p in prices:
        result = _score_setup(p, amd, session)
        if result["score"] >= 30:  # Only show meaningful setups
            setups.append(result)

    # Sort by score descending
    setups.sort(key=lambda x: x["score"], reverse=True)

    # SMT divergence signals
    smt_signals = _check_smt(prices_dict, amd)

    session_info = {
        "session":   session,
        "amd_phase": amd,
        "gmt_time":  data.get("gmt_time", "—"),
    }

    return setups, smt_signals, session_info, prices
