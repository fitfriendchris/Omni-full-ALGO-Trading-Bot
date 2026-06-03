"""
market_structure_context.py — Single source of truth for all ICT structural data.

Reads MT5 omni_data.json format and computes:
  • weekly_bias    (W1 BOS/CHoCH, weekly OB, weekly direction)
  • daily_bias     (D1 structure, PDH/PDL, daily OB, daily FVG)
  • htf_bias       (H4 BOS/CHoCH, H4 swing high/low)
  • h1_bias        (H1 micro-structure)
  • m15_trigger    (M15 precision entry trigger)
  • cycle_phase    (ACCUMULATION / MANIPULATION / DISTRIBUTION)
  • mtf_alignment  (A+ through CONFLICT scoring)
  • session_state  (London→NY flow tracking)

All output is JSON-serializable. Designed to be called once per cycle by
orchestrator.py and written to shared/market_structure.json.

Backwards compatibility:
  All fields are optional. Callers MUST check for field absence and degrade 
  gracefully. This module itself degrades when any timeframe data is missing.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── MT5 time format parser ───────────────────────────────────────────────────

def _parse_mt5_time(t) -> Optional[datetime]:
    """Parse MT5 time string '2026.05.27 00:00:00' into timezone-aware UTC."""
    if not t:
        return None
    if isinstance(t, str):
        try:
            return datetime.strptime(t, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                return datetime.strptime(t, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                return None
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(float(t), tz=timezone.utc)
    return None


# ── Lightweight bar ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MiniBar:
    time: datetime
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0

    @property
    def bullish(self) -> bool:
        return self.c > self.o

    @property
    def bearish(self) -> bool:
        return self.c < self.o

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def range(self) -> float:
        return max(1e-12, self.h - self.l)


def _bars_from_chart(chart_dict: dict) -> List[MiniBar]:
    """Parse MT5 bar list into MiniBar objects."""
    bars = []
    for raw in chart_dict:
        if not isinstance(raw, dict):
            continue
        t = _parse_mt5_time(raw.get("t"))
        if t is None:
            continue
        try:
            bars.append(MiniBar(
                time=t,
                o=float(raw.get("o", 0)),
                h=float(raw.get("h", 0)),
                l=float(raw.get("l", 0)),
                c=float(raw.get("c", 0)),
                v=float(raw.get("v", 0)),
            ))
        except (ValueError, TypeError):
            continue
    return bars


# ── Technical helpers ─────────────────────────────────────────────────────────

def _ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return values
    k = 2.0 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    padding = [ema[0]] * (period - 1)
    return padding + ema


def _swing_pivot_highs(bars: List[MiniBar], left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    out = []
    for i in range(left, len(bars) - right):
        peak = bars[i].h
        if all(bars[i - j].h < peak for j in range(1, left + 1)) and \
           all(bars[i + j].h < peak for j in range(1, right + 1)):
            out.append((i, peak))
    return out


def _swing_pivot_lows(bars: List[MiniBar], left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    out = []
    for i in range(left, len(bars) - right):
        trough = bars[i].l
        if all(bars[i - j].l > trough for j in range(1, left + 1)) and \
           all(bars[i + j].l > trough for j in range(1, right + 1)):
            out.append((i, trough))
    return out


def _atr(bars: List[MiniBar], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        tr = max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period


# ── Fair Value Gap detection (3-bar) ──────────────────────────────────────────

def _detect_fvg(bars: List[MiniBar]) -> List[dict]:
    """Detect unmitigated 3-bar FVGs."""
    fvg = []
    for i in range(2, len(bars)):
        c1, c2, c3 = bars[i - 2], bars[i - 1], bars[i]
        # Bullish FVG: c1.high < c3.low
        if c1.h < c3.l:
            fvg.append({
                "type": "BULL",
                "idx": i - 1,
                "top": round(c3.l, 5),
                "bot": round(c1.h, 5),
                "size": round(c3.l - c1.h, 5),
            })
        # Bearish FVG: c1.low > c3.high
        if c1.l > c3.h:
            fvg.append({
                "type": "BEAR",
                "idx": i - 1,
                "top": round(c1.l, 5),
                "bot": round(c3.h, 5),
                "size": round(c1.l - c3.h, 5),
            })
    return fvg


# ── Order Block detection ─────────────────────────────────────────────────────

def _detect_ob(bars: List[MiniBar]) -> Tuple[Optional[dict], Optional[dict]]:
    """Detect most recent unmitigated bullish and bearish OB."""
    bull_ob = None
    bear_ob = None
    last_close = bars[-1].c
    for i in range(len(bars) - 1, 2, -1):
        b0, b1, b2 = bars[i - 2], bars[i - 1], bars[i]
        # Bullish OB: preceding bearish candle, then strong bullish displacement
        if b0.bearish and b1.bullish and b1.body > b1.range * 0.4:
            # Unmitigated = last close is above OB bottom
            if last_close > b1.l and not bull_ob:
                bull_ob = {
                    "type": "BULL",
                    "idx": i - 1,
                    "top": round(b1.h, 5),
                    "bot": round(b1.l, 5),
                    "body_top": round(max(b1.o, b1.c), 5),
                    "body_bot": round(min(b1.o, b1.c), 5),
                    "mid": round((b1.h + b1.l) / 2, 5),
                }
        # Bearish OB: preceding bullish candle, then strong bearish displacement
        if b0.bullish and b1.bearish and b1.body > b1.range * 0.4:
            if last_close < b1.h and not bear_ob:
                bear_ob = {
                    "type": "BEAR",
                    "idx": i - 1,
                    "top": round(b1.h, 5),
                    "bot": round(b1.l, 5),
                    "body_top": round(max(b1.o, b1.c), 5),
                    "body_bot": round(min(b1.o, b1.c), 5),
                    "mid": round((b1.h + b1.l) / 2, 5),
                }
        if bull_ob and bear_ob:
            break
    return bull_ob, bear_ob


# ── BOS / CHoCH detection ──────────────────────────────────────────────────

def _detect_structure(bars: List[MiniBar]) -> List[dict]:
    """Detect BOS (Break of Structure) and CHoCH (Change of Character)."""
    highs = _swing_pivot_highs(bars, left=3, right=2)
    lows = _swing_pivot_lows(bars, left=3, right=2)
    
    events = []
    prev_high = None
    prev_low = None
    trend = "NEUTRAL"
    
    for idx, price in highs:
        if prev_high is None:
            prev_high = (idx, price)
            continue
        if price > prev_high[1]:
            events.append({
                "type": "BOS",
                "direction": "BULL",
                "idx": idx,
                "price": round(price, 5),
                "breaks_swing": prev_high[1],
            })
            trend = "BULL"
        elif price < prev_high[1] and trend == "BULL":
            events.append({
                "type": "CHOCH",
                "direction": "BEAR",
                "idx": idx,
                "price": round(price, 5),
                "breaks_swing": prev_high[1],
            })
            trend = "BEAR"
        prev_high = (idx, price)
    
    for idx, price in lows:
        if prev_low is None:
            prev_low = (idx, price)
            continue
        if price < prev_low[1]:
            events.append({
                "type": "BOS",
                "direction": "BEAR",
                "idx": idx,
                "price": round(price, 5),
                "breaks_swing": prev_low[1],
            })
            trend = "BEAR"
        elif price > prev_low[1] and trend == "BEAR":
            events.append({
                "type": "CHOCH",
                "direction": "BULL",
                "idx": idx,
                "price": round(price, 5),
                "breaks_swing": prev_low[1],
            })
            trend = "BULL"
        prev_low = (idx, price)
    
    # Sort by index
    events.sort(key=lambda x: x["idx"])
    return events


# ── Weekly bias engine ───────────────────────────────────────────────────────

def _weekly_bias(d1_bars: List[MiniBar]) -> dict:
    """
    Compute weekly bias from D1 bars (minimum 20 D1 bars).
    Groups bars by calendar week, detects BOS/CHoCH on weekly pivots.
    """
    if len(d1_bars) < 10:
        return {"direction": "NEUTRAL", "confidence": 0.0, "reason": "not enough D1 bars"}
    
    # Group into weeks (ISO week)
    weeks: Dict[int, List[MiniBar]] = {}
    for b in d1_bars:
        week_num = b.time.isocalendar()[1]
        weeks.setdefault(week_num, []).append(b)
    
    weekly_bars = []
    for wk, wbars in sorted(weeks.items()):
        if len(wbars) < 1:
            continue
        w_o = wbars[0].o
        w_c = wbars[-1].c
        w_h = max(b.h for b in wbars)
        w_l = min(b.l for b in wbars)
        weekly_bars.append(MiniBar(time=wbars[-1].time, o=w_o, h=w_h, l=w_l, c=w_c, v=0))
    
    if len(weekly_bars) < 3:
        return {"direction": "NEUTRAL", "confidence": 0.0, "reason": "not enough weekly bars"}
    
    current = weekly_bars[-1]
    prior = weekly_bars[-2]
    
    # Detect last weekly structure
    struc = _detect_structure(weekly_bars)
    last_struc = struc[-1] if struc else None
    
    # Find weekly OB
    w_bull_ob, w_bear_ob = _detect_ob(weekly_bars)
    
    # Bias determination
    direction = "NEUTRAL"
    confidence = 0.0
    reasons = []
    
    if last_struc:
        if last_struc["direction"] == "BULL":
            if current.c > current.o * 0.995:  # bullish or near-flat close
                direction = "BULL"
                confidence = 0.70
                reasons.append(f"last weekly {last_struc['type']} BULL, bullish close")
        else:
            if current.c < current.o * 1.005:
                direction = "BEAR"
                confidence = 0.70
                reasons.append(f"last weekly {last_struc['type']} BEAR, bearish close")
    
    # Price relative to weekly OB
    if w_bull_ob and current.c > w_bull_ob["mid"]:
        if direction != "BEAR":
            direction = "BULL"
            confidence = max(confidence, 0.60)
        reasons.append("price above weekly bullish OB")
    if w_bear_ob and current.c < w_bear_ob["mid"]:
        if direction != "BULL":
            direction = "BEAR"
            confidence = max(confidence, 0.60)
        reasons.append("price below weekly bearish OB")
    
    # Fallback via EMA
    closes = [b.c for b in d1_bars]
    if len(closes) >= 20:
        ema20 = _ema(closes, 20)[-1]
        if current.c > ema20 and direction == "NEUTRAL":
            direction = "BULL"
            confidence = 0.35
            reasons.append("price above D1 EMA20")
        elif current.c < ema20 and direction == "NEUTRAL":
            direction = "BEAR"
            confidence = 0.35
            reasons.append("price below D1 EMA20")
    
    return {
        "direction": direction,
        "confidence": round(confidence, 3),
        "reasons": reasons,
        "prior_week_high": round(prior.h, 5),
        "prior_week_low": round(prior.l, 5),
        "weekly_ob_bull": w_bull_ob,
        "weekly_ob_bear": w_bear_ob,
        "last_structure": last_struc,
    }


# ── Daily structure engine ───────────────────────────────────────────────────

def _daily_structure(d1_bars: List[MiniBar], chart_meta: dict) -> dict:
    """
    Compute daily structure from D1 bars + MT5 chart metadata (PDH/PDL etc).
    """
    if not d1_bars:
        return {"direction": "NEUTRAL", "reason": "no D1 bars"}
    
    current = d1_bars[-1]
    prior = d1_bars[-2] if len(d1_bars) > 1 else current
    
    # PDH/PDL from MT5 metadata if present
    pdh = chart_meta.get("pdh", prior.h)
    pdl = chart_meta.get("pdl", prior.l)
    nwog_open = chart_meta.get("nwog_open", 0)
    ndog_open = chart_meta.get("ndog_open", 0)
    
    # Detect daily structure
    struc = _detect_structure(d1_bars)
    last_struc = struc[-1] if struc else None
    
    # Detect daily OB + FVG
    d_bull_ob, d_bear_ob = _detect_ob(d1_bars)
    daily_fvg = _detect_fvg(d1_bars)
    
    # Bias determination
    direction = "NEUTRAL"
    confidence = 0.0
    reasons = []
    
    if last_struc:
        direction = last_struc["direction"]
        confidence = 0.65
        reasons.append(f"last daily {last_struc['type']} {direction}")
    
    # Price relative to daily OB
    if d_bull_ob and current.c > d_bull_ob["mid"]:
        if direction != "BEAR":
            direction = "BULL"
            confidence = max(confidence, 0.55)
        reasons.append("price above daily bullish OB")
    if d_bear_ob and current.c < d_bear_ob["mid"]:
        if direction != "BULL":
            direction = "BEAR"
            confidence = max(confidence, 0.55)
        reasons.append("price below daily bearish OB")
    
    # Is price in discount or premium of daily range?
    drange = current.h - current.l if current.h != current.l else 1e-9
    pct_from_low = (current.c - current.l) / drange
    quarter = _quarter_from_pct(pct_from_low)
    
    return {
        "direction": direction,
        "confidence": round(confidence, 3),
        "reasons": reasons,
        "pdh": round(float(pdh), 5),
        "pdl": round(float(pdl), 5),
        "open": round(current.o, 5),
        "close": round(current.c, 5),
        "high": round(current.h, 5),
        "low": round(current.l, 5),
        "range": round(drange, 5),
        "pct_from_low": round(pct_from_low, 3),
        "quarter": quarter,
        "daily_ob_bull": d_bull_ob,
        "daily_ob_bear": d_bear_ob,
        "daily_fvg": daily_fvg[-3:] if daily_fvg else [],  # last 3 only
        "last_structure": last_struc,
        "nwog_open": round(float(nwog_open), 5) if nwog_open else 0,
        "ndog_open": round(float(ndog_open), 5) if ndog_open else 0,
    }


def _quarter_from_pct(pct: float) -> str:
    if pct <= 0.25:
        return "Q1_DEEP_DISCOUNT"
    elif pct <= 0.5:
        return "Q2_DISCOUNT"
    elif pct <= 0.75:
        return "Q3_PREMIUM"
    return "Q4_DEEP_PREMIUM"


# ── H4 / H1 / M15 structure ──────────────────────────────────────────────────

def _tf_structure(bars: List[MiniBar], tf_name: str) -> dict:
    """Generic structure detector for any timeframe."""
    if not bars:
        return {"direction": "NEUTRAL", "reason": f"no {tf_name} bars"}
    
    struc = _detect_structure(bars)
    last_struc = struc[-1] if struc else None
    bull_ob, bear_ob = _detect_ob(bars)
    fvg = _detect_fvg(bars)
    
    current = bars[-1]
    highs = _swing_pivot_highs(bars)
    lows = _swing_pivot_lows(bars)
    
    swing_highs = [round(p, 5) for _, p in highs[-5:]]
    swing_lows = [round(p, 5) for _, p in lows[-5:]]
    
    direction = "NEUTRAL"
    confidence = 0.0
    reasons = []
    
    if last_struc:
        direction = last_struc["direction"]
        confidence = 0.65
        reasons.append(f"last {tf_name} {last_struc['type']} {direction}")
    
    return {
        "direction": direction,
        "confidence": round(confidence, 3),
        "reasons": reasons,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "ob_bull": bull_ob,
        "ob_bear": bear_ob,
        "fvg": fvg[-3:] if fvg else [],
        "last_structure": last_struc,
        "atr14": round(_atr(bars, 14), 5),
        "last_bar_bullish": current.bullish,
    }


# ── Cycle phase detection ─────────────────────────────────────────────────────

def _detect_cycle_phase(h4_bars: List[MiniBar]) -> dict:
    """
    Classify current ICT cycle phase from H4 bars.
    Uses: range relative to avg, sweep detection, CHoCH success proxy.
    """
    if len(h4_bars) < 12:
        return {"phase": "UNKNOWN", "confidence": 0.0, "reason": "not enough H4 bars"}
    
    # Last 5 days of H4 = 30 bars (6 per day); last 3 days = 18 bars
    recent = h4_bars[-18:]
    ranges = [b.h - b.l for b in recent]
    avg_range = sum(ranges) / len(ranges) if ranges else 1e-9
    
    current = h4_bars[-1]
    current_range = current.h - current.l
    
    # Count sweeps: bars that take out prior bar extreme with rejection
    sweeps = []
    for i in range(1, len(recent)):
        prev, curr = recent[i - 1], recent[i]
        # High sweep
        if curr.h > prev.h + avg_range * 0.1 and curr.c < prev.h:
            sweeps.append({"type": "HIGH", "idx": i, "magnitude": curr.h - prev.h})
        # Low sweep
        if curr.l < prev.l - avg_range * 0.1 and curr.c > prev.l:
            sweeps.append({"type": "LOW", "idx": i, "magnitude": prev.l - curr.l})
    
    # Count structure events in recent bars
    recent_struc = _detect_structure(recent)
    bos_count = sum(1 for e in recent_struc if e["type"] == "BOS")
    choch_count = sum(1 for e in recent_struc if e["type"] == "CHOCH")
    
    # Classification logic
    phase = "UNKNOWN"
    confidence = 0.3
    reasons = []
    
    big_sweep_ratio = max(s["magnitude"] for s in sweeps) / avg_range if sweeps else 0
    expanding = current_range > avg_range * 1.3
    contracting = current_range < avg_range * 0.7
    
    if big_sweep_ratio >= 2.0 and bos_count >= 1 and expanding:
        phase = "DISTRIBUTION"
        confidence = 0.85
        reasons.append(f"sweep {big_sweep_ratio:.1f}x avg range + BOS expansion = distribution")
    elif big_sweep_ratio >= 1.2 and choch_count >= 1 and not expanding:
        phase = "MANIPULATION"
        confidence = 0.70
        reasons.append(f"sweep {big_sweep_ratio:.1f}x avg range + CHoCH = manipulation")
    elif big_sweep_ratio < 1.0 and contracting:
        phase = "ACCUMULATION"
        confidence = 0.75
        reasons.append("contracting range, no sweeps = accumulation")
    else:
        reasons.append("mixed signals — no clear phase")
    
    # Day counter
    hours_since_sweep = 0
    if sweeps:
        hours_since_sweep = (current.time - recent[sweeps[-1]["idx"]].time).total_seconds() / 3600
    day_number = min(5, int(hours_since_sweep / 24) + 1)
    
    return {
        "phase": phase,
        "confidence": round(confidence, 3),
        "reasons": reasons,
        "day_number": day_number,
        "big_sweep_ratio": round(big_sweep_ratio, 2),
        "expanding": expanding,
        "contracting": contracting,
        "sweep_count_3d": len(sweeps),
        "bos_count": bos_count,
        "choch_count": choch_count,
        "avg_h4_range": round(avg_range, 5),
        "current_h4_range": round(current_range, 5),
    }


# ── Multi-timeframe alignment ────────────────────────────────────────────────

def _mtf_alignment(
    weekly: dict,
    daily: dict,
    h4: dict,
    h1: dict,
    m15: dict,
) -> dict:
    """
    Compute MTF alignment score: A+ through CONFLICT.
    """
    dirs = {
        "weekly": weekly.get("direction", "NEUTRAL"),
        "daily": daily.get("direction", "NEUTRAL"),
        "h4": h4.get("direction", "NEUTRAL"),
        "h1": h1.get("direction", "NEUTRAL"),
        "m15": m15.get("direction", "NEUTRAL"),
    }
    
    score = 0
    reasons = []
    
    # Mandatory: weekly must not oppose trade direction
    w = dirs["weekly"]
    d = dirs["daily"]
    h = dirs["h4"]
    h1_dir = dirs["h1"]
    m15_dir = dirs["m15"]
    
    # Alignment scoring
    if w == d == h:
        score += 25
        reasons.append("W1=D1=H4 aligned")
    elif d == h:
        score += 15
        reasons.append("D1=H4 aligned")
    elif w == d:
        score += 10
        reasons.append("W1=D1 aligned")
    
    if h == h1_dir:
        score += 10
        reasons.append("H4=H1 aligned")
    else:
        score -= 15
        reasons.append("H4 conflicts H1")
    
    if h1_dir == m15_dir:
        score += 8
        reasons.append("H1=M15 aligned")
    else:
        score -= 10
        reasons.append("H1 conflicts M15")
    
    # Weekly conflict = major penalty
    if w != "NEUTRAL" and d != "NEUTRAL" and w != d:
        score -= 40
        reasons.append("WEEKLY conflicts DAILY (-40)")
    
    # Neutral penalties
    if d == "NEUTRAL":
        score -= 5
        reasons.append("D1 neutral (-5)")
    if h == "NEUTRAL":
        score -= 5
        reasons.append("H4 neutral (-5)")
    
    # Grade
    if score >= 35:
        grade = "A+"
        tradeable = True
    elif score >= 25:
        grade = "A"
        tradeable = True
    elif score >= 15:
        grade = "B+"
        tradeable = True
    elif score >= 5:
        grade = "B"
        tradeable = True
    elif score <= -20:
        grade = "CONFLICT"
        tradeable = False
    else:
        grade = "C"
        tradeable = False
    
    return {
        "score": score,
        "grade": grade,
        "tradeable": tradeable,
        "directions": dirs,
        "reasons": reasons,
    }


# ── Session state ─────────────────────────────────────────────────────────────

def _session_state(now_utc: datetime) -> dict:
    """Determine current session and session flow."""
    hour = now_utc.hour
    
    if 0 <= hour < 7:
        current = "ASIA"
        confidence_bonus = 0
    elif 7 <= hour < 12:
        current = "LONDON"
        confidence_bonus = 10
    elif 12 <= hour < 15:
        current = "NY_OPEN"
        confidence_bonus = 10
    elif 15 <= hour < 17:
        current = "SILVER_BULLET"
        confidence_bonus = 8
    elif 17 <= hour < 21:
        current = "NY_CLOSE"
        confidence_bonus = 0
    else:
        current = "ASIA_CLOSE"
        confidence_bonus = 0
    
    return {
        "current_session": current,
        "utc_hour": hour,
        "confidence_bonus": confidence_bonus,
        "in_killzone": current in ("LONDON", "NY_OPEN", "SILVER_BULLET"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def build_market_structure(symbol: str, mt5_data: dict) -> dict:
    """
    Compute complete market structure for a symbol from raw MT5 data.
    
    Parameters
    ----------
    symbol : str    the trading symbol (e.g., "XAUUSD")
    mt5_data : dict raw MT5 omni_data.json dict
    
    Returns
    -------
    dict  JSON-serializable market structure with all fields.
    """
    charts = mt5_data.get("charts", {})
    sym_chart = charts.get(symbol, {})
    
    # Parse bars from each timeframe
    d1_bars = _bars_from_chart(sym_chart.get("D1", []))
    h4_bars = _bars_from_chart(sym_chart.get("H4", []))
    h1_bars = _bars_from_chart(sym_chart.get("H1", []))
    m15_bars = _bars_from_chart(sym_chart.get("M15", []))
    
    # MT5 metadata
    chart_meta = {k: sym_chart[k] for k in sym_chart if k not in (
        "D1","H4","H1","M30","M15","M5","M1"
    )}
    
    # Compute structure components
    weekly = _weekly_bias(d1_bars)
    daily = _daily_structure(d1_bars, chart_meta)
    h4 = _tf_structure(h4_bars, "H4")
    h1_struct = _tf_structure(h1_bars, "H1")
    m15_struct = _tf_structure(m15_bars, "M15")
    cycle = _detect_cycle_phase(h4_bars)
    session = _session_state(datetime.now(timezone.utc))
    
    # Compute multi-timeframe alignment
    mtf = _mtf_alignment(weekly, daily, h4, h1_struct, m15_struct)
    
    return {
        "symbol": symbol,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "weekly": weekly,
        "daily": daily,
        "h4": h4,
        "h1": h1_struct,
        "m15": m15_struct,
        "cycle_phase": cycle,
        "session": session,
        "mtf_alignment": mtf,
        "meta": {
            "d1_bars": len(d1_bars),
            "h4_bars": len(h4_bars),
            "h1_bars": len(h1_bars),
            "m15_bars": len(m15_bars),
        },
    }


def build_all_market_structures(mt5_data: dict, symbols: List[str]) -> dict:
    """Build market structure for all symbols and wrap into top-level dict."""
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbols": {},
    }
    for sym in symbols:
        try:
            result["symbols"][sym] = build_market_structure(sym, mt5_data)
        except Exception as e:
            log.error("market_structure failed for %s: %s", sym, e)
            result["symbols"][sym] = {"error": str(e)}
    return result


def write_market_structure(path: str, data: dict) -> None:
    """Write market_structure.json atomically (write temp → rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(p)


def load_market_structure(path: str) -> Optional[dict]:
    """Read market_structure.json. Returns None if missing or stale (>2min)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("timestamp", "")
        if ts:
            t = datetime.fromisoformat(ts)
            age_s = (datetime.now(timezone.utc) - t).total_seconds()
            if age_s > 120:
                log.warning("market_structure.json stale — age=%.0fs", age_s)
                data["_stale"] = True
        return data
    except Exception as e:
        log.error("load_market_structure: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# CLI self-test
# ══════════════════════════════════════════════════════════════════════════════

def _self_test():
    import json, sys
    from pathlib import Path
    
    test_path = (
        Path.home() / "Library/Application Support"
        / "net.metaquotes.wine.metatrader5/drive_c/users/user"
        / "AppData/Roaming/MetaQuotes/Terminal/Common/Files"
        / "omni_data.json"
    )
    
    if not test_path.exists():
        print(f"No MT5 data at {test_path}")
        sys.exit(1)
    
    with open(test_path) as f:
        data = json.load(f)
    
    result = build_all_market_structures(data, ["XAUUSD", "XAGUSD"])
    
    print(json.dumps(result, indent=2, default=str))
    print(f"\n--- XAUUSD Structure Summary ---")
    xau = result["symbols"].get("XAUUSD", {})
    if "error" in xau:
        print(f"ERROR: {xau['error']}")
    else:
        print(f"Weekly:   {xau['weekly']['direction']} (conf={xau['weekly']['confidence']})")
        print(f"Daily:    {xau['daily']['direction']} (quarter={xau['daily']['quarter']})")
        print(f"H4:       {xau['h4']['direction']}")
        print(f"H1:       {xau['h1']['direction']}")
        print(f"M15:      {xau['m15']['direction']}")
        print(f"Cycle:    {xau['cycle_phase']['phase']} day {xau['cycle_phase']['day_number']} (conf={xau['cycle_phase']['confidence']})")
        print(f"MTF:      {xau['mtf_alignment']['grade']} score={xau['mtf_alignment']['score']} tradeable={xau['mtf_alignment']['tradeable']}")
        print(f"Session:  {xau['session']['current_session']} killzone={xau['session']['in_killzone']}")


if __name__ == "__main__":
    _self_test()
