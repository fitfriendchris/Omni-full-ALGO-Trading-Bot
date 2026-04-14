"""
ict_precision.py — ICT Precision Entry Calculator
Detects: liquidity sweeps, OB retests, FVG fills, session H/L grabs
Multi-timeframe: D1 bias → H4 structure → H1/M15 entry → M5 trigger
"""

from __future__ import annotations
import json
import re
import os
import math
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional


JSON_PATH = "/Users/owner/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"


@dataclass
class Bar:
    time: str
    o: float
    h: float
    l: float
    c: float
    v: int

    @property
    def bullish(self): return self.c > self.o
    @property
    def bearish(self): return self.c < self.o
    @property
    def body(self): return abs(self.c - self.o)
    @property
    def range(self): return self.h - self.l
    @property
    def upper_wick(self): return self.h - max(self.o, self.c)
    @property
    def lower_wick(self): return min(self.o, self.c) - self.l


@dataclass
class ICTSetup:
    symbol:     str
    direction:  str          # BUY or SELL
    entry_type: str          # OB_RETEST, FVG_FILL, SWEEP_REVERSAL, PDH_SWEEP, PWL_SWEEP
    entry_price: float       # Limit order price
    sl_price:   float        # Stop loss
    tp1_price:  float        # First target (50% exit)
    tp2_price:  float        # Second target (30% exit)
    tp3_price:  float        # Runner (20% exit)
    confidence: int          # 0-100
    reasons:    list = field(default_factory=list)
    session:    str = ""
    amd_phase:  str = ""
    rr_ratio:   float = 0.0
    tf_bias:    str = ""     # D1 bias direction
    invalidation: float = 0.0  # Price that invalidates setup


def _load():
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = re.sub(r',\s*([\]}])', r'\1', f.read())
        return json.loads(raw)
    except Exception as e:
        print(f"[ICT] Load error: {e}")
        return {}


def _parse_bars(bars_data: list) -> list[Bar]:
    bars = []
    for b in bars_data:
        bars.append(Bar(
            time=b.get("t", ""),
            o=b.get("o", 0), h=b.get("h", 0),
            l=b.get("l", 0), c=b.get("c", 0),
            v=b.get("v", 0)
        ))
    return bars


# ── Swing Detection ───────────────────────────────────────────────────────────

def find_swing_highs(bars: list[Bar], lookback: int = 3) -> list[tuple[int, float]]:
    """Find swing highs: bar[i].h > all neighbors within lookback."""
    swings = []
    for i in range(lookback, len(bars) - lookback):
        is_high = all(bars[i].h >= bars[i-j].h and bars[i].h >= bars[i+j].h
                      for j in range(1, lookback + 1))
        if is_high:
            swings.append((i, bars[i].h))
    return swings


def find_swing_lows(bars: list[Bar], lookback: int = 3) -> list[tuple[int, float]]:
    """Find swing lows: bar[i].l < all neighbors within lookback."""
    swings = []
    for i in range(lookback, len(bars) - lookback):
        is_low = all(bars[i].l <= bars[i-j].l and bars[i].l <= bars[i+j].l
                     for j in range(1, lookback + 1))
        if is_low:
            swings.append((i, bars[i].l))
    return swings


def find_equal_highs(bars: list[Bar], tolerance_pct: float = 0.002) -> list[float]:
    """Equal highs = liquidity pools (multiple touches of same level)."""
    highs = [b.h for b in bars]
    equal_highs = []
    for i in range(len(highs)):
        count = sum(1 for j in range(len(highs))
                    if i != j and abs(highs[i] - highs[j]) / highs[i] < tolerance_pct)
        if count >= 1:  # At least 2 touches total
            equal_highs.append(highs[i])
    # Deduplicate
    result = []
    for h in sorted(set(equal_highs), reverse=True):
        if not any(abs(h - r) / h < tolerance_pct for r in result):
            result.append(h)
    return result


def find_equal_lows(bars: list[Bar], tolerance_pct: float = 0.002) -> list[float]:
    lows = [b.l for b in bars]
    equal_lows = []
    for i in range(len(lows)):
        if lows[i] == 0:
            continue
        count = sum(1 for j in range(len(lows))
                    if i != j and abs(lows[i] - lows[j]) / lows[i] < tolerance_pct)
        if count >= 1:
            equal_lows.append(lows[i])
    result = []
    for l in sorted(set(equal_lows)):
        if not any(abs(l - r) / l < tolerance_pct for r in result):
            result.append(l)
    return result


# ── Sweep Detection ───────────────────────────────────────────────────────────

def detect_sweep_high(bars: list[Bar], level: float, tolerance_pct: float = 0.001) -> bool:
    """
    Bullish sweep then reversal:
    Most recent bars[0] (newest) swept above level then closed back below.
    This is a stop hunt of buy-stops above equal highs → expect DOWN move.
    """
    if len(bars) < 3:
        return False
    b0, b1 = bars[0], bars[1]
    # b1 swept above level (wick above) but closed near/below
    swept = b1.h > level * (1 + tolerance_pct)
    reversed_close = b1.c < level or b0.c < level
    return swept and reversed_close


def detect_sweep_low(bars: list[Bar], level: float, tolerance_pct: float = 0.001) -> bool:
    """
    Bearish sweep then reversal:
    Price swept below level (hit sell-stops) then closed back above → expect UP move.
    """
    if len(bars) < 3:
        return False
    b0, b1 = bars[0], bars[1]
    swept = b1.l < level * (1 - tolerance_pct)
    reversed_close = b1.c > level or b0.c > level
    return swept and reversed_close


# ── Order Block Finder ────────────────────────────────────────────────────────

def find_bullish_ob(bars: list[Bar], start: int = 0, search: int = 15) -> Optional[tuple[float, float]]:
    """
    Bullish OB: last bearish candle before a strong bullish impulse.
    Returns (ob_low, ob_high) — price zone to enter long.
    """
    for i in range(start, min(start + search, len(bars) - 2)):
        b = bars[i]
        b_next = bars[i + 1] if i + 1 < len(bars) else None
        if b_next is None:
            continue
        # Bearish candle followed by strong bullish move
        if b.bearish and b_next.bullish:
            impulse = b_next.body / (b.range + 0.0001)
            if impulse > 0.6:  # Strong impulse
                return (b.l, b.h)
    return None


def find_bearish_ob(bars: list[Bar], start: int = 0, search: int = 15) -> Optional[tuple[float, float]]:
    """
    Bearish OB: last bullish candle before a strong bearish impulse.
    Returns (ob_low, ob_high) — price zone to enter short.
    """
    for i in range(start, min(start + search, len(bars) - 2)):
        b = bars[i]
        b_next = bars[i + 1] if i + 1 < len(bars) else None
        if b_next is None:
            continue
        if b.bullish and b_next.bearish:
            impulse = b_next.body / (b.range + 0.0001)
            if impulse > 0.6:
                return (b.l, b.h)
    return None


# ── FVG Finder ────────────────────────────────────────────────────────────────

def find_bullish_fvg(bars: list[Bar]) -> Optional[tuple[float, float]]:
    """Bullish FVG: bars[2].h < bars[0].l (gap between candles 0 and 2)."""
    for i in range(len(bars) - 2):
        if bars[i].l > bars[i + 2].h:
            return (bars[i + 2].h, bars[i].l)
    return None


def find_bearish_fvg(bars: list[Bar]) -> Optional[tuple[float, float]]:
    """Bearish FVG: bars[2].l > bars[0].h."""
    for i in range(len(bars) - 2):
        if bars[i].h < bars[i + 2].l:
            return (bars[i].h, bars[i + 2].l)
    return None


# ── Bias Detector ─────────────────────────────────────────────────────────────

def get_d1_bias(bars: list[Bar]) -> str:
    """D1 trend direction based on recent structure."""
    if len(bars) < 5:
        return "NEUTRAL"
    # Check if making higher highs and higher lows
    recent = bars[:10]
    highs = [b.h for b in recent]
    lows  = [b.l for b in recent]
    # Simple: compare first half vs second half
    mid = len(recent) // 2
    if not mid:
        return "NEUTRAL"
    avg_h1 = sum(highs[:mid]) / mid
    avg_h2 = sum(highs[mid:]) / mid
    avg_l1 = sum(lows[:mid]) / mid
    avg_l2 = sum(lows[mid:]) / mid
    if avg_h1 > avg_h2 and avg_l1 > avg_l2:
        return "BULLISH"
    elif avg_h1 < avg_h2 and avg_l1 < avg_l2:
        return "BEARISH"
    return "NEUTRAL"


def get_h4_structure(bars: list[Bar]) -> str:
    """H4 market structure (BOS detection)."""
    if len(bars) < 10:
        return "RANGING"
    swing_highs = find_swing_highs(bars, lookback=2)
    swing_lows  = find_swing_lows(bars, lookback=2)
    current = bars[0].c

    if swing_highs and current > swing_highs[-1][1]:
        return "BOS_BULLISH"
    if swing_lows and current < swing_lows[-1][1]:
        return "BOS_BEARISH"
    return "RANGING"


# ── Main Setup Scanner ────────────────────────────────────────────────────────

def scan_symbol(symbol: str, data: dict) -> list[ICTSetup]:
    """
    Full ICT multi-TF scan for one symbol.
    Returns list of ICTSetup objects (high-confidence setups only).
    """
    charts  = data.get("charts", {})
    sym_data = charts.get(symbol, {})
    if not sym_data:
        return []

    session   = data.get("session", "—")
    amd_phase = data.get("amd_phase", "—")

    # Parse bars for each timeframe
    d1_bars  = _parse_bars(sym_data.get("D1",  []))
    h4_bars  = _parse_bars(sym_data.get("H4",  []))
    h1_bars  = _parse_bars(sym_data.get("H1",  []))
    m15_bars = _parse_bars(sym_data.get("M15", []))
    m5_bars  = _parse_bars(sym_data.get("M5",  []))

    if not d1_bars or not h4_bars or not h1_bars:
        return []

    # Key levels from EA
    pdh = sym_data.get("pdh", 0)
    pdl = sym_data.get("pdl", 0)
    pwh = sym_data.get("pwh", 0)
    pwl = sym_data.get("pwl", 0)
    pmh = sym_data.get("pmh", 0)
    pml = sym_data.get("pml", 0)

    # Symbol info for lot sizing
    tick_size  = sym_data.get("tick_size", 0.01)
    tick_value = sym_data.get("tick_value", 1.0)
    point      = sym_data.get("point", 0.0001)

    current_price = h1_bars[0].c if h1_bars else 0
    if current_price == 0:
        return []

    setups = []

    # ── Step 1: D1 Bias ─────────────────────────────────────────────
    d1_bias = get_d1_bias(d1_bars)

    # ── Step 2: H4 Structure ────────────────────────────────────────
    h4_struct = get_h4_structure(h4_bars)

    # ── Step 2b: Quarter Theory (Daily Range Position) ──────────────
    d1_high = max((b.h for b in d1_bars[:5]), default=0)
    d1_low  = min((b.l for b in d1_bars[:5] if b.l > 0), default=0)
    quarter_info = get_quarter_position(current_price, d1_high, d1_low)

    # ── Step 2c: Technical Pattern Detection ────────────────────────
    detected_patterns = []
    dt = detect_double_top(h1_bars[:30])
    if dt:    detected_patterns.append(dt)
    db = detect_double_bottom(h1_bars[:30])
    if db:    detected_patterns.append(db)
    hs = detect_head_shoulders(h1_bars[:30])
    if hs:    detected_patterns.append(hs)
    ihs = detect_inverse_head_shoulders(h1_bars[:30])
    if ihs:   detected_patterns.append(ihs)
    wdg = detect_wedge(h1_bars[:20])
    if wdg:   detected_patterns.append(wdg)

    # Also check M15 for short-term patterns
    if m15_bars:
        dt15 = detect_double_top(m15_bars[:30])
        if dt15: detected_patterns.append(dt15)
        db15 = detect_double_bottom(m15_bars[:30])
        if db15: detected_patterns.append(db15)

    # ── Step 2d: Push/Exhaustion Phase ──────────────────────────────
    push_exh_h1  = detect_push_exhaustion(h1_bars)
    push_exh_m15 = detect_push_exhaustion(m15_bars) if m15_bars else {"phase": "NEUTRAL"}

    # Use the more specific M15 reading if available
    push_exh = push_exh_m15 if push_exh_m15["phase"] != "NEUTRAL" else push_exh_h1

    # ── Step 2e: Support / Resistance Analysis ───────────────────────
    sr_info_h1  = find_key_levels(h1_bars)
    sr_info_h4  = find_key_levels(h4_bars, tolerance_pct=0.003)

    # ── Step 2f: Opening Gaps (NDOG / NWOG) ─────────────────────────
    opening_gaps = get_opening_gap_levels(sym_data)

    # ── Step 2g: Breaker Blocks ──────────────────────────────────────
    bull_breaker = find_bullish_breaker(h1_bars)
    bear_breaker = find_bearish_breaker(h1_bars)

    # ── Step 2h: All FVGs with CE levels ────────────────────────────
    all_bull_fvgs = find_all_bullish_fvgs(h1_bars, max_fvgs=3)
    all_bear_fvgs = find_all_bearish_fvgs(h1_bars, max_fvgs=3)

    # ── Step 2i: GMT hour for Kill Zone / Silver Bullet ──────────────
    try:
        gmt_str  = data.get("gmt_time", "")
        gmt_hour = int(gmt_str.split(" ")[1].split(":")[0]) if gmt_str else datetime.now(timezone.utc).hour
        gmt_min  = int(gmt_str.split(" ")[1].split(":")[1]) if gmt_str else datetime.now(timezone.utc).minute
    except Exception:
        gmt_hour = datetime.now(timezone.utc).hour
        gmt_min  = datetime.now(timezone.utc).minute

    # ── Step 3: Liquidity Levels ────────────────────────────────────
    h4_eq_highs = find_equal_highs(h4_bars, tolerance_pct=0.002)
    h4_eq_lows  = find_equal_lows(h4_bars,  tolerance_pct=0.002)
    h1_eq_highs = find_equal_highs(h1_bars, tolerance_pct=0.001)
    h1_eq_lows  = find_equal_lows(h1_bars,  tolerance_pct=0.001)

    # Include opening gap levels as liquidity targets
    gap_highs = ([opening_gaps["ndog"]["high"]] if "ndog" in opening_gaps else []) + \
                ([opening_gaps["nwog"]["high"]] if "nwog" in opening_gaps else [])
    gap_lows  = ([opening_gaps["ndog"]["low"]]  if "ndog" in opening_gaps else []) + \
                ([opening_gaps["nwog"]["low"]  ] if "nwog" in opening_gaps else [])

    liq_highs = sorted(set(h4_eq_highs + h1_eq_highs + [pdh, pwh, pmh] + gap_highs), reverse=True)
    liq_lows  = sorted(set(h4_eq_lows  + h1_eq_lows  + [pdl, pwl, pml] + gap_lows))
    liq_highs = [l for l in liq_highs if l > current_price * 0.99]
    liq_lows  = [l for l in liq_lows  if l < current_price * 1.01 and l > 0]

    # ── Step 4: Sweep Detection on M15/H1 ───────────────────────────

    # BEARISH SETUP: Sweep of high → SELL from OB below sweep
    for level in liq_highs[:5]:  # Check top 5 liquidity highs
        sweep_bars = m15_bars if m15_bars else h1_bars
        if detect_sweep_high(sweep_bars, level):
            # Find bearish OB to sell from
            ob = find_bearish_ob(h1_bars, start=0, search=10)
            if not ob:
                ob = find_bearish_ob(m15_bars, start=0, search=8) if m15_bars else None

            if ob:
                ob_low, ob_high = ob

                # Pinpoint OB entry using precision levels
                ob_prec = get_ob_precision_entry(ob_low, ob_high, "SELL")
                entry = ob_prec["ote_50"]   # Enter at OB midpoint (50% OTE)

                sl = ob_high + (ob_high - ob_low) * 1.2  # SL above OB with buffer
                risk = sl - entry
                if risk <= 0:
                    continue
                tp1 = entry - risk * 1.5
                tp2 = entry - risk * 2.5
                tp3 = entry - risk * 4.0

                # Override TP with actual liquidity levels if closer
                for liq_l in liq_lows:
                    if liq_l < entry - risk * 0.5:
                        tp1 = min(tp1, liq_l + (entry - liq_l) * 0.1)
                        break

                confidence, extra_reasons = _score_sell_setup_full(
                    d1_bias, h4_struct, amd_phase, session, level, pdh, pwh,
                    quarter_info=quarter_info,
                    patterns=detected_patterns,
                    sr_info=sr_info_h1,
                    push_exh=push_exh,
                )

                if confidence >= 45:
                    pattern_names = [p.get("pattern","") for p in detected_patterns if p.get("direction") == "SELL"]
                    reasons = [
                        f"Liquidity sweep of {level:.5g} — stop hunt confirmed",
                        f"Bearish OB: {ob_low:.5g}—{ob_high:.5g} | Precision entry @ {entry:.5g} (OTE 50%)",
                        f"D1 Bias: {d1_bias} | H4: {h4_struct}",
                        f"AMD Phase: {amd_phase} | Session: {session}",
                        f"Quarter: {quarter_info['quarter']} ({quarter_info['pct']:.0f}% of daily range)",
                    ]
                    if pattern_names:
                        reasons.append(f"Patterns confirmed: {', '.join(pattern_names)}")
                    if push_exh.get("signal"):
                        reasons.append(f"Momentum: {push_exh['signal']}")
                    if sr_info_h1.get("phase_at_resistance"):
                        reasons.append(f"S/R: {sr_info_h1['phase_at_resistance']}")
                    reasons.extend(extra_reasons)
                    if level == pdh:
                        reasons.insert(0, "Previous Day High swept — HIGH PRIORITY")
                    elif level == pwh:
                        reasons.insert(0, "Previous Week High swept — HIGH PRIORITY")
                    elif level == pmh:
                        reasons.insert(0, "Previous Month High swept")

                    rr = abs(tp1 - entry) / abs(sl - entry) if abs(sl - entry) > 0 else 0
                    setups.append(ICTSetup(
                        symbol=symbol, direction="SELL",
                        entry_type="SWEEP_HIGH_OB",
                        entry_price=round(entry, 5),
                        sl_price=round(sl, 5),
                        tp1_price=round(tp1, 5),
                        tp2_price=round(tp2, 5),
                        tp3_price=round(tp3, 5),
                        confidence=confidence,
                        reasons=reasons,
                        session=session, amd_phase=amd_phase,
                        rr_ratio=round(rr, 2),
                        tf_bias=d1_bias,
                        invalidation=round(sl * 1.002, 5),
                    ))

    # BULLISH SETUP: Sweep of low → BUY from OB above sweep
    for level in liq_lows[:5]:
        sweep_bars = m15_bars if m15_bars else h1_bars
        if detect_sweep_low(sweep_bars, level):
            ob = find_bullish_ob(h1_bars, start=0, search=10)
            if not ob:
                ob = find_bullish_ob(m15_bars, start=0, search=8) if m15_bars else None

            if ob:
                ob_low, ob_high = ob

                # Pinpoint OB entry using precision levels
                ob_prec = get_ob_precision_entry(ob_low, ob_high, "BUY")
                entry = ob_prec["ote_50"]   # Enter at OB midpoint (50% OTE)

                sl = ob_low - (ob_high - ob_low) * 1.2
                risk = entry - sl
                if risk <= 0:
                    continue
                tp1 = entry + risk * 1.5
                tp2 = entry + risk * 2.5
                tp3 = entry + risk * 4.0

                for liq_h in liq_highs:
                    if liq_h > entry + risk * 0.5:
                        tp1 = max(tp1, liq_h - (liq_h - entry) * 0.1)
                        break

                confidence, extra_reasons = _score_buy_setup_full(
                    d1_bias, h4_struct, amd_phase, session, level, pdl, pwl,
                    quarter_info=quarter_info,
                    patterns=detected_patterns,
                    sr_info=sr_info_h1,
                    push_exh=push_exh,
                )

                if confidence >= 45:
                    pattern_names = [p.get("pattern","") for p in detected_patterns if p.get("direction") == "BUY"]
                    reasons = [
                        f"Liquidity sweep of {level:.5g} — sell stops hunted",
                        f"Bullish OB: {ob_low:.5g}—{ob_high:.5g} | Precision entry @ {entry:.5g} (OTE 50%)",
                        f"D1 Bias: {d1_bias} | H4: {h4_struct}",
                        f"AMD Phase: {amd_phase} | Session: {session}",
                        f"Quarter: {quarter_info['quarter']} ({quarter_info['pct']:.0f}% of daily range)",
                    ]
                    if pattern_names:
                        reasons.append(f"Patterns confirmed: {', '.join(pattern_names)}")
                    if push_exh.get("signal"):
                        reasons.append(f"Momentum: {push_exh['signal']}")
                    if sr_info_h1.get("phase_at_support"):
                        reasons.append(f"S/R: {sr_info_h1['phase_at_support']}")
                    reasons.extend(extra_reasons)
                    if level == pdl:
                        reasons.insert(0, "Previous Day Low swept — HIGH PRIORITY")
                    elif level == pwl:
                        reasons.insert(0, "Previous Week Low swept — HIGH PRIORITY")
                    elif level == pml:
                        reasons.insert(0, "Previous Month Low swept")

                    rr = abs(tp1 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
                    setups.append(ICTSetup(
                        symbol=symbol, direction="BUY",
                        entry_type="SWEEP_LOW_OB",
                        entry_price=round(entry, 5),
                        sl_price=round(sl, 5),
                        tp1_price=round(tp1, 5),
                        tp2_price=round(tp2, 5),
                        tp3_price=round(tp3, 5),
                        confidence=confidence,
                        reasons=reasons,
                        session=session, amd_phase=amd_phase,
                        rr_ratio=round(rr, 2),
                        tf_bias=d1_bias,
                        invalidation=round(sl * 0.998, 5),
                    ))

    # ── Step 5: FVG Entries (no sweep needed) ───────────────────────
    # Bullish FVG on H1 with D1 bullish bias
    if d1_bias == "BULLISH" or h4_struct in ("BOS_BULLISH", "CHOCH_BULL"):
        fvg = find_bullish_fvg(h1_bars[:8])
        if fvg:
            fvg_low, fvg_high = fvg
            if fvg_low < current_price:  # Price hasn't filled it yet
                # Enter at FVG midpoint (OTE)
                entry = fvg_low + (fvg_high - fvg_low) * 0.50
                sl    = fvg_low - (fvg_high - fvg_low) * 1.5
                risk  = entry - sl
                if risk > 0:
                    tp1 = entry + risk * 1.5
                    tp2 = entry + risk * 2.5
                    tp3 = entry + risk * 4.0

                    # Use enhanced scoring
                    fvg_q = get_quarter_position(entry, d1_high, d1_low)
                    confidence, extra_r = _score_buy_setup_full(
                        d1_bias, h4_struct, amd_phase, session, fvg_low, pdl, pwl,
                        quarter_info=fvg_q, patterns=detected_patterns,
                        sr_info=sr_info_h1, push_exh=push_exh,
                    )
                    # FVG base confidence slightly lower than sweep (no sweep confirmation)
                    confidence = max(40, confidence - 5)

                    if confidence >= 45:
                        rr = abs(tp1 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
                        pat_names = [p.get("pattern","") for p in detected_patterns if p.get("direction")=="BUY"]
                        reasons = [
                            f"Bullish FVG (imbalance): {fvg_low:.5g}—{fvg_high:.5g}",
                            f"Entry at FVG midpoint OTE: {entry:.5g}",
                            f"D1 bias {d1_bias} | H4 {h4_struct}",
                            f"Session: {session} | Phase: {amd_phase}",
                            f"Quarter: {fvg_q['quarter']} ({fvg_q['pct']:.0f}% of daily range)",
                        ]
                        if pat_names:
                            reasons.append(f"Patterns: {', '.join(pat_names)}")
                        if push_exh.get("signal"):
                            reasons.append(f"Momentum: {push_exh['signal']}")
                        reasons.extend(extra_r)
                        setups.append(ICTSetup(
                            symbol=symbol, direction="BUY",
                            entry_type="FVG_FILL_BULLISH",
                            entry_price=round(entry, 5),
                            sl_price=round(sl, 5),
                            tp1_price=round(tp1, 5),
                            tp2_price=round(tp2, 5),
                            tp3_price=round(tp3, 5),
                            confidence=confidence,
                            reasons=reasons,
                            session=session, amd_phase=amd_phase,
                            rr_ratio=round(rr, 2), tf_bias=d1_bias,
                            invalidation=round(sl * 0.999, 5),
                        ))

    if d1_bias == "BEARISH" or h4_struct in ("BOS_BEARISH", "CHOCH_BEAR"):
        fvg = find_bearish_fvg(h1_bars[:8])
        if fvg:
            fvg_low, fvg_high = fvg
            if fvg_high > current_price:
                entry = fvg_high - (fvg_high - fvg_low) * 0.50
                sl    = fvg_high + (fvg_high - fvg_low) * 1.5
                risk  = sl - entry
                if risk > 0:
                    tp1 = entry - risk * 1.5
                    tp2 = entry - risk * 2.5
                    tp3 = entry - risk * 4.0

                    fvg_q = get_quarter_position(entry, d1_high, d1_low)
                    confidence, extra_r = _score_sell_setup_full(
                        d1_bias, h4_struct, amd_phase, session, fvg_high, pdh, pwh,
                        quarter_info=fvg_q, patterns=detected_patterns,
                        sr_info=sr_info_h1, push_exh=push_exh,
                    )
                    confidence = max(40, confidence - 5)

                    if confidence >= 45:
                        rr = abs(entry - tp1) / abs(sl - entry) if abs(sl - entry) > 0 else 0
                        pat_names = [p.get("pattern","") for p in detected_patterns if p.get("direction")=="SELL"]
                        reasons = [
                            f"Bearish FVG (imbalance): {fvg_low:.5g}—{fvg_high:.5g}",
                            f"Entry at FVG midpoint OTE: {entry:.5g}",
                            f"D1 bias {d1_bias} | H4 {h4_struct}",
                            f"Session: {session} | Phase: {amd_phase}",
                            f"Quarter: {fvg_q['quarter']} ({fvg_q['pct']:.0f}% of daily range)",
                        ]
                        if pat_names:
                            reasons.append(f"Patterns: {', '.join(pat_names)}")
                        if push_exh.get("signal"):
                            reasons.append(f"Momentum: {push_exh['signal']}")
                        reasons.extend(extra_r)
                        setups.append(ICTSetup(
                            symbol=symbol, direction="SELL",
                            entry_type="FVG_FILL_BEARISH",
                            entry_price=round(entry, 5),
                            sl_price=round(sl, 5),
                            tp1_price=round(tp1, 5),
                            tp2_price=round(tp2, 5),
                            tp3_price=round(tp3, 5),
                            confidence=confidence,
                            reasons=reasons,
                            session=session, amd_phase=amd_phase,
                            rr_ratio=round(rr, 2), tf_bias=d1_bias,
                            invalidation=round(sl * 1.001, 5),
                        ))

    # ── Step 6: Breaker Block Setups ─────────────────────────────────
    if bull_breaker and (d1_bias == "BULLISH" or h4_struct == "BOS_BULLISH"):
        bb      = bull_breaker
        entry   = bb["ce"]                        # Enter at 50% of old OB (CE)
        sl      = bb["ob_low"] - (bb["ob_high"] - bb["ob_low"]) * 0.5
        risk    = entry - sl
        if risk > 0:
            tp1 = entry + risk * 1.5
            tp2 = entry + risk * 2.5
            tp3 = entry + risk * 4.0
            bb_q  = get_quarter_position(entry, d1_high, d1_low)
            conf, extra_r = _score_buy_setup_full(
                d1_bias, h4_struct, amd_phase, session, entry, pdl, pwl,
                quarter_info=bb_q, patterns=detected_patterns,
                sr_info=sr_info_h1, push_exh=push_exh,
            )
            conf += bb["confidence_bonus"]
            conf = min(conf, 100)
            rr = abs(tp1 - entry) / risk if risk > 0 else 0
            setups.append(ICTSetup(
                symbol=symbol, direction="BUY",
                entry_type="BREAKER_BLOCK_BUY",
                entry_price=round(entry, 5), sl_price=round(sl, 5),
                tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                confidence=conf,
                reasons=[bb["reason"]] + extra_r,
                session=session, amd_phase=amd_phase,
                rr_ratio=round(rr, 2), tf_bias=d1_bias,
                invalidation=round(sl * 0.999, 5),
            ))

    if bear_breaker and (d1_bias == "BEARISH" or h4_struct == "BOS_BEARISH"):
        bb      = bear_breaker
        entry   = bb["ce"]
        sl      = bb["ob_high"] + (bb["ob_high"] - bb["ob_low"]) * 0.5
        risk    = sl - entry
        if risk > 0:
            tp1 = entry - risk * 1.5
            tp2 = entry - risk * 2.5
            tp3 = entry - risk * 4.0
            bb_q  = get_quarter_position(entry, d1_high, d1_low)
            conf, extra_r = _score_sell_setup_full(
                d1_bias, h4_struct, amd_phase, session, entry, pdh, pwh,
                quarter_info=bb_q, patterns=detected_patterns,
                sr_info=sr_info_h1, push_exh=push_exh,
            )
            conf += bb["confidence_bonus"]
            conf = min(conf, 100)
            rr = abs(entry - tp1) / risk if risk > 0 else 0
            setups.append(ICTSetup(
                symbol=symbol, direction="SELL",
                entry_type="BREAKER_BLOCK_SELL",
                entry_price=round(entry, 5), sl_price=round(sl, 5),
                tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                confidence=conf,
                reasons=[bb["reason"]] + extra_r,
                session=session, amd_phase=amd_phase,
                rr_ratio=round(rr, 2), tf_bias=d1_bias,
                invalidation=round(sl * 1.001, 5),
            ))

    # ── Step 7: Silver Bullet Model ───────────────────────────────────
    # Only fires during 10-11 AM EST or 2-3 PM EST windows.
    # Requires: identified draw on liquidity + MSS + first FVG in direction.
    sb_window = is_silver_bullet_window(gmt_hour, gmt_min)
    if sb_window:
        sb_bonus, sb_reason = _score_silver_bullet_bonus(gmt_hour, gmt_min)

        # Silver Bullet BUY: in SB window, D1/H4 bullish, first BISI FVG above current price
        if (d1_bias == "BULLISH" or h4_struct == "BOS_BULLISH") and all_bull_fvgs:
            for fvg in all_bull_fvgs[:2]:
                if fvg["low"] < current_price:    # FVG is below — enter at CE on pullback
                    entry = fvg["ce"]
                    sl    = fvg["low"] - (fvg["high"] - fvg["low"]) * 1.0
                    risk  = entry - sl
                    if risk <= 0:
                        continue
                    tp1 = entry + risk * 1.5
                    tp2 = entry + risk * 2.5
                    tp3 = entry + risk * 4.0
                    fvg_q  = get_quarter_position(entry, d1_high, d1_low)
                    conf, extra_r = _score_buy_setup_full(
                        d1_bias, h4_struct, amd_phase, session, fvg["low"], pdl, pwl,
                        quarter_info=fvg_q, patterns=detected_patterns,
                        sr_info=sr_info_h1, push_exh=push_exh,
                    )
                    conf = min(conf + sb_bonus, 100)
                    rr = abs(tp1 - entry) / risk if risk > 0 else 0
                    reasons = [
                        sb_reason,
                        f"BISI FVG: {fvg['low']:.5g}–{fvg['high']:.5g} | CE entry: {fvg['ce']:.5g}",
                        f"D1 bias {d1_bias} | H4 {h4_struct}",
                    ] + extra_r
                    setups.append(ICTSetup(
                        symbol=symbol, direction="BUY",
                        entry_type=f"SILVER_BULLET_{sb_window}",
                        entry_price=round(entry, 5), sl_price=round(sl, 5),
                        tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                        confidence=conf,
                        reasons=reasons,
                        session=session, amd_phase=amd_phase,
                        rr_ratio=round(rr, 2), tf_bias=d1_bias,
                        invalidation=round(sl * 0.999, 5),
                    ))
                    break   # Only take first qualifying FVG

        # Silver Bullet SELL
        if (d1_bias == "BEARISH" or h4_struct == "BOS_BEARISH") and all_bear_fvgs:
            for fvg in all_bear_fvgs[:2]:
                if fvg["high"] > current_price:   # FVG is above — enter at CE on rally
                    entry = fvg["ce"]
                    sl    = fvg["high"] + (fvg["high"] - fvg["low"]) * 1.0
                    risk  = sl - entry
                    if risk <= 0:
                        continue
                    tp1 = entry - risk * 1.5
                    tp2 = entry - risk * 2.5
                    tp3 = entry - risk * 4.0
                    fvg_q  = get_quarter_position(entry, d1_high, d1_low)
                    conf, extra_r = _score_sell_setup_full(
                        d1_bias, h4_struct, amd_phase, session, fvg["high"], pdh, pwh,
                        quarter_info=fvg_q, patterns=detected_patterns,
                        sr_info=sr_info_h1, push_exh=push_exh,
                    )
                    conf = min(conf + sb_bonus, 100)
                    rr = abs(entry - tp1) / risk if risk > 0 else 0
                    reasons = [
                        sb_reason,
                        f"SIBI FVG: {fvg['low']:.5g}–{fvg['high']:.5g} | CE entry: {fvg['ce']:.5g}",
                        f"D1 bias {d1_bias} | H4 {h4_struct}",
                    ] + extra_r
                    setups.append(ICTSetup(
                        symbol=symbol, direction="SELL",
                        entry_type=f"SILVER_BULLET_{sb_window}",
                        entry_price=round(entry, 5), sl_price=round(sl, 5),
                        tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                        confidence=conf,
                        reasons=reasons,
                        session=session, amd_phase=amd_phase,
                        rr_ratio=round(rr, 2), tf_bias=d1_bias,
                        invalidation=round(sl * 1.001, 5),
                    ))
                    break

    # Sort by confidence
    setups.sort(key=lambda x: x.confidence, reverse=True)
    return setups


def _score_sell_setup(d1_bias, h4_struct, amd, session, level, pdh, pwh) -> int:
    score = 35
    if d1_bias == "BEARISH":   score += 20
    if d1_bias == "NEUTRAL":   score += 5
    if h4_struct == "BOS_BEARISH": score += 15
    if amd == "DISTRIBUTION":  score += 15
    if amd == "MANIPULATION":  score += 10
    if session == "LONDON":    score += 10
    if session == "NEW_YORK":  score += 10
    if level == pdh:           score += 15
    if level == pwh:           score += 20
    return min(score, 100)


def _score_buy_setup(d1_bias, h4_struct, amd, session, level, pdl, pwl) -> int:
    score = 35
    if d1_bias == "BULLISH":   score += 20
    if d1_bias == "NEUTRAL":   score += 5
    if h4_struct == "BOS_BULLISH": score += 15
    if amd == "DISTRIBUTION":  score += 15
    if amd == "MANIPULATION":  score += 10
    if session == "LONDON":    score += 10
    if session == "NEW_YORK":  score += 10
    if level == pdl:           score += 15
    if level == pwl:           score += 20
    return min(score, 100)


# ═══════════════════════════════════════════════════════════════════════════════
# QUARTER THEORY
# ═══════════════════════════════════════════════════════════════════════════════

def get_quarter_position(price: float, period_high: float, period_low: float) -> dict:
    """
    ICT Quarter Theory: divides any price range into 4 equal quarters.

    Q1 (0–25%)  — Discount / Buy zone: institutions accumulate here
    Q2 (25–50%) — Below equilibrium: still favourable for longs
    Q3 (50–75%) — Above equilibrium: favourable for shorts
    Q4 (75–100%) — Premium / Sell zone: institutions distribute here

    Optimal Trade Entry (OTE) Fibonacci sits at 62–79% retracement into a
    discount (for longs) or premium (for shorts).

    Returns dict: quarter, pct, is_discount, is_premium, ote_zone_low, ote_zone_high
    """
    if period_high <= period_low or period_high == 0:
        return {"quarter": "UNKNOWN", "pct": 50.0, "is_discount": False,
                "is_premium": False, "ote_low": 0, "ote_high": 0, "equilibrium": 0}

    rng = period_high - period_low
    pct = (price - period_low) / rng * 100

    if pct <= 25:
        quarter = "Q1"
    elif pct <= 50:
        quarter = "Q2"
    elif pct <= 75:
        quarter = "Q3"
    else:
        quarter = "Q4"

    equilibrium = period_low + rng * 0.5  # 50% of range = fair value midpoint

    # OTE zone: 62–79% retracement INTO a discount or premium
    # For longs: 62–79% pullback from high → price is at 21–38% of range
    ote_low  = period_low + rng * 0.21   # 79% retrace from high
    ote_high = period_low + rng * 0.38   # 62% retrace from high

    return {
        "quarter":      quarter,
        "pct":          round(pct, 1),
        "is_discount":  pct <= 50,
        "is_premium":   pct >= 50,
        "ote_low":      round(ote_low, 5),
        "ote_high":     round(ote_high, 5),
        "equilibrium":  round(equilibrium, 5),
        "period_high":  period_high,
        "period_low":   period_low,
    }


def get_ob_precision_entry(ob_low: float, ob_high: float, direction: str) -> dict:
    """
    Pinpoint entry levels within an Order Block.

    ICT logic:
    - 50% of OB body = Optimal Trade Entry (OTE midpoint)
    - 79% Fibonacci of OB range = deepest OTE entry
    - OB body open level = institutional entry reference
    - Entry limit orders placed at OB midpoint, not extremes

    Returns dict with entry, ote_50, ote_62, ote_79, body_open
    """
    rng     = ob_high - ob_low
    mid     = ob_low + rng * 0.50   # 50% = standard OTE
    fib_62  = ob_low + rng * 0.62   # 62% Fibonacci
    fib_79  = ob_low + rng * 0.79   # 79% deep OTE (most precise)

    if direction == "BUY":
        # For long: enter at the OB bottom area (discount of the OB)
        entry = round(ob_low + rng * 0.50, 5)   # Enter at OB midpoint
        ote_50 = round(ob_low + rng * 0.50, 5)
        ote_62 = round(ob_low + rng * 0.38, 5)  # 62% from top = 38% from bottom
        ote_79 = round(ob_low + rng * 0.21, 5)  # 79% from top = 21% from bottom
    else:
        # For short: enter at OB top area (premium of the OB)
        entry = round(ob_high - rng * 0.50, 5)
        ote_50 = round(ob_high - rng * 0.50, 5)
        ote_62 = round(ob_high - rng * 0.38, 5)
        ote_79 = round(ob_high - rng * 0.21, 5)

    return {
        "entry":     entry,
        "ote_50":    ote_50,
        "ote_62":    ote_62,
        "ote_79":    ote_79,
        "ob_low":    round(ob_low, 5),
        "ob_high":   round(ob_high, 5),
        "ob_mid":    round(mid, 5),
        "ob_range":  round(rng, 5),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TECHNICAL PATTERN DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def detect_double_top(bars: list[Bar], tolerance_pct: float = 0.003,
                      min_separation: int = 5) -> Optional[dict]:
    """
    Double Top: two peaks at same level with a valley between → SELL signal.

    Structure:
      Peak 1 → valley (pull-back) → Peak 2 (≈ same height) → breakdown below neckline
    Entry: at neckline break or retest of neckline after break
    Target: neckline − (peak − neckline)
    """
    if len(bars) < min_separation + 4:
        return None

    highs = [(i, b.h) for i, b in enumerate(bars)]
    highs.sort(key=lambda x: -x[1])

    if len(highs) < 2:
        return None

    p1_idx, p1_h = highs[0]
    # Find second peak: similar height, separated by min_separation bars
    for p2_idx, p2_h in highs[1:]:
        if abs(p2_idx - p1_idx) < min_separation:
            continue
        if abs(p1_h - p2_h) / p1_h > tolerance_pct:
            continue  # Heights must match within tolerance

        # Neckline = lowest low between the two peaks
        lo_idx, hi_idx = min(p1_idx, p2_idx), max(p1_idx, p2_idx)
        valley_bars = bars[lo_idx:hi_idx + 1]
        if not valley_bars:
            continue
        neckline = min(b.l for b in valley_bars)
        pattern_height = ((p1_h + p2_h) / 2) - neckline

        # Confirm current price is near or below neckline (breakdown)
        current = bars[0].c
        breakdown = current <= neckline * 1.005  # within 0.5% of neckline

        return {
            "pattern":       "DOUBLE_TOP",
            "direction":     "SELL",
            "peak1":         round(p1_h, 5),
            "peak2":         round(p2_h, 5),
            "neckline":      round(neckline, 5),
            "target":        round(neckline - pattern_height, 5),
            "breakdown":     breakdown,
            "height":        round(pattern_height, 5),
            "confidence_bonus": 15 if breakdown else 8,
            "reason": (f"Double Top: peaks at {p1_h:.5g} & {p2_h:.5g} | "
                       f"neckline {neckline:.5g} | target {neckline - pattern_height:.5g}"),
        }
    return None


def detect_double_bottom(bars: list[Bar], tolerance_pct: float = 0.003,
                         min_separation: int = 5) -> Optional[dict]:
    """
    Double Bottom: two troughs at same level → BUY signal.
    """
    if len(bars) < min_separation + 4:
        return None

    lows = [(i, b.l) for i, b in enumerate(bars) if b.l > 0]
    lows.sort(key=lambda x: x[1])

    if len(lows) < 2:
        return None

    b1_idx, b1_l = lows[0]
    for b2_idx, b2_l in lows[1:]:
        if abs(b2_idx - b1_idx) < min_separation:
            continue
        if abs(b1_l - b2_l) / max(b1_l, 0.0001) > tolerance_pct:
            continue

        lo_idx, hi_idx = min(b1_idx, b2_idx), max(b1_idx, b2_idx)
        peak_bars = bars[lo_idx:hi_idx + 1]
        if not peak_bars:
            continue
        neckline = max(b.h for b in peak_bars)
        pattern_height = neckline - ((b1_l + b2_l) / 2)

        current = bars[0].c
        breakout = current >= neckline * 0.995

        return {
            "pattern":       "DOUBLE_BOTTOM",
            "direction":     "BUY",
            "bottom1":       round(b1_l, 5),
            "bottom2":       round(b2_l, 5),
            "neckline":      round(neckline, 5),
            "target":        round(neckline + pattern_height, 5),
            "breakout":      breakout,
            "height":        round(pattern_height, 5),
            "confidence_bonus": 15 if breakout else 8,
            "reason": (f"Double Bottom: lows at {b1_l:.5g} & {b2_l:.5g} | "
                       f"neckline {neckline:.5g} | target {neckline + pattern_height:.5g}"),
        }
    return None


def detect_head_shoulders(bars: list[Bar], min_bars: int = 20) -> Optional[dict]:
    """
    Head & Shoulders (bearish): left shoulder < head > right shoulder.
    Neckline connects left and right shoulder lows.
    """
    if len(bars) < min_bars:
        return None

    # Require recent (last 30 bars) price data
    recent = bars[:min(30, len(bars))]
    highs = [b.h for b in recent]
    lows  = [b.l for b in recent]

    if len(highs) < 5:
        return None

    # Find the global high (head)
    head_idx = highs.index(max(highs))
    if head_idx < 3 or head_idx > len(highs) - 4:
        return None

    # Left shoulder: local high to the left of head
    left_range  = highs[:head_idx]
    right_range = highs[head_idx + 1:]

    if not left_range or not right_range:
        return None

    ls_h = max(left_range)
    rs_h = max(right_range)
    head_h = highs[head_idx]

    # Both shoulders must be lower than head and roughly equal
    if ls_h >= head_h or rs_h >= head_h:
        return None
    shoulder_diff = abs(ls_h - rs_h) / max(ls_h, 0.0001)
    if shoulder_diff > 0.05:  # Shoulders within 5% of each other
        return None

    # Neckline = average of the lows between head and each shoulder
    ls_idx = left_range.index(ls_h)
    rs_idx = head_idx + 1 + right_range.index(rs_h)

    left_valley  = min(lows[ls_idx:head_idx + 1]) if ls_idx < head_idx else lows[head_idx]
    right_valley = min(lows[head_idx:rs_idx + 1]) if head_idx < rs_idx else lows[rs_idx]
    neckline = (left_valley + right_valley) / 2

    pattern_height = head_h - neckline
    target = neckline - pattern_height

    current = bars[0].c
    breakdown = current <= neckline * 1.003

    return {
        "pattern":       "HEAD_SHOULDERS",
        "direction":     "SELL",
        "left_shoulder": round(ls_h, 5),
        "head":          round(head_h, 5),
        "right_shoulder": round(rs_h, 5),
        "neckline":      round(neckline, 5),
        "target":        round(target, 5),
        "breakdown":     breakdown,
        "confidence_bonus": 18 if breakdown else 10,
        "reason": (f"H&S: LS={ls_h:.5g} Head={head_h:.5g} RS={rs_h:.5g} | "
                   f"neckline {neckline:.5g} | target {target:.5g}"),
    }


def detect_inverse_head_shoulders(bars: list[Bar], min_bars: int = 20) -> Optional[dict]:
    """Inverse Head & Shoulders (bullish reversal)."""
    if len(bars) < min_bars:
        return None

    recent = bars[:min(30, len(bars))]
    lows  = [b.l for b in recent]
    highs = [b.h for b in recent]

    if len(lows) < 5:
        return None

    head_idx = lows.index(min(lows))
    if head_idx < 3 or head_idx > len(lows) - 4:
        return None

    left_range  = lows[:head_idx]
    right_range = lows[head_idx + 1:]

    if not left_range or not right_range:
        return None

    ls_l = min(left_range)
    rs_l = min(right_range)
    head_l = lows[head_idx]

    if ls_l <= head_l or rs_l <= head_l:
        return None
    shoulder_diff = abs(ls_l - rs_l) / max(ls_l, 0.0001)
    if shoulder_diff > 0.05:
        return None

    ls_idx = left_range.index(ls_l)
    rs_idx = head_idx + 1 + right_range.index(rs_l)

    left_peak  = max(highs[ls_idx:head_idx + 1]) if ls_idx < head_idx else highs[head_idx]
    right_peak = max(highs[head_idx:rs_idx + 1]) if head_idx < rs_idx else highs[rs_idx]
    neckline = (left_peak + right_peak) / 2

    pattern_height = neckline - head_l
    target = neckline + pattern_height

    current = bars[0].c
    breakout = current >= neckline * 0.997

    return {
        "pattern":       "INVERSE_HEAD_SHOULDERS",
        "direction":     "BUY",
        "left_shoulder": round(ls_l, 5),
        "head":          round(head_l, 5),
        "right_shoulder": round(rs_l, 5),
        "neckline":      round(neckline, 5),
        "target":        round(target, 5),
        "breakout":      breakout,
        "confidence_bonus": 18 if breakout else 10,
        "reason": (f"Inv H&S: LS={ls_l:.5g} Head={head_l:.5g} RS={rs_l:.5g} | "
                   f"neckline {neckline:.5g} | target {target:.5g}"),
    }


def detect_wedge(bars: list[Bar], min_bars: int = 10) -> Optional[dict]:
    """
    Rising Wedge (bearish): both highs and lows rising but converging.
    Falling Wedge (bullish): both highs and lows falling but converging.

    Uses linear regression slope on highs and lows.
    """
    if len(bars) < min_bars:
        return None

    recent = bars[:min(20, len(bars))]
    n = len(recent)
    xs = list(range(n))
    hs = [b.h for b in recent]
    ls = [b.l for b in recent]

    def slope(vals):
        x_mean = sum(xs) / n
        y_mean = sum(vals) / n
        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, vals))
        den = sum((x - x_mean) ** 2 for x in xs)
        return num / den if den != 0 else 0

    h_slope = slope(hs)
    l_slope = slope(ls)

    # Wedge: slopes in same direction but converging (high slope < low slope for rising)
    convergence = abs(h_slope - l_slope) / max(abs(h_slope) + abs(l_slope), 0.0001)

    if convergence < 0.1:   # Not converging enough
        return None

    if h_slope > 0 and l_slope > 0 and h_slope < l_slope:
        # Rising wedge (bearish) — lows rising faster → converging toward top
        return {
            "pattern":    "RISING_WEDGE",
            "direction":  "SELL",
            "h_slope":    round(h_slope, 6),
            "l_slope":    round(l_slope, 6),
            "confidence_bonus": 12,
            "reason": f"Rising Wedge (bearish): highs slope={h_slope:.5g}, lows slope={l_slope:.5g} — converging upward",
        }
    elif h_slope < 0 and l_slope < 0 and h_slope > l_slope:
        # Falling wedge (bullish) — highs falling faster → converging toward bottom
        return {
            "pattern":    "FALLING_WEDGE",
            "direction":  "BUY",
            "h_slope":    round(h_slope, 6),
            "l_slope":    round(l_slope, 6),
            "confidence_bonus": 12,
            "reason": f"Falling Wedge (bullish): highs slope={h_slope:.5g}, lows slope={l_slope:.5g} — converging downward",
        }
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PUSH / EXHAUSTION PHASE DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def detect_push_exhaustion(bars: list[Bar], lookback: int = 6) -> dict:
    """
    Push phase:     candles in same direction with increasing body size → momentum building.
    Exhaustion phase: candles in same direction but decreasing body size + increasing wicks
                      → momentum fading, reversal likely.

    Returns dict: phase, direction, candle_count, body_trend, wick_trend, signal
    """
    if len(bars) < lookback + 1:
        return {"phase": "NEUTRAL", "direction": "NONE", "signal": ""}

    recent = bars[:lookback]
    bodies = [b.body for b in recent]
    wicks  = [b.upper_wick + b.lower_wick for b in recent]

    # Determine dominant direction of the recent candles
    bull_count = sum(1 for b in recent if b.bullish)
    bear_count = sum(1 for b in recent if b.bearish)

    if bull_count >= lookback - 1:
        direction = "UP"
    elif bear_count >= lookback - 1:
        direction = "DOWN"
    else:
        return {"phase": "NEUTRAL", "direction": "MIXED", "signal": ""}

    # Linear trend of bodies
    n = len(bodies)
    xs = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(bodies) / n
    den = sum((x - x_mean) ** 2 for x in xs)
    body_slope = (sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, bodies))
                  / den) if den != 0 else 0
    wick_slope = (sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, wicks))
                  / den) if den != 0 else 0

    # Push: bodies growing, wicks small
    if body_slope > 0 and wick_slope <= 0:
        phase = "PUSH"
        signal = (f"PUSH phase — {direction} momentum building "
                  f"({bull_count if direction=='UP' else bear_count}/{lookback} candles, "
                  f"bodies growing)")
    # Exhaustion: bodies shrinking, wicks growing
    elif body_slope < 0 and wick_slope > 0:
        phase = "EXHAUSTION"
        signal = (f"EXHAUSTION — {direction} move fading "
                  f"(bodies shrinking, wicks growing — potential reversal)")
    else:
        phase = "NEUTRAL"
        signal = ""

    return {
        "phase":       phase,
        "direction":   direction,
        "candle_count": lookback,
        "body_slope":  round(body_slope, 6),
        "wick_slope":  round(wick_slope, 6),
        "signal":      signal,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPORT / RESISTANCE ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def find_key_levels(bars: list[Bar], tolerance_pct: float = 0.002,
                    min_touches: int = 2) -> dict:
    """
    Identify significant support and resistance levels from price structure.

    Also classifies whether price is:
    - PUSHING through level (accelerating, continuation)
    - EXHAUSTING at level (fading, reversal probable)
    - TESTING level (first approach, wait for confirmation)

    Returns dict: supports, resistances, nearest_support, nearest_resistance,
                  current_phase_at_level
    """
    if len(bars) < 10:
        return {"supports": [], "resistances": [], "nearest_support": 0, "nearest_resistance": 0}

    current_price = bars[0].c
    all_highs = [b.h for b in bars]
    all_lows  = [b.l for b in bars if b.l > 0]

    # Cluster levels that are within tolerance of each other
    def cluster_levels(levels: list[float]) -> list[dict]:
        if not levels:
            return []
        sorted_lvls = sorted(set(round(l, 5) for l in levels))
        clusters = []
        cur_cluster = [sorted_lvls[0]]
        for lvl in sorted_lvls[1:]:
            if abs(lvl - cur_cluster[-1]) / max(cur_cluster[-1], 0.0001) < tolerance_pct:
                cur_cluster.append(lvl)
            else:
                if len(cur_cluster) >= min_touches:
                    clusters.append({
                        "level":   round(sum(cur_cluster) / len(cur_cluster), 5),
                        "touches": len(cur_cluster),
                        "strength": min(len(cur_cluster) / 5, 1.0),
                    })
                cur_cluster = [lvl]
        if len(cur_cluster) >= min_touches:
            clusters.append({
                "level":   round(sum(cur_cluster) / len(cur_cluster), 5),
                "touches": len(cur_cluster),
                "strength": min(len(cur_cluster) / 5, 1.0),
            })
        return clusters

    supports    = [lvl for lvl in cluster_levels(all_lows)  if lvl["level"] < current_price]
    resistances = [lvl for lvl in cluster_levels(all_highs) if lvl["level"] > current_price]

    nearest_sup = max((s["level"] for s in supports),    default=0)
    nearest_res = min((r["level"] for r in resistances), default=0)

    # Round numbers (psychological S/R)
    round_levels = _get_round_levels(current_price)

    # Phase at nearest levels
    push_exh = detect_push_exhaustion(bars)
    phase_at_resistance = ""
    phase_at_support    = ""

    if nearest_res > 0 and abs(current_price - nearest_res) / nearest_res < 0.005:
        if push_exh["phase"] == "PUSH" and push_exh["direction"] == "UP":
            phase_at_resistance = "PUSHING_INTO_RESISTANCE"
        elif push_exh["phase"] == "EXHAUSTION" and push_exh["direction"] == "UP":
            phase_at_resistance = "EXHAUSTING_AT_RESISTANCE"
        else:
            phase_at_resistance = "TESTING_RESISTANCE"

    if nearest_sup > 0 and abs(current_price - nearest_sup) / nearest_sup < 0.005:
        if push_exh["phase"] == "PUSH" and push_exh["direction"] == "DOWN":
            phase_at_support = "PUSHING_THROUGH_SUPPORT"
        elif push_exh["phase"] == "EXHAUSTION" and push_exh["direction"] == "DOWN":
            phase_at_support = "EXHAUSTING_AT_SUPPORT"
        else:
            phase_at_support = "TESTING_SUPPORT"

    return {
        "supports":             supports,
        "resistances":          resistances,
        "nearest_support":      nearest_sup,
        "nearest_resistance":   nearest_res,
        "round_levels":         round_levels,
        "phase_at_resistance":  phase_at_resistance,
        "phase_at_support":     phase_at_support,
        "push_exhaustion":      push_exh,
    }


def _get_round_levels(price: float) -> list[float]:
    """Identify nearby psychological round number levels."""
    if price <= 0:
        return []

    # Determine appropriate step based on price magnitude
    if price >= 1000:   step = 50.0    # Gold: 1800, 1850, 1900
    elif price >= 100:  step = 10.0
    elif price >= 10:   step = 1.0
    elif price >= 1:    step = 0.10
    else:               step = 0.010

    base = round(price / step) * step
    return [round(base + i * step, 5) for i in range(-3, 4) if base + i * step != price]


# ═══════════════════════════════════════════════════════════════════════════════
# ENHANCED SCORING (Quarter + Patterns + S/R + Push/Exhaustion)
# ═══════════════════════════════════════════════════════════════════════════════

def _score_buy_setup_full(d1_bias: str, h4_struct: str, amd: str, session: str,
                          level: float, pdl: float, pwl: float,
                          quarter_info: dict = None,
                          patterns: list = None,
                          sr_info: dict = None,
                          push_exh: dict = None) -> tuple[int, list[str]]:
    """
    Full enhanced buy setup scoring with all ICT factors.
    Returns (score, extra_reasons).
    """
    score = 35
    reasons = []

    # Base ICT factors
    if d1_bias == "BULLISH":    score += 20; reasons.append("D1 Bias: BULLISH")
    elif d1_bias == "NEUTRAL":  score += 5
    if h4_struct == "BOS_BULLISH": score += 15; reasons.append("H4 BOS Bullish")
    elif h4_struct == "CHOCH_BULL": score += 12; reasons.append("H4 CHoCH Bullish")
    if amd == "DISTRIBUTION":   score += 15; reasons.append("AMD: Distribution (NY active)")
    if amd == "MANIPULATION":   score += 10; reasons.append("AMD: Manipulation (sweep phase)")
    if session == "LONDON":     score += 10; reasons.append("London Kill Zone active")
    if session == "NEW_YORK":   score += 10; reasons.append("NY Kill Zone active")
    if session == "ASIA":       score += 3;  reasons.append("Asia session (lower liq)")
    if level == pdl:            score += 15; reasons.append("Previous Day Low swept")
    if level == pwl:            score += 20; reasons.append("Previous Week Low swept")

    # Quarter Theory
    if quarter_info:
        q = quarter_info.get("quarter", "")
        pct = quarter_info.get("pct", 50)
        if q == "Q1":
            score += 20
            reasons.append(f"Q1 Discount zone ({pct:.0f}% of range) — ideal BUY area")
        elif q == "Q2":
            score += 12
            reasons.append(f"Q2 Below equilibrium ({pct:.0f}% of range) — BUY favourable")
        elif q == "Q3":
            score -= 5
            reasons.append(f"Q3 Above equilibrium ({pct:.0f}%) — BUY less optimal")
        elif q == "Q4":
            score -= 12
            reasons.append(f"Q4 Premium zone ({pct:.0f}%) — avoid BUY here")

        eq = quarter_info.get("equilibrium", 0)
        if eq > 0 and quarter_info.get("ote_low", 0) > 0:
            ote_l = quarter_info["ote_low"]
            ote_h = quarter_info["ote_high"]
            price = quarter_info.get("period_low", 0)
            if price and ote_l <= price <= ote_h:
                score += 15
                reasons.append(f"Price in OTE Fibonacci zone ({ote_l:.5g}–{ote_h:.5g}) — highest precision entry")

    # Technical Patterns
    for p in (patterns or []):
        if isinstance(p, dict):
            pat_dir = p.get("direction", "")
            bonus   = p.get("confidence_bonus", 10)
            name    = p.get("pattern", "")
            if pat_dir == "BUY":
                score += bonus
                reasons.append(f"Pattern: {name} ({'+' + str(bonus)} confluence)")

    # S/R Context
    if sr_info:
        phase_sup = sr_info.get("phase_at_support", "")
        if phase_sup == "EXHAUSTING_AT_SUPPORT":
            score += 15
            reasons.append("Exhaustion at support — reversal probable")
        elif phase_sup == "TESTING_SUPPORT":
            score += 8
            reasons.append("Testing key support level")
        elif phase_sup == "PUSHING_THROUGH_SUPPORT":
            score -= 10
            reasons.append("Pushing THROUGH support — wait for reclaim")

        nearest_sup = sr_info.get("nearest_support", 0)
        if nearest_sup > 0:
            reasons.append(f"Nearest support: {nearest_sup:.5g}")

    # Push / Exhaustion
    if push_exh:
        phase = push_exh.get("phase", "")
        dirn  = push_exh.get("direction", "")
        if phase == "EXHAUSTION" and dirn == "DOWN":
            score += 12
            reasons.append("Down-move EXHAUSTING — reversal signal for BUY")
        elif phase == "PUSH" and dirn == "UP":
            score += 8
            reasons.append("Bullish PUSH momentum confirmed")
        elif phase == "PUSH" and dirn == "DOWN":
            score -= 8
            reasons.append("Bearish PUSH — avoid BUY into momentum")

    return min(score, 100), reasons


def _score_sell_setup_full(d1_bias: str, h4_struct: str, amd: str, session: str,
                           level: float, pdh: float, pwh: float,
                           quarter_info: dict = None,
                           patterns: list = None,
                           sr_info: dict = None,
                           push_exh: dict = None) -> tuple[int, list[str]]:
    """Full enhanced sell setup scoring."""
    score = 35
    reasons = []

    if d1_bias == "BEARISH":    score += 20; reasons.append("D1 Bias: BEARISH")
    elif d1_bias == "NEUTRAL":  score += 5
    if h4_struct == "BOS_BEARISH": score += 15; reasons.append("H4 BOS Bearish")
    elif h4_struct == "CHOCH_BEAR": score += 12; reasons.append("H4 CHoCH Bearish")
    if amd == "DISTRIBUTION":   score += 15; reasons.append("AMD: Distribution")
    if amd == "MANIPULATION":   score += 10; reasons.append("AMD: Manipulation")
    if session == "LONDON":     score += 10; reasons.append("London Kill Zone active")
    if session == "NEW_YORK":   score += 10; reasons.append("NY Kill Zone active")
    if session == "ASIA":       score += 3;  reasons.append("Asia session")
    if level == pdh:            score += 15; reasons.append("Previous Day High swept")
    if level == pwh:            score += 20; reasons.append("Previous Week High swept")

    if quarter_info:
        q   = quarter_info.get("quarter", "")
        pct = quarter_info.get("pct", 50)
        if q == "Q4":
            score += 20
            reasons.append(f"Q4 Premium zone ({pct:.0f}% of range) — ideal SELL area")
        elif q == "Q3":
            score += 12
            reasons.append(f"Q3 Above equilibrium ({pct:.0f}%) — SELL favourable")
        elif q == "Q2":
            score -= 5
            reasons.append(f"Q2 Below equilibrium ({pct:.0f}%) — SELL less optimal")
        elif q == "Q1":
            score -= 12
            reasons.append(f"Q1 Discount zone ({pct:.0f}%) — avoid SELL here")

        if quarter_info.get("ote_low", 0) > 0:
            ote_l = quarter_info["ote_low"]
            ote_h = quarter_info["ote_high"]
            p_high = quarter_info.get("period_high", 0)
            if p_high:
                ote_sell_l = p_high - (quarter_info["period_high"] - quarter_info["period_low"]) * 0.38
                ote_sell_h = p_high - (quarter_info["period_high"] - quarter_info["period_low"]) * 0.21
                if ote_sell_l <= p_high <= ote_sell_h:
                    score += 15
                    reasons.append("Price in OTE Fibonacci SELL zone — highest precision entry")

    for p in (patterns or []):
        if isinstance(p, dict):
            pat_dir = p.get("direction", "")
            bonus   = p.get("confidence_bonus", 10)
            name    = p.get("pattern", "")
            if pat_dir == "SELL":
                score += bonus
                reasons.append(f"Pattern: {name} (+{bonus} confluence)")

    if sr_info:
        phase_res = sr_info.get("phase_at_resistance", "")
        if phase_res == "EXHAUSTING_AT_RESISTANCE":
            score += 15
            reasons.append("Exhaustion at resistance — reversal probable")
        elif phase_res == "TESTING_RESISTANCE":
            score += 8
            reasons.append("Testing key resistance level")
        elif phase_res == "PUSHING_INTO_RESISTANCE":
            score -= 5
            reasons.append("Still pushing up — wait for rejection candle")

        nearest_res = sr_info.get("nearest_resistance", 0)
        if nearest_res > 0:
            reasons.append(f"Nearest resistance: {nearest_res:.5g}")

    if push_exh:
        phase = push_exh.get("phase", "")
        dirn  = push_exh.get("direction", "")
        if phase == "EXHAUSTION" and dirn == "UP":
            score += 12
            reasons.append("Up-move EXHAUSTING — reversal signal for SELL")
        elif phase == "PUSH" and dirn == "DOWN":
            score += 8
            reasons.append("Bearish PUSH momentum confirmed")
        elif phase == "PUSH" and dirn == "UP":
            score -= 8
            reasons.append("Bullish PUSH — avoid SELL into momentum")

    return min(score, 100), reasons


# ═══════════════════════════════════════════════════════════════════════════════
# CONSEQUENT ENCROACHMENT (CE) — 50% of any FVG or Gap
# ═══════════════════════════════════════════════════════════════════════════════

def fvg_consequent_encroachment(fvg_low: float, fvg_high: float) -> float:
    """
    CE = the 50% midpoint of a Fair Value Gap.
    ICT: price commonly retraces to the CE of an FVG before continuing.
    This is the highest-precision entry point within an FVG.
    """
    return round((fvg_low + fvg_high) / 2, 5)


def find_all_bullish_fvgs(bars: list[Bar], max_fvgs: int = 5) -> list[dict]:
    """
    Find all unmitigated bullish FVGs (BISI — Buyside Imbalance Sellside Inefficiency).
    Returns list of dicts with low, high, ce (50%), and whether it's been partially filled.
    """
    fvgs = []
    current_price = bars[0].c if bars else 0
    for i in range(len(bars) - 2):
        b0, b2 = bars[i], bars[i + 2]
        if b0.l > b2.h:   # Gap: candle[i].low > candle[i+2].high
            low  = b2.h
            high = b0.l
            ce   = fvg_consequent_encroachment(low, high)
            # Skip if fully mitigated (price has traded through it)
            if current_price > low:   # Price is above → still below FVG? No — if price above high, FVG filled
                if current_price > high:
                    continue   # Fully above — already mitigated
            fvgs.append({
                "type":        "BISI",
                "direction":   "BUY",
                "low":         round(low, 5),
                "high":        round(high, 5),
                "ce":          ce,
                "size":        round(high - low, 5),
                "bar_index":   i,
                "mitigated":   current_price < low,   # Price hasn't returned yet
            })
        if len(fvgs) >= max_fvgs:
            break
    return fvgs


def find_all_bearish_fvgs(bars: list[Bar], max_fvgs: int = 5) -> list[dict]:
    """
    Find all unmitigated bearish FVGs (SIBI — Sellside Imbalance Buyside Inefficiency).
    """
    fvgs = []
    current_price = bars[0].c if bars else 0
    for i in range(len(bars) - 2):
        b0, b2 = bars[i], bars[i + 2]
        if b0.h < b2.l:   # Gap: candle[i].high < candle[i+2].low
            low  = b0.h
            high = b2.l
            ce   = fvg_consequent_encroachment(low, high)
            if current_price < high:
                if current_price < low:
                    continue   # Fully below — already mitigated
            fvgs.append({
                "type":        "SIBI",
                "direction":   "SELL",
                "low":         round(low, 5),
                "high":        round(high, 5),
                "ce":          ce,
                "size":        round(high - low, 5),
                "bar_index":   i,
                "mitigated":   current_price > high,
            })
        if len(fvgs) >= max_fvgs:
            break
    return fvgs


# ═══════════════════════════════════════════════════════════════════════════════
# BREAKER BLOCKS
# ═══════════════════════════════════════════════════════════════════════════════

def find_bullish_breaker(bars: list[Bar]) -> Optional[dict]:
    """
    Bullish Breaker: a bearish OB that price has closed ABOVE (breaking the OB).
    The OB zone now acts as support — institutions re-enter longs here.

    Logic:
    1. Find a bearish OB (last bullish candle before bearish impulse)
    2. Price then closed above the OB high (breaking out)
    3. On retest of the OB zone from above = Breaker Block BUY entry
    """
    if len(bars) < 10:
        return None

    current_price = bars[0].c

    # Look back for a bearish OB that has since been broken above
    for i in range(2, min(20, len(bars) - 2)):
        b = bars[i]
        # Bearish OB = bullish candle before a bearish impulse
        if not b.bullish:
            continue
        b_next = bars[i - 1]
        if not b_next.bearish:
            continue
        impulse = b_next.body / (b.range + 0.0001)
        if impulse < 0.5:
            continue

        ob_low  = b.l
        ob_high = b.h

        # Check if a more recent candle has closed above ob_high (breakout)
        breakout_above = any(bars[j].c > ob_high for j in range(0, i - 1))
        if not breakout_above:
            continue

        # Confirm price is currently retesting the OB zone from above
        in_retest_zone = ob_low <= current_price <= ob_high * 1.002
        if in_retest_zone:
            return {
                "type":       "BULLISH_BREAKER",
                "direction":  "BUY",
                "ob_low":     round(ob_low, 5),
                "ob_high":    round(ob_high, 5),
                "ce":         round((ob_low + ob_high) / 2, 5),
                "bar_index":  i,
                "reason":     f"Bullish Breaker Block retest: {ob_low:.5g}–{ob_high:.5g} (former bearish OB now support)",
                "confidence_bonus": 14,
            }
    return None


def find_bearish_breaker(bars: list[Bar]) -> Optional[dict]:
    """
    Bearish Breaker: a bullish OB that price has closed BELOW (breaking down).
    The OB zone now acts as resistance — institutions re-enter shorts here.
    """
    if len(bars) < 10:
        return None

    current_price = bars[0].c

    for i in range(2, min(20, len(bars) - 2)):
        b = bars[i]
        if not b.bearish:
            continue
        b_next = bars[i - 1]
        if not b_next.bullish:
            continue
        impulse = b_next.body / (b.range + 0.0001)
        if impulse < 0.5:
            continue

        ob_low  = b.l
        ob_high = b.h

        # Breakout below ob_low
        breakout_below = any(bars[j].c < ob_low for j in range(0, i - 1))
        if not breakout_below:
            continue

        in_retest_zone = ob_low * 0.998 <= current_price <= ob_high
        if in_retest_zone:
            return {
                "type":       "BEARISH_BREAKER",
                "direction":  "SELL",
                "ob_low":     round(ob_low, 5),
                "ob_high":    round(ob_high, 5),
                "ce":         round((ob_low + ob_high) / 2, 5),
                "bar_index":  i,
                "reason":     f"Bearish Breaker Block retest: {ob_low:.5g}–{ob_high:.5g} (former bullish OB now resistance)",
                "confidence_bonus": 14,
            }
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# OPENING GAPS (NDOG / NWOG)
# ═══════════════════════════════════════════════════════════════════════════════

def get_opening_gap_levels(sym_data: dict) -> dict:
    """
    Extract New Day Opening Gap (NDOG) and New Week Opening Gap (NWOG) from EA data.
    These are powerful institutional reference points — price often returns to fill them.

    A gap exists when today's open ≠ yesterday's close.
    Gap filled = price trades through the gap zone.
    CE of gap = 50% of the gap range.
    """
    ndog_close = sym_data.get("ndog_close", 0)
    ndog_open  = sym_data.get("ndog_open",  0)
    nwog_close = sym_data.get("nwog_close", 0)
    nwog_open  = sym_data.get("nwog_open",  0)

    result = {}

    if ndog_close > 0 and ndog_open > 0 and abs(ndog_close - ndog_open) > 0:
        gap_low  = min(ndog_close, ndog_open)
        gap_high = max(ndog_close, ndog_open)
        result["ndog"] = {
            "low":       round(gap_low,  5),
            "high":      round(gap_high, 5),
            "ce":        round((gap_low + gap_high) / 2, 5),
            "direction": "BUY" if ndog_open > ndog_close else "SELL",  # gap up = bullish fill target below
            "size":      round(gap_high - gap_low, 5),
        }

    if nwog_close > 0 and nwog_open > 0 and abs(nwog_close - nwog_open) > 0:
        gap_low  = min(nwog_close, nwog_open)
        gap_high = max(nwog_close, nwog_open)
        result["nwog"] = {
            "low":       round(gap_low,  5),
            "high":      round(gap_high, 5),
            "ce":        round((gap_low + gap_high) / 2, 5),
            "direction": "BUY" if nwog_open > nwog_close else "SELL",
            "size":      round(gap_high - gap_low, 5),
        }

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# KILL ZONE & SILVER BULLET TIME WINDOWS
# ═══════════════════════════════════════════════════════════════════════════════

def get_killzone(gmt_hour: int) -> str:
    """
    Returns the current ICT Kill Zone name, or '' if outside all kill zones.
    All times UTC (GMT).
    """
    if 22 <= gmt_hour or gmt_hour < 1:
        return "ASIA_KZ"
    if 7 <= gmt_hour < 9:
        return "LONDON_OPEN"
    if 11 <= gmt_hour < 13:
        return "LONDON_CLOSE"
    if 12 <= gmt_hour < 15:
        return "NY_OPEN"
    if 19 <= gmt_hour < 21:
        return "NY_CLOSE"
    return ""


def is_silver_bullet_window(gmt_hour: int, gmt_minute: int = 0) -> str:
    """
    ICT Silver Bullet — two specific time windows (New York local time):
      • 10:00–11:00 AM EST  = UTC 15:00–16:00 (summer) / 16:00–17:00 (winter)
      • 14:00–15:00 PM EST  = UTC 19:00–20:00 (summer) / 20:00–21:00 (winter)

    We use a fixed 4-hour offset (UTC-4, EDT summer).  During winter (EST, UTC-5)
    the windows shift one hour later — handle this by accepting a ±1 hour tolerance.

    Returns: 'SB_AM' | 'SB_PM' | ''
    """
    # AM Silver Bullet: 10–11 AM EST = 14–16 UTC (EDT±1h tolerance)
    if 14 <= gmt_hour < 16:
        return "SB_AM"
    # PM Silver Bullet: 14–15 PM EST = 18–20 UTC
    if 18 <= gmt_hour < 20:
        return "SB_PM"
    return ""


def _score_silver_bullet_bonus(gmt_hour: int, gmt_minute: int = 0) -> tuple[int, str]:
    """Returns (confidence_bonus, reason) if in a Silver Bullet window."""
    sb = is_silver_bullet_window(gmt_hour, gmt_minute)
    if sb == "SB_AM":
        return 15, "Silver Bullet AM window (10-11 AM EST) — highest precision entry"
    if sb == "SB_PM":
        return 12, "Silver Bullet PM window (2-3 PM EST) — high precision entry"
    return 0, ""


# ═══════════════════════════════════════════════════════════════════════════════
# HIERARCHICAL CONFLUENCE GATE
# Time > Liquidity > Inefficiency > Structure (per ICT's own priority order)
# ═══════════════════════════════════════════════════════════════════════════════

def hierarchical_confluence_score(
    session: str,
    gmt_hour: int,
    liquidity_swept: bool,
    has_fvg: bool,
    has_ob: bool,
    has_bos: bool,
    amd_phase: str,
    d1_bias_aligned: bool,
    extras: list = None,
) -> tuple[int, list[str]]:
    """
    Score a setup using ICT's own hierarchy:

    Step 1 — Time (Kill Zone / Silver Bullet):
        Outside kill zone = hard -20, no silver bullet = -5.
        In Silver Bullet = +15.

    Step 2 — Liquidity (External draw):
        Liquidity swept = +25 (the trigger event — most important).
        No sweep = setup is speculative only.

    Step 3 — Inefficiency (FVG / Gap):
        FVG present and aligned = +15, CE entry = additional +10.

    Step 4 — Structure (BOS / CHoCH + OB):
        BOS aligned = +15.
        OB present = +10.
        D1 bias aligned = +10.

    AMD phase bonus: Manipulation/Distribution = +10.

    Returns (score, reasons).
    """
    score   = 30   # base
    reasons = []
    extras  = extras or []

    # ── Step 1: Time ─────────────────────────────────────────────────────
    kz = get_killzone(gmt_hour)
    if kz:
        score += 12
        reasons.append(f"Kill Zone: {kz} (+12)")
    else:
        score -= 20
        reasons.append("Outside Kill Zone (-20) — lower probability")

    sb_bonus, sb_reason = _score_silver_bullet_bonus(gmt_hour)
    if sb_bonus:
        score += sb_bonus
        reasons.append(sb_reason)

    # ── Step 2: Liquidity ─────────────────────────────────────────────────
    if liquidity_swept:
        score += 25
        reasons.append("External liquidity swept — institutional trigger (+25)")
    else:
        reasons.append("No liquidity sweep — speculative entry (0)")

    # ── Step 3: Inefficiency ──────────────────────────────────────────────
    if has_fvg:
        score += 15
        reasons.append("FVG imbalance present (+15)")

    # ── Step 4: Structure ─────────────────────────────────────────────────
    if has_bos:
        score += 15
        reasons.append("BOS confirmed on execution TF (+15)")
    if has_ob:
        score += 10
        reasons.append("Order Block aligned (+10)")
    if d1_bias_aligned:
        score += 10
        reasons.append("D1 bias aligned with trade (+10)")

    # AMD phase
    if amd_phase == "MANIPULATION":
        score += 10
        reasons.append("AMD Manipulation phase — sweep expected (+10)")
    elif amd_phase == "DISTRIBUTION":
        score += 8
        reasons.append("AMD Distribution phase — directional move expected (+8)")

    # Session
    if session in ("LONDON", "NEW_YORK"):
        score += 5
        reasons.append(f"{session} session — peak liquidity (+5)")

    # Extras passed in (quarter, pattern bonuses, etc.)
    for extra in extras:
        if isinstance(extra, tuple) and len(extra) == 2:
            score += extra[0]
            reasons.append(extra[1])

    return min(score, 100), reasons


def scan_all_primary_symbols() -> list[ICTSetup]:
    """Scan all primary symbols and return sorted setups."""
    data = _load()
    if not data:
        return []

    primary = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]
    all_setups = []
    for sym in primary:
        try:
            setups = scan_symbol(sym, data)
            all_setups.extend(setups)
        except Exception as e:
            print(f"[ICT] Error scanning {sym}: {e}")

    all_setups.sort(key=lambda x: x.confidence, reverse=True)
    return all_setups


def get_symbol_info(symbol: str) -> dict:
    """Get symbol tick/contract info for lot sizing."""
    data = _load()
    charts = data.get("charts", {})
    return charts.get(symbol, {})
