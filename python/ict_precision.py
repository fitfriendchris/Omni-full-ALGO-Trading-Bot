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
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

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


# ── Pattern recognition model — graceful degrade if module/file absent ────────
# When trained (online_learner triggers .fit()), this model nudges per-setup
# confidence ±10 pts based on learned win probability. Capped so it can never
# override the 7-layer ICT confluence gate alone — it's a tiebreaker, not a veto.
try:
    from pattern_recognition_model import PatternRecognitionModel as _PatternModel
    _PATTERN_AVAILABLE = True
except Exception:
    _PatternModel = None  # type: ignore
    _PATTERN_AVAILABLE = False

_PATTERN_MODEL_SINGLETON = None
_PATTERN_BONUS_CAP = 10  # max ±delta the model can apply to confidence


def _get_pattern_model():
    """Lazy singleton accessor for the pattern recognition model."""
    global _PATTERN_MODEL_SINGLETON
    if not _PATTERN_AVAILABLE:
        return None
    if _PATTERN_MODEL_SINGLETON is None:
        try:
            _PATTERN_MODEL_SINGLETON = _PatternModel()
        except Exception:
            return None
    return _PATTERN_MODEL_SINGLETON


def _setup_to_pattern_features(setup) -> dict:
    """Flat numeric features for PatternRecognitionModel.predict_proba."""
    conf = getattr(setup, "confluence", None)
    feats: dict = {
        "confidence": float(getattr(setup, "confidence", 0) or 0),
        "rr_ratio":   float(getattr(setup, "rr_ratio", 0) or 0),
    }
    if conf is not None:
        # Match keys used by online_learner._compute_feature_importance
        feats.update({
            "ict_features.htf_bias_aligned":  1.0 if getattr(conf, "htf_bias_aligned",  False) else 0.0,
            "ict_features.amd_phase_aligned": 1.0 if getattr(conf, "amd_phase_aligned", False) else 0.0,
            "ict_features.killzone_active":   1.0 if getattr(conf, "killzone_active",   False) else 0.0,
            "ict_features.liquidity_swept":   1.0 if getattr(conf, "liquidity_swept",   False) else 0.0,
            "ict_features.fvg_in_ote":        1.0 if getattr(conf, "fvg_in_ote",        False) else 0.0,
            "ict_features.mss_confirmed":     1.0 if getattr(conf, "mss_confirmed",     False) else 0.0,
            "ict_features.smt_divergence":    1.0 if getattr(conf, "smt_divergence",    False) else 0.0,
            "ict_features.layers_met":        float(getattr(conf, "layers_met", 0) or 0),
        })
    feats["ict_features.liq_swept"] = 1.0 if getattr(setup, "liq_swept", False) else 0.0
    feats["structure_features.rr_ratio"] = float(getattr(setup, "rr_ratio", 0) or 0)
    return feats


def _apply_pattern_model_boost(setups: list) -> list:
    """For each setup, ask the pattern model for P(WIN) and adjust confidence.

    Delta = round((p - 0.5) * 20), clamped to ±_PATTERN_BONUS_CAP.
    The setup keeps an extra `reasons` line documenting the adjustment so the
    Telegram alert and pre-trade log show why confidence shifted.
    Returns the modified list (same objects, in place).
    """
    pm = _get_pattern_model()
    if pm is None:
        return setups
    info = pm.info() if hasattr(pm, "info") else {"loaded": False}
    if not info.get("loaded"):
        return setups
    for s in setups:
        try:
            feats = _setup_to_pattern_features(s)
            p = float(pm.predict_proba(feats))
            delta = int(round((p - 0.5) * 20))
            if delta > _PATTERN_BONUS_CAP:
                delta = _PATTERN_BONUS_CAP
            elif delta < -_PATTERN_BONUS_CAP:
                delta = -_PATTERN_BONUS_CAP
            if delta == 0:
                continue
            new_conf = max(0, min(100, int(s.confidence) + delta))
            if hasattr(s, "reasons") and isinstance(s.reasons, list):
                s.reasons.append(f"PatternModel P(WIN)={p:.2f} → conf {delta:+d}")
            s.confidence = new_conf
        except Exception:
            continue
    return setups


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
class ConfluenceScore:
    """
    7-Layer ICT Confluence Gate.
    Each layer represents one institutional filter from ICT methodology.
    Grade A+/A = high-conviction, B+/B = valid but wait for more confluence.
    """
    htf_bias_aligned:  bool = False   # L1: D1 + H4 both agree with trade direction
    amd_phase_aligned: bool = False   # L2: AMD phase supports trade (Manipulation/Distribution)
    killzone_active:   bool = False   # L3: London / NY / Silver Bullet window active
    liquidity_swept:   bool = False   # L4: external liquidity pool taken before entry
    fvg_in_ote:        bool = False   # L5: FVG or OB sits within 61.8–79% OTE (IOFED)
    mss_confirmed:     bool = False   # L6: M15 or M5 MSS / CHoCH confirmed on LTF
    smt_divergence:    bool = False   # L7: correlated pair shows SMT divergence
    ema_aligned:       bool = False   # L8: price returning to EMA20 on entry TF

    @property
    def layers_met(self) -> int:
        return sum([
            self.htf_bias_aligned, self.amd_phase_aligned, self.killzone_active,
            self.liquidity_swept,  self.fvg_in_ote,        self.mss_confirmed,
            self.smt_divergence,   self.ema_aligned,
        ])

    @property
    def grade(self) -> str:
        n = self.layers_met
        if n >= 6: return "A+"
        if n >= 5: return "A"
        if n >= 4: return "B+"
        if n >= 3: return "B"
        return "C"

    @property
    def tradeable(self) -> bool:
        """Minimum: HTF bias aligned + at least 2 other layers."""
        return self.htf_bias_aligned and self.layers_met >= 3


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
    confluence: ConfluenceScore = field(default_factory=ConfluenceScore)
    grade:      str = ""     # A+, A, B+, B, C — derived from confluence layers
    liq_swept:  bool = False # True if a liquidity sweep preceded this entry
    ema20:      float = 0.0  # EMA 20 on entry timeframe
    ema200:     float = 0.0  # EMA 200 on entry timeframe  
    ema800:     float = 0.0  # EMA 800 on entry timeframe (macro)
    ema_aligned: bool = False # Price returning to EMA20 = high-probability entry
    ema_deviation: float = 0.0 # How many ATRs price is from EMA20
    tf_emas:    dict = field(default_factory=dict)  # EMA20 for ALL timeframes


# ── Module-level data cache (keyed on file mtime) ─────────────────────────────
_DATA_CACHE: dict = {"data": {}, "mtime": 0.0, "path": ""}
_SCAN_CACHE: dict = {"result": [], "mtime": 0.0}

def _freshest_path() -> tuple[str, float]:
    """Return (path, mtime) of whichever of JSON_PATH / .tmp is newer."""
    best_path, best_mtime = JSON_PATH, 0.0
    for p in (JSON_PATH, JSON_PATH + ".tmp"):
        try:
            m = os.path.getmtime(p)
            if m > best_mtime:
                best_mtime, best_path = m, p
        except OSError:
            pass
    return best_path, best_mtime


try:
    import orjson as _orjson_ict
except ImportError:
    _orjson_ict = None  # type: ignore


def _try_parse(path: str) -> dict:
    """Attempt to read and parse a JSON file. Returns {} on any error."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if _orjson_ict is not None:
            try:
                return _orjson_ict.loads(raw)
            except Exception:
                pass
        # Fallback: trailing-comma strip + stdlib json
        text = raw.decode("utf-8", errors="replace")
        return json.loads(re.sub(r',\s*([\]}])', r'\1', text))
    except Exception:
        return {}


def _load():
    path, mtime = _freshest_path()
    # Return cached data if the file hasn't changed
    if mtime and mtime == _DATA_CACHE["mtime"]:
        return _DATA_CACHE["data"]

    # Try the freshest file first; if it's mid-write (parse error) try the other
    data = _try_parse(path)
    if not data:
        other = JSON_PATH if path.endswith(".tmp") else JSON_PATH + ".tmp"
        if os.path.exists(other):
            data = _try_parse(other)
    if not data:
        # Both failed — return last known good data
        return _DATA_CACHE["data"] or {}

    _DATA_CACHE["data"]  = data
    _DATA_CACHE["mtime"] = mtime
    _DATA_CACHE["path"]  = path
    _SCAN_CACHE["mtime"] = 0.0   # invalidate scan cache on new data
    return data


def _blended_rr(risk: float, tp1: float, tp2: float, tp3: float, entry: float) -> float:
    """
    Compute expected R-multiple weighted by partial-exit allocation:
      50% exits at TP1 (1.5R), 30% at TP2 (2.5R), 20% at TP3 (4.0R)
    Returns blended expected R so that MIN_RR checks reflect full trade value.
    """
    if risk <= 0:
        return 0.0
    r1 = abs(tp1 - entry) / risk
    r2 = abs(tp2 - entry) / risk
    r3 = abs(tp3 - entry) / risk
    return round(0.5 * r1 + 0.3 * r2 + 0.2 * r3, 2)


def calculate_amd_phase_local() -> str:
    """
    Local UTC-time fallback for AMD phase when EA data is stale/absent.

    ICT AMD (Accumulation / Manipulation / Distribution) maps to UTC sessions:
      22:00–07:00  ACCUMULATION  — Asia range building, institutions loading
      07:00–10:00  MANIPULATION  — London open, sweeps Asia high/low (Judas)
      10:00–14:00  DISTRIBUTION  — NY open, true directional delivery
      14:00–17:00  LATE_DIST     — Silver Bullet window, continuation
      17:00–22:00  OFF_HOURS     — Reduce size, cautious

    Returns one of: ACCUMULATION | MANIPULATION | DISTRIBUTION | LATE_DIST | OFF_HOURS
    """
    h = datetime.now(timezone.utc).hour
    if h >= 22 or h < 7:
        return "ACCUMULATION"
    if 7 <= h < 10:
        return "MANIPULATION"
    if 10 <= h < 14:
        return "DISTRIBUTION"
    if 14 <= h < 17:
        return "LATE_DIST"
    return "OFF_HOURS"


def detect_power_of_3(m15_bars: list) -> dict:
    """
    ICT Power of 3: Asia range → London sweeps → NY distributes.

    Phase 1 (ACCUMULATION): Asia session (22:00-07:00 UTC) builds a range.
    Phase 2 (MANIPULATION): London open (07:00-10:00 UTC) wicks above or below
                             the Asia range extreme but CLOSES back inside within 3 bars.
    Phase 3 (DISTRIBUTION): NY session (12:00+) moves away from the swept side.

    Returns dict with keys:
      po3_detected: bool
      direction:    'BUY' | 'SELL' | ''   (BUY = swept low, NY will go up)
      asia_high:    float
      asia_low:     float
      sweep_extreme: float   (the manipulation wick extreme)
      entry_bar_idx: int     (bar index of first OB/FVG after sweep closes back inside)
      confidence_bonus: int  (25 if all 3 phases confirmed, 15 if 2 phases)
      reason: str
    """
    if not m15_bars or len(m15_bars) < 24:
        return {"po3_detected": False, "direction": "", "confidence_bonus": 0, "reason": ""}

    result = {"po3_detected": False, "direction": "", "confidence_bonus": 0,
              "asia_high": 0.0, "asia_low": 0.0, "sweep_extreme": 0.0,
              "entry_bar_idx": 0, "reason": ""}

    asia_bars, london_bars, ny_bars = [], [], []
    for b in m15_bars:
        try:
            dt = datetime.fromisoformat(b.time.replace("Z", "+00:00")) if b.time else None
            if dt is None:
                continue
            h = dt.hour
            if h >= 22 or h < 7:
                asia_bars.append(b)
            elif 7 <= h < 10:
                london_bars.append(b)
            elif h >= 12:
                ny_bars.append(b)
        except Exception:
            continue

    if not asia_bars or not london_bars:
        return result

    asia_high = max(b.h for b in asia_bars)
    asia_low  = min(b.l for b in asia_bars)
    result.update({"asia_high": asia_high, "asia_low": asia_low})

    # Check London for sweep of Asia high (Judas high → BUY distribution)
    for i, b in enumerate(london_bars):
        if b.h > asia_high and b.c <= asia_high:
            # Swept above Asia high but closed back inside — SELL manipulation confirmed
            if ny_bars and ny_bars[0].c < asia_high:
                result.update({
                    "po3_detected": True, "direction": "SELL",
                    "sweep_extreme": b.h, "entry_bar_idx": i,
                    "confidence_bonus": 25,
                    "reason": f"Power of 3 SELL: Asia {asia_low:.5g}-{asia_high:.5g} → London swept {b.h:.5g} → NY distributing down",
                })
                return result

    # Check London for sweep of Asia low (Judas low → SELL manipulation → BUY distribution)
    for i, b in enumerate(london_bars):
        if b.l < asia_low and b.c >= asia_low:
            # Swept below Asia low but closed back inside — BUY distribution expected
            if ny_bars and ny_bars[0].c > asia_low:
                result.update({
                    "po3_detected": True, "direction": "BUY",
                    "sweep_extreme": b.l, "entry_bar_idx": i,
                    "confidence_bonus": 25,
                    "reason": f"Power of 3 BUY: Asia {asia_low:.5g}-{asia_high:.5g} → London swept {b.l:.5g} → NY distributing up",
                })
                return result

    return result


def _structural_sl(bars: list, entry: float, direction: str,
                   structure_edge: float) -> float:
    """
    ICT structural stop-loss placement.

    BUY  → nearest swing low below `structure_edge`, buffered by 10% of ATR.
    SELL → nearest swing high above `structure_edge`, buffered by 10% of ATR.

    Falls back to structure_edge ± 0.5×ATR when no structural level is found,
    guaranteeing a meaningful SL distance regardless of how small the FVG/OB is.
    """
    period = 14
    trs = [max(b.h - b.l, abs(b.h - bars[i+1].c), abs(b.l - bars[i+1].c))
           for i, b in enumerate(bars[:-1])]
    atr = sum(trs[:period]) / period if len(trs) >= period else (sum(trs) / len(trs) if trs else structure_edge * 0.005)
    atr_buf = atr * 0.10

    if direction == "BUY":
        # All swing lows strictly below the structure edge
        lows = sorted(
            [p for _, p in find_swing_lows(bars, lookback=2) if p < structure_edge],
            reverse=True,          # highest first (= nearest to price)
        )
        if lows:
            sl = lows[0] - atr_buf
        else:
            sl = structure_edge - max(atr * 0.5, structure_edge * 0.001)
        # Hard floor: SL must be at least 0.5×ATR below entry
        if entry - sl < atr * 0.5:
            sl = entry - max(atr * 0.5, structure_edge * 0.001)
        return sl

    else:  # SELL
        highs = sorted(
            [p for _, p in find_swing_highs(bars, lookback=2) if p > structure_edge],
        )
        if highs:
            sl = highs[0] + atr_buf
        else:
            sl = structure_edge + max(atr * 0.5, structure_edge * 0.001)
        if sl - entry < atr * 0.5:
            sl = entry + max(atr * 0.5, structure_edge * 0.001)
        return sl


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


def find_equal_highs(bars: list[Bar], tolerance_pct: float = 0.002,
                     max_bars: int = 200) -> list[float]:
    """
    Equal highs = liquidity pools (multiple touches of same level).
    Only the most recent `max_bars` bars are considered — older levels
    are irrelevant for ICT analysis and the O(n²) cost is prohibitive
    on 5000-bar arrays.
    """
    recent = bars[:max_bars]   # bars[0] is most recent
    highs = [b.h for b in recent]
    n = len(highs)
    equal_highs = []
    for i in range(n):
        h = highs[i]
        if h == 0:
            continue
        if any(j != i and abs(h - highs[j]) / h < tolerance_pct for j in range(n)):
            equal_highs.append(h)
    # Deduplicate
    result = []
    for h in sorted(set(equal_highs), reverse=True):
        if not any(abs(h - r) / h < tolerance_pct for r in result):
            result.append(h)
    return result


def find_equal_lows(bars: list[Bar], tolerance_pct: float = 0.002,
                    max_bars: int = 200) -> list[float]:
    """Equal lows = buy-side liquidity pools. Only recent `max_bars` bars."""
    recent = bars[:max_bars]
    lows = [b.l for b in recent]
    n = len(lows)
    equal_lows = []
    for i in range(n):
        l = lows[i]
        if l == 0:
            continue
        if any(j != i and abs(l - lows[j]) / l < tolerance_pct for j in range(n)):
            equal_lows.append(l)
    result = []
    for l in sorted(set(equal_lows)):
        if not any(abs(l - r) / l < tolerance_pct for r in result):
            result.append(l)
    return result


# ── Sweep Detection ───────────────────────────────────────────────────────────

def _sweep_tolerance(symbol: str = "") -> float:
    """Instrument-appropriate sweep tolerance — volatile metals/indices need wider window."""
    sym = symbol.upper()
    if any(x in sym for x in ("XAU", "XAG", "US30", "NAS", "SPX", "GER", "UK")):
        return 0.003   # 0.3% — high-volatility instruments
    if any(x in sym for x in ("JPY", "GBP")):
        return 0.0015  # 0.15% — medium-volatility pairs
    return 0.0015      # 0.15% — standard forex majors (was 0.001, now loosened)


def count_sweep_touches_high(bars: list[Bar], level: float, tolerance_pct: float = 0.0015,
                              window: int = 6) -> int:
    """Count wick penetrations above level within last `window` bars (Judas swing multi-touch)."""
    return sum(1 for b in bars[:window] if b.h > level * (1 + tolerance_pct))


def count_sweep_touches_low(bars: list[Bar], level: float, tolerance_pct: float = 0.0015,
                             window: int = 6) -> int:
    """Count wick penetrations below level within last `window` bars (Judas swing multi-touch)."""
    return sum(1 for b in bars[:window] if b.l < level * (1 - tolerance_pct))


def judas_swing_bonus(touches: int) -> int:
    """Confidence bonus based on Judas swing touch count (multi-touch = highest conviction)."""
    if touches >= 3:
        return 20   # Full Judas swing confirmed — institutional manipulation
    if touches == 2:
        return 12   # Judas swing forming — high conviction
    return 0        # Single touch — standard sweep, no extra bonus


def _has_recent_sweep(bars: list, liq_highs: list, liq_lows: list,
                      lookback: int = 25, tolerance_pct: float = 0.0015) -> tuple:
    """
    Checks whether any liquidity level was swept within the last `lookback` bars.
    Returns (swept_high, swept_low) booleans used to gate FVG/CHoCH entries.
    ICT: a stop hunt must precede any retracement entry into an imbalance.
    """
    recent = bars[:lookback]
    swept_high = False
    swept_low  = False
    # Allow close within 30% of the tolerance band above/below the level — catches
    # sweeps where price closes just a pip or two above/below after the wick.
    close_tol = tolerance_pct * 0.3
    for level in liq_highs[:6]:
        if level <= 0:
            continue
        for b in recent:
            if b.h > level * (1 + tolerance_pct) and b.c < level * (1 + close_tol):
                swept_high = True
                break
        if swept_high:
            break
    for level in liq_lows[:6]:
        if level <= 0:
            continue
        for b in recent:
            if b.l < level * (1 - tolerance_pct) and b.c > level * (1 - close_tol):
                swept_low = True
                break
        if swept_low:
            break
    return swept_high, swept_low


_chop_cooldown_ts: dict = {}   # symbol → timestamp when chop was last flagged
_CHOP_COOLDOWN_SECS = 4 * 3600  # skip FVG/CHoCH entries for 4 H1 bars after chop

# Load distribution intervals from rules.json once at import time
_dist_intervals: dict = {}
try:
    _rules_path = os.path.join(os.path.dirname(__file__), "rules.json")
    with open(_rules_path) as _rf:
        _dist_intervals = json.load(_rf).get("distribution_intervals", {})
except Exception:
    pass


def _snap_to_interval(price: float, interval: float, direction: str) -> float:
    """Snap TP to nearest institutional distribution level (round number grid)."""
    if interval <= 0:
        return price
    lower = math.floor(price / interval) * interval
    upper = lower + interval
    if direction == "BUY":
        return lower if (price - lower) < interval * 0.7 else upper
    else:
        return upper if (upper - price) < interval * 0.7 else lower


def is_market_choppy(bars: list, lookback: int = 20, chop_ratio: float = 2.5,
                     symbol: str = "") -> bool:
    """
    Returns True when H1 price is consolidating in a tight range — skip new entries.
    Condition: total H-L range across lookback bars < chop_ratio × avg bar range.
    Metals and indices are naturally more volatile so use a tighter ratio (2.0).
    """
    if not bars or len(bars) < lookback:
        return False
    # Adjust ratio for high-volatility instruments — don't flag normal gold swings as chop
    sym = symbol.upper()
    if any(x in sym for x in ("XAU", "XAG", "US30", "NAS", "SPX", "GER", "UK")):
        effective_ratio = 2.0  # metals/indices move more — require tighter range to count as chop
    else:
        effective_ratio = chop_ratio
    recent = bars[:lookback]
    total_range   = max(b.h for b in recent) - min(b.l for b in recent)
    avg_bar_range = sum(b.h - b.l for b in recent) / len(recent)
    return avg_bar_range > 0 and total_range < avg_bar_range * effective_ratio


def detect_inverted_fvg(bars: list, direction: str) -> dict:
    """
    Inverted FVG (CISD — Change in State of Delivery):
    Price closes aggressively THROUGH a prior opposing FVG, confirming directional intent.

    BUY: current bar closes above a bearish FVG → bullish intent confirmed
    SELL: current bar closes below a bullish FVG → bearish intent confirmed

    This is the 'LTF FVG close-through = institutional commitment' model.
    """
    if not bars or len(bars) < 5:
        return {"detected": False}
    current = bars[0]
    if direction == "BUY":
        for i in range(1, min(8, len(bars) - 2)):
            b_now, b_prev2 = bars[i], bars[i + 2]
            if b_now.h < b_prev2.l:  # bearish FVG exists between these bars
                fvg_low, fvg_high = b_now.h, b_prev2.l
                if current.c > fvg_high:  # closed above it → bullish intent
                    return {
                        "detected": True, "type": "INVERTED_BISI",
                        "fvg_low": fvg_low, "fvg_high": fvg_high,
                        "reason": (f"Inverted FVG: closed above bearish imbalance "
                                   f"{fvg_low:.5g}–{fvg_high:.5g} (CISD BUY)"),
                        "bonus": 18,
                    }
    elif direction == "SELL":
        for i in range(1, min(8, len(bars) - 2)):
            b_now, b_prev2 = bars[i], bars[i + 2]
            if b_now.l > b_prev2.h:  # bullish FVG exists
                fvg_low, fvg_high = b_prev2.h, b_now.l
                if current.c < fvg_low:  # closed below it → bearish intent
                    return {
                        "detected": True, "type": "INVERTED_SIBI",
                        "fvg_low": fvg_low, "fvg_high": fvg_high,
                        "reason": (f"Inverted FVG: closed below bullish imbalance "
                                   f"{fvg_low:.5g}–{fvg_high:.5g} (CISD SELL)"),
                        "bonus": 18,
                    }
    return {"detected": False}


def detect_cisd(bars: list, direction: str) -> dict:
    """
    Change in State of Delivery:
    A 3-bar same-direction delivery is engulfed by a single opposing candle
    covering 50%+ of the delivery range — signals institutional order-flow shift.
    """
    if not bars or len(bars) < 4:
        return {"detected": False}
    current = bars[0]
    prior   = bars[1:4]
    if direction == "BUY" and all(b.bearish for b in prior):
        d_range = max(b.h for b in prior) - min(b.l for b in prior)
        if current.bullish and d_range > 0 and current.body >= d_range * 0.5:
            return {"detected": True, "type": "CISD_BUY",
                    "reason": "CISD: Bullish candle engulfed 3-bar bearish delivery", "bonus": 15}
    elif direction == "SELL" and all(b.bullish for b in prior):
        d_range = max(b.h for b in prior) - min(b.l for b in prior)
        if current.bearish and d_range > 0 and current.body >= d_range * 0.5:
            return {"detected": True, "type": "CISD_SELL",
                    "reason": "CISD: Bearish candle engulfed 3-bar bullish delivery", "bonus": 15}
    return {"detected": False}


def detect_sweep_high(bars: list[Bar], level: float, tolerance_pct: float = 0.001) -> bool:
    """
    Bullish sweep then reversal:
    Any bar in the window wicked above level and that bar (or a later bar) closed back below.
    bars[0] = most recent. Searches full window so sweeps from up to 8 bars ago are valid.
    """
    if len(bars) < 3:
        return False
    threshold = level * (1 + tolerance_pct)
    for i, b in enumerate(bars):
        if b.h > threshold:
            # Sweep bar found at index i — check if it or any more-recent bar (j < i) closed below
            if b.c < level:
                return True
            for j in range(i):
                if bars[j].c < level:
                    return True
    return False


def detect_sweep_low(bars: list[Bar], level: float, tolerance_pct: float = 0.001) -> bool:
    """
    Bearish sweep then reversal:
    Any bar wicked below level and that bar (or a later bar) closed back above.
    bars[0] = most recent. Searches full window for sweeps up to 8 bars old.
    """
    if len(bars) < 3:
        return False
    threshold = level * (1 - tolerance_pct)
    for i, b in enumerate(bars):
        if b.l < threshold:
            if b.c > level:
                return True
            for j in range(i):
                if bars[j].c > level:
                    return True
    return False


def detect_turtle_soup(bars: list, level: float, direction: str,
                       lookback: int = 4, min_break_atr_pct: float = 0.25) -> bool:
    """
    Turtle Soup (ICT stop-run reversal):
    Price breaks sharply through a swing level — triggering retail turtle breakout traders —
    then closes back inside within 1-3 bars. Classic liquidity grab signature.

    direction='SELL': price broke above `level` then closed back below (bearish reversal).
    direction='BUY':  price broke below `level` then closed back above (bullish reversal).

    min_break_atr_pct: minimum break distance as % of ATR to qualify as "sharp".
    Returns True if turtle soup pattern is present in recent bars.
    """
    if len(bars) < lookback + 1:
        return False
    atr_val = _calc_atr(bars[:lookback + 5])
    min_break = atr_val * min_break_atr_pct if atr_val > 0 else level * 0.001

    recent = bars[:lookback]
    if direction == "SELL":
        # Look for a bar that broke above level by at least min_break, then closed back below
        for i, b in enumerate(recent[1:], 1):
            if b.h > level + min_break:           # sharp break above level
                # Check if subsequent bar(s) closed back below the level
                for j in range(i):
                    if recent[j].c < level:
                        return True
    else:  # BUY
        for i, b in enumerate(recent[1:], 1):
            if b.l < level - min_break:           # sharp break below level
                for j in range(i):
                    if recent[j].c > level:
                        return True
    return False


# ── Order Block Finder ────────────────────────────────────────────────────────

def _calc_atr(bars: list, period: int = 14) -> float:
    """Average True Range over `period` bars. bars[0] = most recent."""
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(min(period, len(bars) - 1)):
        tr = max(bars[i].h, bars[i + 1].c) - min(bars[i].l, bars[i + 1].c)
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def _calc_ema(values: list[float], period: int) -> list[float]:
    """Compute EMA for a list of closing prices. Returns list same length as input."""
    if len(values) < period:
        return values[:]  # Not enough data, return as-is
    k = 2.0 / (period + 1)
    emas = [0.0] * len(values)
    # First EMA = SMA of first `period` values
    emas[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        emas[i] = values[i] * k + emas[i - 1] * (1 - k)
    return emas


def _ema_from_bars(bars: list, period: int = 20) -> float:
    """Most recent EMA value from bars (bars[0] = most recent).
    Handles both ict_precision.Bar (.c) and smc_engine.Bar (.close)."""
    if len(bars) < period:
        return 0.0
    # Polymorphic close extraction
    if hasattr(bars[0], 'c'):
        closes = [b.c for b in bars]
    elif hasattr(bars[0], 'close'):
        closes = [b.close for b in bars]
    elif isinstance(bars[0], dict):
        closes = [b.get('c', b.get('close', 0)) for b in bars]
    else:
        closes = [0.0] * len(bars)
    emas = _calc_ema(closes, period)
    return emas[-1] if emas else 0.0


def get_ema_levels(bars: list) -> dict:
    """
    Return EMA 20, 200, 800 for any bar list.
    Key insight: price always returns to EMA 20 on all timeframes.
    Use for: entry validation, dynamic SL adjustment, confluence scoring.
    """
    result = {"ema_20": 0.0, "ema_200": 0.0, "ema_800": 0.0}
    if len(bars) < 20:
        return result
    closes = [b.c for b in bars]
    result["ema_20"] = _calc_ema(closes, 20)[-1]
    if len(bars) >= 200:
        result["ema_200"] = _calc_ema(closes, 200)[-1]
    if len(bars) >= 800:
        result["ema_800"] = _calc_ema(closes, 800)[-1]
    return result


def price_vs_ema20(bars: list) -> dict:
    """
    Returns relationship of current price to EMA 20.
    Handles both ict_precision.Bar (o,h,l,c) and smc_engine.Bar (open,high,low,close).
    """
    if len(bars) < 20:
        return {"ema20": 0.0, "dist_pct": 0.0, "above": False, "deviation": 0.0}
    ema20 = _ema_from_bars(bars, 20)
    # Handle both bar formats
    if hasattr(bars[0], 'c'):
        current = bars[0].c  # ict_precision.Bar
    elif hasattr(bars[0], 'close'):
        current = bars[0].close  # smc_engine.Bar
    elif isinstance(bars[0], dict):
        current = bars[0].get('c', bars[0].get('close', 0))
    else:
        current = 0.0
    dist_pct = abs(current - ema20) / ema20 * 100 if ema20 > 0 else 0.0
    above = current > ema20
    # ATR-based deviation: how many ATRs is price from EMA20?
    atr = _calc_atr(bars, 14)
    deviation = abs(current - ema20) / atr if atr > 0 else 0.0
    return {
        "ema20": round(ema20, 5),
        "current": round(current, 5),
        "dist_pct": round(dist_pct, 3),
        "above": above,
        "deviation": round(deviation, 2),
    }


def find_bullish_ob(bars: list[Bar], start: int = 0, search: int = 15) -> Optional[tuple[float, float]]:
    """
    Bullish OB: last bearish candle before a strong bullish impulse.
    Excludes high-volatility bars (range >= 2×ATR) — LuxAlgo parsed H/L concept:
    news/spike bars are not institutional OBs.
    """
    atr_val = _calc_atr(bars[:search + 5])
    for i in range(start, min(start + search, len(bars) - 2)):
        b = bars[i]
        b_next = bars[i + 1] if i + 1 < len(bars) else None
        if b_next is None:
            continue
        # Skip volatility spike bars — not institutional positioning
        if atr_val > 0 and (b.h - b.l) >= 2 * atr_val:
            continue
        if b.bearish and b_next.bullish:
            impulse = b_next.body / (b.range + 0.0001)
            # LuxAlgo confluence filter: impulse candle must be body-dominated
            b_next_body_pct = b_next.body / (b_next.range + 0.0001)
            if impulse > 0.6 and b_next_body_pct > 0.4:
                return (b.l, b.h)
    return None


def find_bearish_ob(bars: list[Bar], start: int = 0, search: int = 15) -> Optional[tuple[float, float]]:
    """
    Bearish OB: last bullish candle before a strong bearish impulse.
    Excludes high-volatility bars — LuxAlgo parsed H/L concept.
    """
    atr_val = _calc_atr(bars[:search + 5])
    for i in range(start, min(start + search, len(bars) - 2)):
        b = bars[i]
        b_next = bars[i + 1] if i + 1 < len(bars) else None
        if b_next is None:
            continue
        if atr_val > 0 and (b.h - b.l) >= 2 * atr_val:
            continue
        if b.bullish and b_next.bearish:
            impulse = b_next.body / (b.range + 0.0001)
            b_next_body_pct = b_next.body / (b_next.range + 0.0001)
            if impulse > 0.6 and b_next_body_pct > 0.4:
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
    """D1 trend direction using HH/HL vs LH/LL swing structure (ICT correct)."""
    if len(bars) < 6:
        return "NEUTRAL"

    recent = bars[:30]
    swing_highs = find_swing_highs(recent, lookback=2)
    swing_lows  = find_swing_lows(recent, lookback=2)

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        # bars[0]=newest → swing_highs[0] = most recent swing high (smallest index)
        h1, h2 = swing_highs[0][1], swing_highs[1][1]
        l1, l2 = swing_lows[0][1],  swing_lows[1][1]
        hh = h1 > h2   # Higher High
        hl = l1 > l2   # Higher Low
        lh = h1 < h2   # Lower High
        ll = l1 < l2   # Lower Low
        if hh and hl:
            return "BULLISH"
        if lh and ll:
            return "BEARISH"

    # Fallback: 3-swing momentum check
    if len(swing_highs) >= 2:
        if swing_highs[0][1] > swing_highs[1][1]:
            return "BULLISH"
        if swing_highs[0][1] < swing_highs[1][1]:
            return "BEARISH"

    # Final fallback: close momentum over last 10 bars
    if len(bars) >= 10:
        if bars[0].c > bars[9].c * 1.001:
            return "BULLISH"
        if bars[0].c < bars[9].c * 0.999:
            return "BEARISH"
    return "NEUTRAL"


def get_h4_structure(bars: list[Bar]) -> str:
    """H4 market structure: BOS (trend continuation) + CHoCH (trend reversal)."""
    if len(bars) < 10:
        return "RANGING"
    swing_highs = find_swing_highs(bars, lookback=2)
    swing_lows  = find_swing_lows(bars, lookback=2)
    if not swing_highs or not swing_lows:
        return "RANGING"

    current = bars[0].c
    # Most recent swings (smallest index = most recent bar)
    last_sh_idx, last_sh = swing_highs[0]
    last_sl_idx, last_sl = swing_lows[0]

    # BOS: break of previous swing high/low in the direction of the current trend
    prev_sh = swing_highs[1][1] if len(swing_highs) > 1 else last_sh
    prev_sl = swing_lows[1][1]  if len(swing_lows)  > 1 else last_sl

    # BOS Bullish: current price broke above previous swing high (trend continuation up)
    if current > prev_sh and (last_sl > swing_lows[1][1] if len(swing_lows) > 1 else True):
        return "BOS_BULLISH"
    # BOS Bearish: current price broke below previous swing low (trend continuation down)
    if current < prev_sl and (last_sh < swing_highs[1][1] if len(swing_highs) > 1 else True):
        return "BOS_BEARISH"

    # CHoCH: Change of Character — prior swing broken against the prevailing structure
    # CHoCH Bullish: bearish structure broken — price broke ABOVE the last swing high
    if last_sl_idx < last_sh_idx and current > last_sh:
        return "CHOCH_BULL"
    # CHoCH Bearish: bullish structure broken — price broke BELOW the last swing low
    if last_sh_idx < last_sl_idx and current < last_sl:
        return "CHOCH_BEAR"

    # Simple BOS fallback
    if current > last_sh:
        return "BOS_BULLISH"
    if current < last_sl:
        return "BOS_BEARISH"
    return "RANGING"


# ── LTF Entry Trigger (M15 / M5 / M1 confirmation) ───────────────────────────

def detect_mss(bars: list[Bar], direction: str, lookback: int = 8) -> bool:
    """
    Market Structure Shift on a lower timeframe.
    BUY  MSS: price made a LL, then broke above the last swing high (CHoCH up).
    SELL MSS: price made a HH, then broke below the last swing low  (CHoCH down).
    """
    if len(bars) < lookback + 2:
        return False
    recent = bars[:lookback]
    if direction == "BUY":
        # BUY MSS: price made a LL then broke above the last LH that formed BEFORE the LL.
        # bars[0]=newest, higher index=older. OLDER bars have HIGHER index.
        lows  = find_swing_lows(recent,  lookback=2)
        highs = find_swing_highs(recent, lookback=2)
        if not lows or not highs:
            return False
        last_low_idx = lows[0][0]   # most recent swing low index
        # We need the swing high that is OLDER than the low (higher index = older bar)
        highs_before_low = [(i, p) for i, p in highs if i > last_low_idx]
        if not highs_before_low:
            return False
        pivot_high = highs_before_low[0][1]   # the Lower High preceding the Lower Low
        return recent[0].c > pivot_high   # CHoCH up: broke above the preceding LH

    else:  # SELL
        # SELL MSS: price made a HH then broke below the last HL that formed BEFORE the HH.
        highs = find_swing_highs(recent, lookback=2)
        lows  = find_swing_lows(recent,  lookback=2)
        if not highs or not lows:
            return False
        last_high_idx = highs[0][0]   # most recent swing high index
        # We need the swing low that is OLDER than the high (higher index = older bar)
        lows_before_high = [(i, p) for i, p in lows if i > last_high_idx]
        if not lows_before_high:
            return False
        pivot_low = lows_before_high[0][1]   # the Higher Low preceding the Higher High
        return recent[0].c < pivot_low   # CHoCH down: broke below the preceding HL


def get_ltf_entry(
    m15_bars: list[Bar],
    m5_bars:  list[Bar],
    m1_bars:  list[Bar],
    direction: str,
    zone_low:  float,
    zone_high: float,
    h1_sl:     float,
) -> dict:
    """
    Look for a precision entry trigger on M15 → M5 → M1 within an H1 zone.

    Flow:
        1. Is current price at or inside the H1 zone?  (zone proximity check)
        2. Is there a M15 MSS in the direction?         (structure shift)
        3. Is there a M15 or M5 FVG inside / near the zone in the direction?
        4. Is there an M1 MSS for final confirmation?  (optional, boosts conf)

    Returns dict:
        confirmed    : bool — at least M15 MSS + FVG found
        entry        : float — LTF FVG CE (or zone midpoint fallback)
        sl           : float — below LTF FVG / structural level
        trigger_tf   : str   — "M15" | "M5" | "M1"
        m15_mss      : bool
        m5_fvg       : bool
        m1_mss       : bool
        reason       : str
    """
    result = {
        "confirmed": False, "entry": 0.0, "sl": h1_sl,
        "trigger_tf": "", "m15_mss": False, "m5_fvg": False,
        "m1_mss": False, "reason": "",
    }

    if not m15_bars:
        return result

    current = m15_bars[0].c
    zone_mid = (zone_low + zone_high) / 2
    zone_range = max(zone_high - zone_low, (zone_high * 0.0005))   # at least 0.05% of price

    # ── 1. Zone proximity — price must be within 3× zone range of the zone ──
    proximity = 3.0
    if direction == "BUY":
        in_zone = zone_low - zone_range * proximity <= current <= zone_high + zone_range * proximity
    else:
        in_zone = zone_low - zone_range * proximity <= current <= zone_high + zone_range * proximity

    if not in_zone:
        result["reason"] = f"Price {current:.5g} not near zone {zone_low:.5g}–{zone_high:.5g}"
        return result

    # ── 2. M15 MSS ──────────────────────────────────────────────────────────
    m15_mss = detect_mss(m15_bars, direction, lookback=10)
    result["m15_mss"] = m15_mss

    # ── 3. M15 FVG in direction near zone ───────────────────────────────────
    m15_fvg_entry = None
    m15_fvg_sl    = h1_sl
    if direction == "BUY":
        fvg = find_bullish_fvg(m15_bars[:12])
        if fvg:
            fl, fh = fvg
            if fl <= zone_high + zone_range:   # FVG is near or inside zone
                m15_fvg_entry = fl + (fh - fl) * 0.5
                m15_fvg_sl    = _structural_sl(m15_bars, m15_fvg_entry, "BUY", fl)
    else:
        fvg = find_bearish_fvg(m15_bars[:12])
        if fvg:
            fl, fh = fvg
            if fh >= zone_low - zone_range:
                m15_fvg_entry = fh - (fh - fl) * 0.5
                m15_fvg_sl    = _structural_sl(m15_bars, m15_fvg_entry, "SELL", fh)

    # ── 4. M5 FVG (tighter entry) ────────────────────────────────────────────
    m5_fvg_entry = None
    m5_fvg_sl    = h1_sl
    if m5_bars:
        result["m5_fvg"] = False
        if direction == "BUY":
            fvg5 = find_bullish_fvg(m5_bars[:15])
            if fvg5:
                fl, fh = fvg5
                if fl <= zone_high + zone_range:
                    m5_fvg_entry = fl + (fh - fl) * 0.5
                    m5_fvg_sl    = _structural_sl(m5_bars, m5_fvg_entry, "BUY", fl)
                    result["m5_fvg"] = True
        else:
            fvg5 = find_bearish_fvg(m5_bars[:15])
            if fvg5:
                fl, fh = fvg5
                if fh >= zone_low - zone_range:
                    m5_fvg_entry = fh - (fh - fl) * 0.5
                    m5_fvg_sl    = _structural_sl(m5_bars, m5_fvg_entry, "SELL", fh)
                    result["m5_fvg"] = True

    # ── 5. M1 MSS (optional final trigger) ──────────────────────────────────
    m1_mss = detect_mss(m1_bars, direction, lookback=8) if m1_bars else False
    result["m1_mss"] = m1_mss

    # ── Decision: use tightest confirmed entry ───────────────────────────────
    # Prefer M5 FVG + M15 MSS, fall back to M15 FVG, fall back to zone midpoint
    if m5_fvg_entry and (m15_mss or m1_mss):
        result.update({
            "confirmed":  True,
            "entry":      round(m5_fvg_entry, 5),
            "sl":         round(m5_fvg_sl, 5),
            "trigger_tf": "M5",
            "reason":     f"M5 FVG + {'M15' if m15_mss else 'M1'} MSS trigger",
        })
    elif m15_fvg_entry and m15_mss:
        result.update({
            "confirmed":  True,
            "entry":      round(m15_fvg_entry, 5),
            "sl":         round(m15_fvg_sl, 5),
            "trigger_tf": "M15",
            "reason":     "M15 FVG + M15 MSS trigger",
        })
    elif m15_fvg_entry and m1_mss:
        result.update({
            "confirmed":  True,
            "entry":      round(m15_fvg_entry, 5),
            "sl":         round(m15_fvg_sl, 5),
            "trigger_tf": "M15",
            "reason":     "M15 FVG + M1 MSS trigger",
        })
    elif m15_mss and (m15_fvg_entry or m5_fvg_entry):
        # MSS confirmed + FVG present
        e = m5_fvg_entry or m15_fvg_entry
        s = m5_fvg_sl    if m5_fvg_entry else m15_fvg_sl
        result.update({
            "confirmed":  True,
            "entry":      round(e, 5),
            "sl":         round(s, 5),
            "trigger_tf": "M5" if m5_fvg_entry else "M15",
            "reason":     f"{'M5' if m5_fvg_entry else 'M15'} FVG + M15 MSS",
        })
    else:
        # No LTF confirmation — zone is valid but not triggered yet
        result["reason"] = (
            f"Zone valid, waiting: MSS={'Y' if m15_mss else 'N'} "
            f"M15FVG={'Y' if m15_fvg_entry else 'N'} "
            f"M5FVG={'Y' if m5_fvg_entry else 'N'}"
        )

    return result


def _pivot_tag_levels(reasons: list, d1_piv: dict, w1_piv: dict,
                      entry: float, tp1: float, tp2: float, direction: str) -> None:
    """Append a note to reasons when a TP target is near a pivot level."""
    all_pivs = {**d1_piv, **{f"W1_{k}": v for k, v in w1_piv.items()}}
    tol = entry * 0.001  # 0.1% tolerance
    for tp_label, tp_val in [("TP1", tp1), ("TP2", tp2)]:
        for label, pval in all_pivs.items():
            if pval > 0 and abs(tp_val - pval) <= tol:
                period = "W1" if label.startswith("W1_") else "D1"
                key = label.replace("W1_", "")
                reasons.append(f"{tp_label} aligns with {period} pivot {key.upper()} @ {pval:.5g}")
                break


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
    amd_phase = data.get("amd_phase", "") or ""
    # Use local UTC fallback when EA hasn't provided AMD phase (stale data / offline)
    if not amd_phase or amd_phase in ("—", "UNKNOWN", ""):
        amd_phase = calculate_amd_phase_local()

    # ── Parse bars — full timeframe stack ───────────────────────────
    # W1/D1/H4 → macro bias & trend direction
    # H1       → setup zone (OB / FVG / Breaker)
    # M15/M5   → entry trigger (MSS + FVG within zone)
    # M1       → execution confirmation
    w1_bars  = _parse_bars(sym_data.get("W1",  []))
    d1_bars  = _parse_bars(sym_data.get("D1",  []))
    h4_bars  = _parse_bars(sym_data.get("H4",  []))
    h1_bars  = _parse_bars(sym_data.get("H1",  []))
    m15_bars = _parse_bars(sym_data.get("M15", []))
    m5_bars  = _parse_bars(sym_data.get("M5",  []))
    m1_bars  = _parse_bars(sym_data.get("M1",  []))

    if not d1_bars or not h4_bars or not h1_bars:
        return []

    # ── Compute EMA levels across all timeframes ─────────────────────
    # EMA 20, 200, 800 — price always returns to EMA20 on all TFs
    w1_ema  = get_ema_levels(w1_bars)  if w1_bars  else {}
    d1_ema  = get_ema_levels(d1_bars)  if d1_bars  else {}
    h4_ema  = get_ema_levels(h4_bars)  if h4_bars  else {}
    h1_ema  = get_ema_levels(h1_bars)  if h1_bars  else {}
    m15_ema = get_ema_levels(m15_bars) if m15_bars else {}
    m5_ema  = get_ema_levels(m5_bars)  if m5_bars  else {}

    # Current price deviation from EMA20 on each timeframe
    w1_vs_ema20  = price_vs_ema20(w1_bars)  if w1_bars  else {}
    d1_vs_ema20  = price_vs_ema20(d1_bars)  if d1_bars  else {}
    h4_vs_ema20  = price_vs_ema20(h4_bars)  if h4_bars  else {}
    h1_vs_ema20  = price_vs_ema20(h1_bars)  if h1_bars  else {}
    m15_vs_ema20 = price_vs_ema20(m15_bars) if m15_bars else {}
    m5_vs_ema20  = price_vs_ema20(m5_bars)  if m5_bars  else {}

    # Key insight for entry: if price is >2 ATR from EMA20, it's "stretched"
    # and likely to return. This is a high-confluence entry signal.
    m5_ema20_dev = m5_vs_ema20.get("deviation", 0) if m5_vs_ema20 else 0
    h1_ema20_dev = h1_vs_ema20.get("deviation", 0) if h1_vs_ema20 else 0
    price_stretched = (m5_ema20_dev > 2.0) or (h1_ema20_dev > 2.0)

    # EMA alignment for confluence scoring
    # If D1 price > EMA20 AND H4 price > EMA20 = bullish trend
    # If D1 price < EMA20 AND H4 price < EMA20 = bearish trend
    ema_trend_aligned = False
    if d1_vs_ema20 and h4_vs_ema20:
        d1_above = d1_vs_ema20.get("above", False)
        h4_above = h4_vs_ema20.get("above", False)
        if d1_above and h4_above:
            ema_trend_aligned = True  # Both TFs bullish
        elif not d1_above and not h4_above:
            ema_trend_aligned = True  # Both TFs bearish

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

    # ── Step 0: W1 Macro Bias ────────────────────────────────────────
    # Weekly context — 3 HTF timeframes aligned is strongest confluence
    w1_bias = get_d1_bias(w1_bars) if w1_bars else "NEUTRAL"

    # ── Step 1: D1 Bias ─────────────────────────────────────────────
    d1_bias = get_d1_bias(d1_bars)

    # ── Step 2: H4 Structure ────────────────────────────────────────
    h4_struct = get_h4_structure(h4_bars)

    # ── Step 2b: Quarter Theory (Daily Range Position) ──────────────
    d1_high = max((b.h for b in d1_bars[:5]), default=0)
    d1_low  = min((b.l for b in d1_bars[:5] if b.l > 0), default=0)
    quarter_info = get_quarter_position(current_price, d1_high, d1_low)

    # ── Step 2c: Technical Pattern Detection (all patterns) ──────────
    detected_patterns = detect_all_patterns(h1_bars, short_bars=m15_bars)

    # ── Step 2d: Push/Exhaustion Phase ──────────────────────────────
    push_exh_h1  = detect_push_exhaustion(h1_bars)
    push_exh_m15 = detect_push_exhaustion(m15_bars) if m15_bars else {"phase": "NEUTRAL"}

    # Use the more specific M15 reading if available
    push_exh = push_exh_m15 if push_exh_m15["phase"] != "NEUTRAL" else push_exh_h1

    # ── Step 2e: Support / Resistance Analysis ───────────────────────
    sr_info_h1  = find_key_levels(h1_bars, symbol=symbol)
    sr_info_h4  = find_key_levels(h4_bars, tolerance_pct=0.003, symbol=symbol)

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

    # Daily pivot points from previous day's D1 bar
    d1_pivots: dict = {}
    if len(d1_bars) >= 2:
        prev = d1_bars[1]  # prior closed day
        d1_pivots = calculate_pivot_points(prev.h, prev.l, prev.c)

    # Weekly pivot points from previous week's W1 bar
    w1_pivots: dict = {}
    if w1_bars and len(w1_bars) >= 2:
        prev_w = w1_bars[1]
        w1_pivots = calculate_pivot_points(prev_w.h, prev_w.l, prev_w.c)

    # Pivot resistance levels (above price) → add to liq_highs
    pivot_highs = [v for k, v in {**d1_pivots, **w1_pivots}.items()
                   if k in ("r1","r2","r3","fib_r1","fib_r2","fib_r3","pp") and v > 0]
    # Pivot support levels (below price) → add to liq_lows
    pivot_lows  = [v for k, v in {**d1_pivots, **w1_pivots}.items()
                   if k in ("s1","s2","s3","fib_s1","fib_s2","fib_s3","pp") and v > 0]

    liq_highs = sorted(set(h4_eq_highs + h1_eq_highs + [pdh, pwh, pmh] + gap_highs + pivot_highs), reverse=True)
    liq_lows  = sorted(set(h4_eq_lows  + h1_eq_lows  + [pdl, pwl, pml] + gap_lows  + pivot_lows))
    liq_highs = [l for l in liq_highs if l > current_price * 0.99]
    liq_lows  = [l for l in liq_lows  if l < current_price * 1.01 and l > 0]

    # EQH/EQL dict passed to scoring functions for draw-on-liquidity bonus
    # Filter to levels above (for BUY) or below (for SELL) current price
    eqh_above = [l for l in h4_eq_highs + h1_eq_highs if l > current_price]
    eql_below  = [l for l in h4_eq_lows  + h1_eq_lows  if l < current_price and l > 0]
    eqh_eql_info = {
        "eqh": sorted(eqh_above)[:3],   # nearest equal highs above
        "eql": sorted(eql_below, reverse=True)[:3],  # nearest equal lows below
    }

    # Prices dict for SMT check — from the raw JSON prices list
    prices_list = data.get("prices", [])
    prices_dict_all = {p["symbol"]: p for p in prices_list}

    # ── Precompute sweep context & chop state ────────────────────────
    # Used to gate FVG/CHoCH entries: ICT requires a stop hunt (sweep)
    # to have occurred BEFORE entering from an imbalance zone.
    _sweep_bars_gate = m15_bars if m15_bars else h1_bars
    _sweep_tol_gate  = _sweep_tolerance(symbol)
    if _sweep_bars_gate:
        _recently_swept_high, _recently_swept_low = _has_recent_sweep(
            _sweep_bars_gate, liq_highs, liq_lows,
            lookback=25, tolerance_pct=_sweep_tol_gate,
        )
    else:
        _recently_swept_high = _recently_swept_low = False

    # Chop filter: suppress new setups when H1 is range-bound
    _now = time.time()
    _h1_choppy = is_market_choppy(h1_bars, lookback=20, chop_ratio=2.5, symbol=symbol)
    if _h1_choppy:
        _chop_cooldown_ts[symbol] = _now
    elif symbol in _chop_cooldown_ts and _now - _chop_cooldown_ts[symbol] < _CHOP_COOLDOWN_SECS:
        _h1_choppy = True  # Within 4-bar cooldown after chop — still suppress FVG/CHoCH

    # ── Step 4: Sweep Detection on M15/H1 ───────────────────────────
    _atr_local = _calc_atr(h1_bars[-20:], period=14) if h1_bars else 0.0

    # BEARISH SETUP: Sweep of high → SELL from OB below sweep
    for level in liq_highs[:5]:  # Check top 5 liquidity highs
        sweep_bars = m15_bars if m15_bars else h1_bars
        tol = _sweep_tolerance(symbol)
        if detect_sweep_high(sweep_bars, level, tolerance_pct=tol):
            # Displacement check: last bar must show bearish conviction, not a doji/inside bar
            _sweep_last = sweep_bars[-1] if sweep_bars else None
            if _sweep_last and _sweep_last.range > 0 and (_sweep_last.body / _sweep_last.range) < 0.28:
                continue  # Spinning top after sweep = no institutional follow-through

            sweep_touches = count_sweep_touches_high(sweep_bars, level, tolerance_pct=tol)
            # Find bearish OB to sell from
            ob = find_bearish_ob(h1_bars, start=0, search=10)
            if not ob:
                ob = find_bearish_ob(m15_bars, start=0, search=8) if m15_bars else None

            if ob:
                ob_low, ob_high = ob

                # Proximity gate: OB must be close to swept level — stale OBs don't hold
                if _atr_local > 0 and abs(ob_high - level) > _atr_local * 2:
                    continue

                # ICT AMD / Market Maker Model — ENTER AT MANIPULATION WICK EXTREME
                # When price sweeps liquidity and forms an OB, the optimal entry
                # is the manipulation wick extreme (the stop-run spike), NOT the
                # OB midpoint. The SL goes just beyond the wick — if price comes back
                # past the manipulation, the setup is invalidated.
                sweep_extreme_high = max((b.h for b in sweep_bars[:4]), default=level * 1.002)
                entry = sweep_extreme_high  # Enter at manipulation wick extreme
                
                # SL: just above the manipulation wick — if price retraces past here,
                # the sweep was NOT a true liquidity grab (stop hunt failed)
                sl_buffer = max(_atr_local * 0.5, (sweep_extreme_high - level) * 0.2)
                sl = sweep_extreme_high + sl_buffer
                # Cap SL at 1.5× ATR to prevent runaway risk
                if _atr_local > 0:
                    sl = min(sl, entry + _atr_local * 1.5)
                
                risk = sl - entry
                if risk <= 0:
                    continue
                tp1 = entry - risk * 1.5
                tp2 = entry - risk * 2.5
                tp3 = entry - risk * 4.0

                # Override TP1 with nearest liquidity level if within 3R (avoids far targets)
                for liq_l in liq_lows:
                    if liq_l < entry - risk * 0.5 and liq_l > entry - risk * 3.0:
                        tp1 = min(tp1, liq_l + (entry - liq_l) * 0.1)
                        break

                confidence, extra_reasons = _score_sell_setup_full(
                    d1_bias, h4_struct, amd_phase, session, level, pdh, pwh,
                    quarter_info=quarter_info,
                    patterns=detected_patterns,
                    sr_info=sr_info_h1,
                    push_exh=push_exh,
                    eqh_eql=eqh_eql_info,
                )
                # W1 alignment bonus/penalty
                if w1_bias == d1_bias and w1_bias != "NEUTRAL":
                    confidence = min(100, confidence + 15)
                elif w1_bias not in ("NEUTRAL", d1_bias) and w1_bias != "NEUTRAL":
                    confidence = max(0, confidence - 10)

                # LuxAlgo Strong High: sweeping with-trend institutional resistance = A+ signal
                if d1_bias == "BEARISH" and level in liq_highs[:3]:
                    confidence = min(100, confidence + 8)

                # Turtle Soup: sharp break above level + close back below = stop-run confirmed
                _turtle = detect_turtle_soup(m15_bars or h1_bars, level, "SELL")
                if _turtle:
                    confidence = min(100, confidence + 12)

                # Inducement: swept level is a known equal high = institutional stop hunt confirmed
                _is_inducement = level in h1_eq_highs or level in h4_eq_highs
                if _is_inducement:
                    confidence = min(100, confidence + 10)

                # Judas swing multi-touch bonus
                j_bonus = judas_swing_bonus(sweep_touches)
                if j_bonus:
                    confidence = min(100, confidence + j_bonus)

                # OTE precision: high-confidence setups get deepest entry (tighter SL, better RR)
                if confidence >= 70:
                    _deep_entry = ob_prec["ote_79"]
                    _deep_risk  = sl - _deep_entry
                    if _deep_risk > 0:
                        entry = _deep_entry
                        risk  = _deep_risk
                        tp1   = entry - risk * 1.5
                        tp2   = entry - risk * 2.5
                        tp3   = entry - risk * 4.0

                # Snap TPs to institutional distribution grid
                _dist = _dist_intervals.get(symbol, _dist_intervals.get("default", 0.0))
                if _dist > 0:
                    tp1 = _snap_to_interval(tp1, _dist, "SELL")
                    tp2 = _snap_to_interval(tp2, _dist, "SELL")

                # RR guard: TP snapping must not collapse RR below 1.3:1
                if abs(tp1 - entry) < risk * 1.3:
                    continue

                # Multi-confluence gate: bare sweep alone is never enough
                # Need at least 1 of: turtle soup, inducement, D1 alignment, W1 alignment
                _sell_confirmers = sum([
                    bool(_turtle), _is_inducement,
                    d1_bias == "BEARISH", w1_bias == "BEARISH",
                ])
                if _sell_confirmers == 0:
                    continue

                if confidence >= 60:
                    pattern_names = [p.get("pattern","") for p in detected_patterns if p.get("direction") == "SELL"]
                    judas_tag = "" if sweep_touches < 2 else f" | Judas {sweep_touches}-touch sweep"
                    reasons = [
                        f"Liquidity sweep of {level:.5g}{judas_tag} — stop hunt confirmed",
                        f"Bearish OB: {ob_low:.5g}—{ob_high:.5g} | Precision entry @ {entry:.5g} (OTE 50%)",
                        f"W1={w1_bias} | D1 Bias: {d1_bias} | H4: {h4_struct}",
                        f"AMD Phase: {amd_phase} | Session: {session}",
                        f"Quarter: {quarter_info['quarter']} ({quarter_info['pct']:.0f}% of daily range)",
                    ]
                    if pattern_names:
                        reasons.append(f"Patterns confirmed: {', '.join(pattern_names)}")
                    if push_exh.get("signal"):
                        reasons.append(f"Momentum: {push_exh['signal']}")
                    if sr_info_h1.get("phase_at_resistance"):
                        reasons.append(f"S/R: {sr_info_h1['phase_at_resistance']}")
                    if _is_inducement:
                        reasons.append("Inducement confirmed — swept level is equal high liquidity pool")
                    reasons.extend(extra_reasons)
                    if level == pdh:
                        reasons.insert(0, "Previous Day High swept — HIGH PRIORITY")
                    elif level == pwh:
                        reasons.insert(0, "Previous Week High swept — HIGH PRIORITY")
                    elif level == pmh:
                        reasons.insert(0, "Previous Month High swept")

                    # Tag nearest pivot levels if within TP range
                    _pivot_tag_levels(reasons, d1_pivots, w1_pivots, entry, tp1, tp2, "SELL")

                    rr = _blended_rr(abs(sl - entry), tp1, tp2, tp3, entry)
                    cs = _build_confluence(
                        symbol, "SELL", d1_bias, h4_struct, amd_phase,
                        gmt_hour, gmt_min, "SWEEP_HIGH_OB",
                        liq_swept=True, h4_eq_highs=h4_eq_highs, h4_eq_lows=h4_eq_lows,
                        current_price=current_price, fvg_low=ob_low, fvg_high=ob_high,
                        h1_bars=h1_bars, ltf_confirmed=False, prices_dict=prices_dict_all,
                    )
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
                        confluence=cs, grade=cs.grade,
                    ))

    # BULLISH SETUP: Sweep of low → BUY from OB above sweep
    for level in liq_lows[:5]:
        sweep_bars = m15_bars if m15_bars else h1_bars
        tol = _sweep_tolerance(symbol)
        if detect_sweep_low(sweep_bars, level, tolerance_pct=tol):
            # Displacement check: last bar must show bullish conviction, not a doji/inside bar
            _sweep_last = sweep_bars[-1] if sweep_bars else None
            if _sweep_last and _sweep_last.range > 0 and (_sweep_last.body / _sweep_last.range) < 0.28:
                continue  # Spinning top after sweep = no institutional follow-through

            sweep_touches = count_sweep_touches_low(sweep_bars, level, tolerance_pct=tol)
            ob = find_bullish_ob(h1_bars, start=0, search=10)
            if not ob:
                ob = find_bullish_ob(m15_bars, start=0, search=8) if m15_bars else None

            if ob:
                ob_low, ob_high = ob

                # Proximity gate: OB must be close to swept level — stale OBs don't hold
                if _atr_local > 0 and abs(ob_low - level) > _atr_local * 2:
                    continue

                # ICT AMD / Market Maker Model — ENTER AT MANIPULATION WICK EXTREME
                # When price sweeps liquidity and forms an OB, the optimal entry
                # is the manipulation wick extreme (the stop-run spike), NOT the
                # OB midpoint. The SL goes just beyond the wick — if price comes back
                # past the manipulation, the setup is invalidated.
                sweep_extreme_low = min((b.l for b in sweep_bars[:4]), default=level * 0.998)
                entry = sweep_extreme_low  # Enter at manipulation wick extreme
                
                # SL: just below the manipulation wick — if price retraces past here,
                # the sweep was NOT a true liquidity grab (stop hunt failed)
                sl_buffer = max(_atr_local * 0.5, (level - sweep_extreme_low) * 0.2)
                sl = sweep_extreme_low - sl_buffer
                # Cap SL at 1.5× ATR to prevent runaway risk
                if _atr_local > 0:
                    sl = max(sl, entry - _atr_local * 1.5)
                
                risk = entry - sl
                if risk <= 0:
                    continue
                tp1 = entry + risk * 1.5
                tp2 = entry + risk * 2.5
                tp3 = entry + risk * 4.0

                for liq_h in liq_highs:
                    if liq_h > entry + risk * 0.5 and liq_h < entry + risk * 3.0:
                        tp1 = max(tp1, liq_h - (liq_h - entry) * 0.1)
                        break

                confidence, extra_reasons = _score_buy_setup_full(
                    d1_bias, h4_struct, amd_phase, session, level, pdl, pwl,
                    quarter_info=quarter_info,
                    patterns=detected_patterns,
                    sr_info=sr_info_h1,
                    push_exh=push_exh,
                    eqh_eql=eqh_eql_info,
                )
                if w1_bias == d1_bias and w1_bias != "NEUTRAL":
                    confidence = min(100, confidence + 15)
                elif w1_bias not in ("NEUTRAL", d1_bias) and w1_bias != "NEUTRAL":
                    confidence = max(0, confidence - 10)

                # LuxAlgo Strong Low: sweeping with-trend institutional support = A+ signal
                if d1_bias == "BULLISH" and level in liq_lows[:3]:
                    confidence = min(100, confidence + 8)

                # Turtle Soup: sharp break below level + close back above = stop-run confirmed
                _turtle = detect_turtle_soup(m15_bars or h1_bars, level, "BUY")
                if _turtle:
                    confidence = min(100, confidence + 12)

                # Inducement: swept level is a known equal low = institutional stop hunt confirmed
                _is_inducement = level in h1_eq_lows or level in h4_eq_lows
                if _is_inducement:
                    confidence = min(100, confidence + 10)

                # Judas swing multi-touch bonus
                j_bonus = judas_swing_bonus(sweep_touches)
                if j_bonus:
                    confidence = min(100, confidence + j_bonus)

                # OTE precision: high-confidence setups get deepest entry (tighter SL, better RR)
                if confidence >= 70:
                    _deep_entry = ob_prec["ote_79"]
                    _deep_risk  = _deep_entry - sl
                    if _deep_risk > 0:
                        entry = _deep_entry
                        risk  = _deep_risk
                        tp1   = entry + risk * 1.5
                        tp2   = entry + risk * 2.5
                        tp3   = entry + risk * 4.0

                # Snap TPs to institutional distribution grid
                _dist = _dist_intervals.get(symbol, _dist_intervals.get("default", 0.0))
                if _dist > 0:
                    tp1 = _snap_to_interval(tp1, _dist, "BUY")
                    tp2 = _snap_to_interval(tp2, _dist, "BUY")

                # RR guard: TP snapping must not collapse RR below 1.3:1
                if abs(tp1 - entry) < risk * 1.3:
                    continue

                # Multi-confluence gate: bare sweep alone is never enough
                _buy_confirmers = sum([
                    bool(_turtle), _is_inducement,
                    d1_bias == "BULLISH", w1_bias == "BULLISH",
                ])
                if _buy_confirmers == 0:
                    continue

                if confidence >= 60:
                    pattern_names = [p.get("pattern","") for p in detected_patterns if p.get("direction") == "BUY"]
                    judas_tag = "" if sweep_touches < 2 else f" | Judas {sweep_touches}-touch sweep"
                    reasons = [
                        f"Liquidity sweep of {level:.5g}{judas_tag} — sell stops hunted",
                        f"Bullish OB: {ob_low:.5g}—{ob_high:.5g} | Precision entry @ {entry:.5g} (OTE 50%)",
                        f"W1={w1_bias} | D1 Bias: {d1_bias} | H4: {h4_struct}",
                        f"AMD Phase: {amd_phase} | Session: {session}",
                        f"Quarter: {quarter_info['quarter']} ({quarter_info['pct']:.0f}% of daily range)",
                    ]
                    if pattern_names:
                        reasons.append(f"Patterns confirmed: {', '.join(pattern_names)}")
                    if push_exh.get("signal"):
                        reasons.append(f"Momentum: {push_exh['signal']}")
                    if sr_info_h1.get("phase_at_support"):
                        reasons.append(f"S/R: {sr_info_h1['phase_at_support']}")
                    if _is_inducement:
                        reasons.append("Inducement confirmed — swept level is equal low liquidity pool")
                    reasons.extend(extra_reasons)
                    if level == pdl:
                        reasons.insert(0, "Previous Day Low swept — HIGH PRIORITY")
                    elif level == pwl:
                        reasons.insert(0, "Previous Week Low swept — HIGH PRIORITY")
                    elif level == pml:
                        reasons.insert(0, "Previous Month Low swept")

                    _pivot_tag_levels(reasons, d1_pivots, w1_pivots, entry, tp1, tp2, "BUY")

                    rr = _blended_rr(abs(entry - sl), tp1, tp2, tp3, entry)
                    cs = _build_confluence(
                        symbol, "BUY", d1_bias, h4_struct, amd_phase,
                        gmt_hour, gmt_min, "SWEEP_LOW_OB",
                        liq_swept=True, h4_eq_highs=h4_eq_highs, h4_eq_lows=h4_eq_lows,
                        current_price=current_price, fvg_low=ob_low, fvg_high=ob_high,
                        h1_bars=h1_bars, ltf_confirmed=False, prices_dict=prices_dict_all,
                    )
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
                        confluence=cs, grade=cs.grade,
                    ))

    # ── Step 5: FVG Entries — sweep context gated ───────────────────
    # ICT: FVG retracement entries are only valid AFTER liquidity has been
    # swept. Without a confirmed sweep, entries into imbalances are premature.
    # When no sweep: confidence is penalized (-20) and threshold raised to 65.
    # If CISD or Inverted FVG is detected, partial credit restores threshold.
    if _h1_choppy:
        pass  # Skip FVG entries in choppy/ranging markets entirely
    # Bullish FVG on H1 with D1 bullish bias
    elif d1_bias == "BULLISH" or h4_struct in ("BOS_BULLISH", "CHOCH_BULL"):
        fvg = find_bullish_fvg(h1_bars[:8])
        if fvg:
            fvg_low, fvg_high = fvg
            if fvg_low < current_price:  # Price hasn't filled it yet
                # Enter at FVG midpoint (OTE)
                entry = fvg_low + (fvg_high - fvg_low) * 0.50
                # H1 zone defines the trade area; LTF trigger refines entry
                h1_sl = _structural_sl(h1_bars, entry, "BUY", fvg_low)
                ltf   = get_ltf_entry(m15_bars, m5_bars, m1_bars, "BUY",
                                      fvg_low, fvg_high, h1_sl)

                # Use LTF entry if triggered, otherwise H1 zone entry
                final_entry = ltf["entry"] if ltf["confirmed"] else entry
                final_sl    = ltf["sl"]    if ltf["confirmed"] else h1_sl
                risk = final_entry - final_sl
                if risk > 0:
                    tp1 = final_entry + risk * 1.5
                    tp2 = final_entry + risk * 2.5
                    tp3 = final_entry + risk * 4.0

                    fvg_q = get_quarter_position(final_entry, d1_high, d1_low)
                    confidence, extra_r = _score_buy_setup_full(
                        d1_bias, h4_struct, amd_phase, session, fvg_low, pdl, pwl,
                        quarter_info=fvg_q, patterns=detected_patterns,
                        sr_info=sr_info_h1, push_exh=push_exh,
                        eqh_eql=eqh_eql_info,
                    )
                    confidence = max(40, confidence - 5)

                    # ── Sweep gate: penalize FVG entries without prior stop hunt ──
                    # ICT: enter from an imbalance ONLY after liquidity has been swept
                    if not _recently_swept_low:
                        confidence = max(0, confidence - 20)
                    _fvg_buy_min = 68 if not _recently_swept_low else 58  # raised: swept 45→58, unswept 65→68

                    # W1 alignment bonus/penalty
                    if w1_bias == d1_bias and w1_bias != "NEUTRAL":
                        confidence = min(100, confidence + 15)
                    elif w1_bias not in ("NEUTRAL", d1_bias) and w1_bias != "NEUTRAL":
                        confidence = max(0, confidence - 10)

                    # IOFED: FVG in OTE of displacement candle = A+ factor
                    iofed = analyze_iofed(h1_bars, "BUY", fvg_low, fvg_high)
                    if iofed["fvg_in_ote"]:
                        confidence = min(100, confidence + 20)

                    # Inverted FVG / CISD: close-through of opposing imbalance = intent signal
                    _ltf_bars_buy = m15_bars if m15_bars else h1_bars
                    _inv_fvg_buy  = detect_inverted_fvg(_ltf_bars_buy, "BUY")
                    _cisd_buy     = detect_cisd(_ltf_bars_buy, "BUY")
                    if _inv_fvg_buy["detected"]:
                        confidence = min(100, confidence + _inv_fvg_buy["bonus"])
                        if not _recently_swept_low:
                            _fvg_buy_min = 63  # Inv FVG = partial sweep: discount 68→63
                    if _cisd_buy["detected"]:
                        confidence = min(100, confidence + _cisd_buy["bonus"])
                        if not _recently_swept_low:
                            _fvg_buy_min = min(_fvg_buy_min, 63)  # CISD caps at 63, never below

                    # LTF confirmed → precision entry, boost confidence
                    # LTF unconfirmed → zone identified, reduce confidence (awaiting trigger)
                    if ltf["confirmed"]:
                        confidence = min(100, confidence + 10)
                        etype = f"H1_FVG_{ltf['trigger_tf']}_BUY"
                    else:
                        confidence = max(35, confidence - 8)
                        # Price already inside zone = partial confirmation even without MSS
                        if fvg_low <= current_price <= fvg_high:
                            confidence = min(100, confidence + 5)
                        etype = "H1_FVG_ZONE_BUY"

                    if confidence >= _fvg_buy_min:
                        rr = _blended_rr(abs(final_entry - final_sl), tp1, tp2, tp3, final_entry)
                        pat_names = [p.get("pattern","") for p in detected_patterns if p.get("direction")=="BUY"]
                        reasons = [
                            f"HTF Bias: W1={w1_bias} | D1={d1_bias} | H4={h4_struct}",
                            f"H1 Bullish FVG zone: {fvg_low:.5g}—{fvg_high:.5g}",
                            f"LTF: {ltf['reason']}",
                            f"Entry: {final_entry:.5g} | SL: {final_sl:.5g}",
                            f"Session: {session} | AMD: {amd_phase}",
                            f"Quarter: {fvg_q['quarter']} ({fvg_q['pct']:.0f}%)",
                            f"Sweep confirmed: {'YES' if _recently_swept_low else 'NO (CISD/IOFED required)'}",
                        ]
                        if iofed["fvg_in_ote"]: reasons.append(f"IOFED: {iofed['reason']}")
                        if _inv_fvg_buy["detected"]: reasons.append(_inv_fvg_buy["reason"])
                        if _cisd_buy["detected"]:    reasons.append(_cisd_buy["reason"])
                        if ltf["m15_mss"]: reasons.append("M15 MSS confirmed")
                        if ltf["m5_fvg"]:  reasons.append("M5 FVG entry trigger")
                        if ltf["m1_mss"]:  reasons.append("M1 MSS — execution confirmed")
                        if pat_names:      reasons.append(f"Patterns: {', '.join(pat_names)}")
                        if push_exh.get("signal"): reasons.append(f"Momentum: {push_exh['signal']}")
                        reasons.extend(extra_r)
                        cs = _build_confluence(
                            symbol, "BUY", d1_bias, h4_struct, amd_phase,
                            gmt_hour, gmt_min, etype,
                            liq_swept=_recently_swept_low, h4_eq_highs=h4_eq_highs, h4_eq_lows=h4_eq_lows,
                            current_price=current_price, fvg_low=fvg_low, fvg_high=fvg_high,
                            h1_bars=h1_bars, ltf_confirmed=ltf["confirmed"],
                            prices_dict=prices_dict_all,
                        )
                        setups.append(ICTSetup(
                            symbol=symbol, direction="BUY",
                            entry_type=etype,
                            entry_price=round(final_entry, 5),
                            sl_price=round(final_sl, 5),
                            tp1_price=round(tp1, 5),
                            tp2_price=round(tp2, 5),
                            tp3_price=round(tp3, 5),
                            confidence=confidence,
                            reasons=reasons,
                            session=session, amd_phase=amd_phase,
                            rr_ratio=round(rr, 2), tf_bias=d1_bias,
                            invalidation=round(final_sl * 0.999, 5),
                            confluence=cs, grade=cs.grade,
                            liq_swept=_recently_swept_low,
                        ))

    if not _h1_choppy and (d1_bias == "BEARISH" or h4_struct in ("BOS_BEARISH", "CHOCH_BEAR")):
        fvg = find_bearish_fvg(h1_bars[:8])
        if fvg:
            fvg_low, fvg_high = fvg
            if fvg_high > current_price:
                entry = fvg_high - (fvg_high - fvg_low) * 0.50
                sl    = _structural_sl(h1_bars, entry, "SELL", fvg_high)
                h1_sl = _structural_sl(h1_bars, entry, "SELL", fvg_high)
                ltf   = get_ltf_entry(m15_bars, m5_bars, m1_bars, "SELL",
                                      fvg_low, fvg_high, h1_sl)
                final_entry = ltf["entry"] if ltf["confirmed"] else entry
                final_sl    = ltf["sl"]    if ltf["confirmed"] else h1_sl
                risk = final_sl - final_entry
                if risk > 0:
                    tp1 = final_entry - risk * 1.5
                    tp2 = final_entry - risk * 2.5
                    tp3 = final_entry - risk * 4.0

                    fvg_q = get_quarter_position(final_entry, d1_high, d1_low)
                    confidence, extra_r = _score_sell_setup_full(
                        d1_bias, h4_struct, amd_phase, session, fvg_high, pdh, pwh,
                        quarter_info=fvg_q, patterns=detected_patterns,
                        sr_info=sr_info_h1, push_exh=push_exh,
                        eqh_eql=eqh_eql_info,
                    )
                    confidence = max(40, confidence - 5)

                    # ── Sweep gate: penalize FVG entries without prior stop hunt ──
                    if not _recently_swept_high:
                        confidence = max(0, confidence - 20)
                    _fvg_sell_min = 68 if not _recently_swept_high else 58  # raised: swept 45→58, unswept 65→68

                    if w1_bias == d1_bias and w1_bias != "NEUTRAL":
                        confidence = min(100, confidence + 15)
                    elif w1_bias not in ("NEUTRAL", d1_bias) and w1_bias != "NEUTRAL":
                        confidence = max(0, confidence - 10)

                    iofed = analyze_iofed(h1_bars, "SELL", fvg_low, fvg_high)
                    if iofed["fvg_in_ote"]:
                        confidence = min(100, confidence + 20)

                    # Inverted FVG / CISD: close-through of opposing imbalance = intent signal
                    _ltf_bars_sell = m15_bars if m15_bars else h1_bars
                    _inv_fvg_sell  = detect_inverted_fvg(_ltf_bars_sell, "SELL")
                    _cisd_sell     = detect_cisd(_ltf_bars_sell, "SELL")
                    if _inv_fvg_sell["detected"]:
                        confidence = min(100, confidence + _inv_fvg_sell["bonus"])
                        if not _recently_swept_high:
                            _fvg_sell_min = 63  # Inv FVG = partial sweep: discount 68→63
                    if _cisd_sell["detected"]:
                        confidence = min(100, confidence + _cisd_sell["bonus"])
                        if not _recently_swept_high:
                            _fvg_sell_min = min(_fvg_sell_min, 63)  # CISD caps at 63, never below

                    if ltf["confirmed"]:
                        confidence = min(100, confidence + 10)
                        etype = f"H1_FVG_{ltf['trigger_tf']}_SELL"
                    else:
                        confidence = max(35, confidence - 8)
                        # Price already inside zone = partial confirmation even without MSS
                        if fvg_low <= current_price <= fvg_high:
                            confidence = min(100, confidence + 5)
                        etype = "H1_FVG_ZONE_SELL"

                    if confidence >= _fvg_sell_min:
                        rr = _blended_rr(abs(final_sl - final_entry), tp1, tp2, tp3, final_entry)
                        pat_names = [p.get("pattern","") for p in detected_patterns if p.get("direction")=="SELL"]
                        reasons = [
                            f"HTF Bias: W1={w1_bias} | D1={d1_bias} | H4={h4_struct}",
                            f"H1 Bearish FVG zone: {fvg_low:.5g}—{fvg_high:.5g}",
                            f"LTF: {ltf['reason']}",
                            f"Entry: {final_entry:.5g} | SL: {final_sl:.5g}",
                            f"Session: {session} | AMD: {amd_phase}",
                            f"Quarter: {fvg_q['quarter']} ({fvg_q['pct']:.0f}%)",
                            f"Sweep confirmed: {'YES' if _recently_swept_high else 'NO (CISD/IOFED required)'}",
                        ]
                        if iofed["fvg_in_ote"]: reasons.append(f"IOFED: {iofed['reason']}")
                        if _inv_fvg_sell["detected"]: reasons.append(_inv_fvg_sell["reason"])
                        if _cisd_sell["detected"]:    reasons.append(_cisd_sell["reason"])
                        if ltf["m15_mss"]: reasons.append("M15 MSS confirmed")
                        if ltf["m5_fvg"]:  reasons.append("M5 FVG entry trigger")
                        if ltf["m1_mss"]:  reasons.append("M1 MSS — execution confirmed")
                        if pat_names:      reasons.append(f"Patterns: {', '.join(pat_names)}")
                        if push_exh.get("signal"): reasons.append(f"Momentum: {push_exh['signal']}")
                        reasons.extend(extra_r)
                        cs = _build_confluence(
                            symbol, "SELL", d1_bias, h4_struct, amd_phase,
                            gmt_hour, gmt_min, etype,
                            liq_swept=_recently_swept_high, h4_eq_highs=h4_eq_highs, h4_eq_lows=h4_eq_lows,
                            current_price=current_price, fvg_low=fvg_low, fvg_high=fvg_high,
                            h1_bars=h1_bars, ltf_confirmed=ltf["confirmed"],
                            prices_dict=prices_dict_all,
                        )
                        setups.append(ICTSetup(
                            symbol=symbol, direction="SELL",
                            entry_type=etype,
                            entry_price=round(final_entry, 5),
                            sl_price=round(final_sl, 5),
                            tp1_price=round(tp1, 5),
                            tp2_price=round(tp2, 5),
                            tp3_price=round(tp3, 5),
                            confidence=confidence,
                            reasons=reasons,
                            session=session, amd_phase=amd_phase,
                            rr_ratio=round(rr, 2), tf_bias=d1_bias,
                            invalidation=round(final_sl * 1.001, 5),
                            confluence=cs, grade=cs.grade,
                            liq_swept=_recently_swept_high,
                        ))

    # ── Step 6: Breaker Block Setups ─────────────────────────────────
    # Breaker blocks are mitigated OBs — they require the same sweep context as FVGs.
    if bull_breaker and (d1_bias == "BULLISH" or h4_struct == "BOS_BULLISH") and not _h1_choppy:
        bb       = bull_breaker
        h1_entry = bb["ce"]
        h1_sl    = _structural_sl(h1_bars, h1_entry, "BUY", bb["ob_low"])
        ltf      = get_ltf_entry(m15_bars, m5_bars, m1_bars, "BUY",
                                 bb["ob_low"], bb["ob_high"], h1_sl)
        final_entry = ltf["entry"] if ltf["confirmed"] else h1_entry
        final_sl    = ltf["sl"]    if ltf["confirmed"] else h1_sl
        risk = final_entry - final_sl
        if risk > 0:
            tp1 = final_entry + risk * 1.5
            tp2 = final_entry + risk * 2.5
            tp3 = final_entry + risk * 4.0
            bb_q  = get_quarter_position(final_entry, d1_high, d1_low)
            conf, extra_r = _score_buy_setup_full(
                d1_bias, h4_struct, amd_phase, session, final_entry, pdl, pwl,
                quarter_info=bb_q, patterns=detected_patterns,
                sr_info=sr_info_h1, push_exh=push_exh,
                eqh_eql=eqh_eql_info,
            )
            conf += bb["confidence_bonus"]
            conf = min(conf, 100)
            # Sweep gate for breaker blocks (same principle as FVG entries)
            if not _recently_swept_low:
                conf = max(0, conf - 15)
            if w1_bias == d1_bias and w1_bias != "NEUTRAL":
                conf = min(100, conf + 15)
            elif w1_bias not in ("NEUTRAL", d1_bias) and w1_bias != "NEUTRAL":
                conf = max(0, conf - 10)
            if ltf["confirmed"]:
                conf = min(100, conf + 10)
                etype = f"H1_BREAKER_{ltf['trigger_tf']}_BUY"
            else:
                conf = max(35, conf - 10)
                etype = "H1_BREAKER_ZONE_BUY"
            rr = _blended_rr(risk, tp1, tp2, tp3, final_entry)
            reasons = [
                f"HTF Bias: W1={w1_bias} | D1={d1_bias} | H4={h4_struct}",
                bb["reason"],
                f"LTF: {ltf['reason']}",
                f"Entry: {final_entry:.5g} | SL: {final_sl:.5g}",
            ]
            if ltf["m15_mss"]: reasons.append("M15 MSS confirmed")
            if ltf["m5_fvg"]:  reasons.append("M5 FVG entry trigger")
            if ltf["m1_mss"]:  reasons.append("M1 MSS — execution confirmed")
            reasons.extend(extra_r)
            cs = _build_confluence(
                symbol, "BUY", d1_bias, h4_struct, amd_phase,
                gmt_hour, gmt_min, etype,
                liq_swept=_recently_swept_low, h4_eq_highs=h4_eq_highs, h4_eq_lows=h4_eq_lows,
                current_price=current_price, fvg_low=bb["ob_low"], fvg_high=bb["ob_high"],
                h1_bars=h1_bars, ltf_confirmed=ltf["confirmed"], prices_dict=prices_dict_all,
            )
            setups.append(ICTSetup(
                symbol=symbol, direction="BUY",
                entry_type=etype,
                entry_price=round(final_entry, 5), sl_price=round(final_sl, 5),
                tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                confidence=conf,
                reasons=reasons,
                session=session, amd_phase=amd_phase,
                rr_ratio=round(rr, 2), tf_bias=d1_bias,
                invalidation=round(final_sl * 0.999, 5),
                confluence=cs, grade=cs.grade,
                liq_swept=_recently_swept_low,
            ))

    if bear_breaker and (d1_bias == "BEARISH" or h4_struct == "BOS_BEARISH") and not _h1_choppy:
        bb       = bear_breaker
        h1_entry = bb["ce"]
        h1_sl    = _structural_sl(h1_bars, h1_entry, "SELL", bb["ob_high"])
        ltf      = get_ltf_entry(m15_bars, m5_bars, m1_bars, "SELL",
                                 bb["ob_low"], bb["ob_high"], h1_sl)
        final_entry = ltf["entry"] if ltf["confirmed"] else h1_entry
        final_sl    = ltf["sl"]    if ltf["confirmed"] else h1_sl
        risk = final_sl - final_entry
        if risk > 0:
            tp1 = final_entry - risk * 1.5
            tp2 = final_entry - risk * 2.5
            tp3 = final_entry - risk * 4.0
            bb_q  = get_quarter_position(final_entry, d1_high, d1_low)
            conf, extra_r = _score_sell_setup_full(
                d1_bias, h4_struct, amd_phase, session, final_entry, pdh, pwh,
                quarter_info=bb_q, patterns=detected_patterns,
                sr_info=sr_info_h1, push_exh=push_exh,
                eqh_eql=eqh_eql_info,
            )
            conf += bb["confidence_bonus"]
            conf = min(conf, 100)
            # Sweep gate for bearish breaker
            if not _recently_swept_high:
                conf = max(0, conf - 15)
            if w1_bias == d1_bias and w1_bias != "NEUTRAL":
                conf = min(100, conf + 15)
            elif w1_bias not in ("NEUTRAL", d1_bias) and w1_bias != "NEUTRAL":
                conf = max(0, conf - 10)
            if ltf["confirmed"]:
                conf = min(100, conf + 10)
                etype = f"H1_BREAKER_{ltf['trigger_tf']}_SELL"
            else:
                conf = max(35, conf - 10)
                etype = "H1_BREAKER_ZONE_SELL"
            rr = _blended_rr(risk, tp1, tp2, tp3, final_entry)
            reasons = [
                f"HTF Bias: W1={w1_bias} | D1={d1_bias} | H4={h4_struct}",
                bb["reason"],
                f"LTF: {ltf['reason']}",
                f"Entry: {final_entry:.5g} | SL: {final_sl:.5g}",
            ]
            if ltf["m15_mss"]: reasons.append("M15 MSS confirmed")
            if ltf["m5_fvg"]:  reasons.append("M5 FVG entry trigger")
            if ltf["m1_mss"]:  reasons.append("M1 MSS — execution confirmed")
            reasons.extend(extra_r)
            cs = _build_confluence(
                symbol, "SELL", d1_bias, h4_struct, amd_phase,
                gmt_hour, gmt_min, etype,
                liq_swept=_recently_swept_high, h4_eq_highs=h4_eq_highs, h4_eq_lows=h4_eq_lows,
                current_price=current_price, fvg_low=bb["ob_low"], fvg_high=bb["ob_high"],
                h1_bars=h1_bars, ltf_confirmed=ltf["confirmed"], prices_dict=prices_dict_all,
            )
            setups.append(ICTSetup(
                symbol=symbol, direction="SELL",
                entry_type=etype,
                entry_price=round(final_entry, 5), sl_price=round(final_sl, 5),
                tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                confidence=conf,
                reasons=reasons,
                session=session, amd_phase=amd_phase,
                rr_ratio=round(rr, 2), tf_bias=d1_bias,
                invalidation=round(final_sl * 1.001, 5),
                confluence=cs, grade=cs.grade,
                liq_swept=_recently_swept_high,
            ))

    # ── Step 7: Silver Bullet Model ───────────────────────────────────
    # Only fires during 10-11 AM EST or 2-3 PM EST windows (London/NY sessions).
    # Blocked during ACCUMULATION (Asia) — Silver Bullet is a NY/London model only.
    sb_window = is_silver_bullet_window(gmt_hour, gmt_min)
    if sb_window and amd_phase != "ACCUMULATION":
        sb_bonus, sb_reason = _score_silver_bullet_bonus(gmt_hour, gmt_min)

        # Silver Bullet BUY: in SB window, D1/H4 bullish, first BISI FVG above current price
        if (d1_bias == "BULLISH" or h4_struct == "BOS_BULLISH") and all_bull_fvgs:
            for fvg in all_bull_fvgs[:2]:
                if fvg["low"] < current_price:    # FVG is below — enter at CE on pullback
                    entry = fvg["ce"]
                    sl    = _structural_sl(h1_bars, entry, "BUY", fvg["low"])
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
                        eqh_eql=eqh_eql_info,
                    )
                    conf = min(conf + sb_bonus, 100)
                    fvg_mit = fvg.get("mitigation_pct", 0)
                    if fvg_mit > 60:
                        continue  # Skip FVGs that are more than 60% filled
                    if fvg_mit > 30:
                        conf = max(0, conf - 8)  # Penalise partially filled zones
                    iofed = analyze_iofed(h1_bars, "BUY", fvg["low"], fvg["high"])
                    if iofed["fvg_in_ote"]:
                        conf = min(100, conf + 20)
                    rr = _blended_rr(risk, tp1, tp2, tp3, entry)
                    sb_etype = f"SILVER_BULLET_{sb_window}"
                    reasons = [
                        sb_reason,
                        f"BISI FVG: {fvg['low']:.5g}–{fvg['high']:.5g} | CE entry: {fvg['ce']:.5g}",
                        f"W1={w1_bias} | D1={d1_bias} | H4={h4_struct}",
                    ] + extra_r
                    if iofed["fvg_in_ote"]: reasons.append(f"IOFED: {iofed['reason']}")
                    cs = _build_confluence(
                        symbol, "BUY", d1_bias, h4_struct, amd_phase,
                        gmt_hour, gmt_min, sb_etype,
                        liq_swept=_recently_swept_low, h4_eq_highs=h4_eq_highs, h4_eq_lows=h4_eq_lows,
                        current_price=current_price, fvg_low=fvg["low"], fvg_high=fvg["high"],
                        h1_bars=h1_bars, ltf_confirmed=False, prices_dict=prices_dict_all,
                    )
                    setups.append(ICTSetup(
                        symbol=symbol, direction="BUY",
                        entry_type=sb_etype,
                        entry_price=round(entry, 5), sl_price=round(sl, 5),
                        tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                        confidence=conf,
                        reasons=reasons,
                        session=session, amd_phase=amd_phase,
                        rr_ratio=round(rr, 2), tf_bias=d1_bias,
                        invalidation=round(sl * 0.999, 5),
                        confluence=cs, grade=cs.grade,
                        liq_swept=_recently_swept_low,
                    ))
                    break   # Only take first qualifying FVG

        # Silver Bullet SELL
        if (d1_bias == "BEARISH" or h4_struct == "BOS_BEARISH") and all_bear_fvgs:
            for fvg in all_bear_fvgs[:2]:
                if fvg["high"] > current_price:   # FVG is above — enter at CE on rally
                    entry = fvg["ce"]
                    sl    = _structural_sl(h1_bars, entry, "SELL", fvg["high"])
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
                        eqh_eql=eqh_eql_info,
                    )
                    conf = min(conf + sb_bonus, 100)
                    fvg_mit = fvg.get("mitigation_pct", 0)
                    if fvg_mit > 60:
                        continue  # Skip FVGs that are more than 60% filled
                    if fvg_mit > 30:
                        conf = max(0, conf - 8)  # Penalise partially filled zones
                    iofed = analyze_iofed(h1_bars, "SELL", fvg["low"], fvg["high"])
                    if iofed["fvg_in_ote"]:
                        conf = min(100, conf + 20)
                    rr = _blended_rr(risk, tp1, tp2, tp3, entry)
                    sb_etype = f"SILVER_BULLET_{sb_window}"
                    reasons = [
                        sb_reason,
                        f"SIBI FVG: {fvg['low']:.5g}–{fvg['high']:.5g} | CE entry: {fvg['ce']:.5g}",
                        f"W1={w1_bias} | D1={d1_bias} | H4={h4_struct}",
                    ] + extra_r
                    if iofed["fvg_in_ote"]: reasons.append(f"IOFED: {iofed['reason']}")
                    cs = _build_confluence(
                        symbol, "SELL", d1_bias, h4_struct, amd_phase,
                        gmt_hour, gmt_min, sb_etype,
                        liq_swept=_recently_swept_high, h4_eq_highs=h4_eq_highs, h4_eq_lows=h4_eq_lows,
                        current_price=current_price, fvg_low=fvg["low"], fvg_high=fvg["high"],
                        h1_bars=h1_bars, ltf_confirmed=False, prices_dict=prices_dict_all,
                    )
                    setups.append(ICTSetup(
                        symbol=symbol, direction="SELL",
                        entry_type=sb_etype,
                        entry_price=round(entry, 5), sl_price=round(sl, 5),
                        tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                        confidence=conf,
                        reasons=reasons,
                        session=session, amd_phase=amd_phase,
                        rr_ratio=round(rr, 2), tf_bias=d1_bias,
                        invalidation=round(sl * 1.001, 5),
                        confluence=cs, grade=cs.grade,
                        liq_swept=_recently_swept_high,
                    ))
                    break

    # ── Power of 3 Detection ──────────────────────────────────────────────────
    po3 = detect_power_of_3(m15_bars)
    if po3["po3_detected"] and po3["direction"]:
        po3_dir = po3["direction"]
        if po3_dir == "BUY":
            ob = find_bullish_ob(m15_bars, start=0, search=8)
            fvg = find_bullish_fvg(m15_bars)
            zone = ob or (fvg and (fvg[0], fvg[1]))
            if zone:
                entry = (zone[0] + zone[1]) / 2
                sl = po3["sweep_extreme"] - abs(zone[1] - zone[0]) * 0.5
                sl = min(sl, zone[0] - abs(zone[1] - zone[0]) * 0.3)
                risk = entry - sl
                if risk > 0:
                    po3_conf = 60 + po3["confidence_bonus"]
                    if amd_phase in ("MANIPULATION", "DISTRIBUTION"):
                        po3_conf = min(100, po3_conf + 8)
                    tp1 = entry + risk * 1.5
                    tp2 = entry + risk * 2.5
                    tp3 = entry + risk * 4.0
                    setups.append(ICTSetup(
                        symbol=symbol, direction="BUY",
                        entry_type="POWER_OF_3_BUY",
                        entry_price=round(entry, 5), sl_price=round(sl, 5),
                        tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                        confidence=min(100, po3_conf),
                        reasons=[po3["reason"], f"Session: {session} | AMD: {amd_phase}"],
                        session=session, amd_phase=amd_phase,
                        rr_ratio=round(abs(tp1 - entry) / risk, 2), tf_bias=d1_bias,
                    ))
        elif po3_dir == "SELL":
            ob = find_bearish_ob(m15_bars, start=0, search=8)
            fvg = find_bearish_fvg(m15_bars)
            zone = ob or (fvg and (fvg[0], fvg[1]))
            if zone:
                entry = (zone[0] + zone[1]) / 2
                sl = po3["sweep_extreme"] + abs(zone[1] - zone[0]) * 0.5
                sl = max(sl, zone[1] + abs(zone[1] - zone[0]) * 0.3)
                risk = sl - entry
                if risk > 0:
                    po3_conf = 60 + po3["confidence_bonus"]
                    if amd_phase in ("MANIPULATION", "DISTRIBUTION"):
                        po3_conf = min(100, po3_conf + 8)
                    tp1 = entry - risk * 1.5
                    tp2 = entry - risk * 2.5
                    tp3 = entry - risk * 4.0
                    setups.append(ICTSetup(
                        symbol=symbol, direction="SELL",
                        entry_type="POWER_OF_3_SELL",
                        entry_price=round(entry, 5), sl_price=round(sl, 5),
                        tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                        confidence=min(100, po3_conf),
                        reasons=[po3["reason"], f"Session: {session} | AMD: {amd_phase}"],
                        session=session, amd_phase=amd_phase,
                        rr_ratio=round(abs(entry - tp1) / risk, 2), tf_bias=d1_bias,
                    ))

    # ── CHoCH Standalone Entry ────────────────────────────────────────────────
    # After a liquidity sweep + CHoCH on M15 + fresh FVG/OB near CHoCH candle
    if m15_bars and len(m15_bars) >= 8:
        for choch_dir, score_fn, ob_fn, fvg_fn in [
            ("BUY",  _score_buy_setup_full,  find_bullish_ob,  find_bullish_fvg),
            ("SELL", _score_sell_setup_full, find_bearish_ob,  find_bearish_fvg),
        ]:
            if detect_mss(m15_bars, choch_dir, lookback=8):
                ob_zone  = ob_fn(m15_bars, start=0, search=4)
                fvg_zone = fvg_fn(m15_bars)
                zone = ob_zone or (fvg_zone and (fvg_zone[0], fvg_zone[1]))
                if zone:
                    z_low, z_high = zone
                    if choch_dir == "BUY":
                        entry = (z_low + z_high) / 2
                        sl = _structural_sl(m15_bars, entry, "BUY", z_low)
                        risk = entry - sl
                        if risk <= 0:
                            continue
                        tp1 = entry + risk * 1.5
                        tp2 = entry + risk * 2.5
                        tp3 = entry + risk * 4.0
                        level_arg = (pdl, pwl)
                        choch_conf, choch_extra = score_fn(
                            d1_bias, h4_struct, amd_phase, session, z_low, *level_arg,
                            quarter_info=get_quarter_position(entry, d1_high, d1_low),
                            patterns=detected_patterns, sr_info=sr_info_h1,
                            push_exh=push_exh, eqh_eql=eqh_eql_info,
                        )
                    else:
                        entry = (z_low + z_high) / 2
                        sl = _structural_sl(m15_bars, entry, "SELL", z_high)
                        risk = sl - entry
                        if risk <= 0:
                            continue
                        tp1 = entry - risk * 1.5
                        tp2 = entry - risk * 2.5
                        tp3 = entry - risk * 4.0
                        level_arg = (pdh, pwh)
                        choch_conf, choch_extra = score_fn(
                            d1_bias, h4_struct, amd_phase, session, z_high, *level_arg,
                            quarter_info=get_quarter_position(entry, d1_high, d1_low),
                            patterns=detected_patterns, sr_info=sr_info_h1,
                            push_exh=push_exh, eqh_eql=eqh_eql_info,
                        )
                    # Sweep gate for CHoCH: penalize when no sweep preceded the CHoCH
                    _choch_swept = (_recently_swept_low  if choch_dir == "BUY"
                                    else _recently_swept_high)
                    if not _choch_swept:
                        choch_conf = max(0, choch_conf - 15)
                    if w1_bias == d1_bias and w1_bias != "NEUTRAL":
                        choch_conf = min(100, choch_conf + 15)
                    _choch_min = 60 if _choch_swept else 72  # Higher bar without sweep
                    if choch_conf >= _choch_min:
                        setups.append(ICTSetup(
                            symbol=symbol, direction=choch_dir,
                            entry_type=f"CHOCH_ENTRY_{choch_dir}",
                            entry_price=round(entry, 5), sl_price=round(sl, 5),
                            tp1_price=round(tp1, 5), tp2_price=round(tp2, 5), tp3_price=round(tp3, 5),
                            confidence=min(100, choch_conf),
                            reasons=[
                                f"M15 CHoCH {choch_dir} — structure shift confirmed",
                                f"Entry zone: {z_low:.5g}–{z_high:.5g}",
                                f"Session: {session} | AMD: {amd_phase}",
                                f"Sweep context: {'confirmed' if _choch_swept else 'NOT confirmed — high bar applied'}",
                            ] + choch_extra[:3],
                            session=session, amd_phase=amd_phase,
                            rr_ratio=round(abs(tp1 - entry) / max(risk, 0.00001), 2),
                            tf_bias=d1_bias,
                        ))

    # Apply learned pattern-recognition model boost (no-op if model not yet trained)
    try:
        _apply_pattern_model_boost(setups)
    except Exception:
        pass

    # ── Enrich every setup with EMA context ───────────────────────────
    for st in setups:
        # Pick bars based on entry_type (LTF entries use M5, HTF use H1)
        entry_tf_bars = m5_bars if "M5" in st.entry_type or "LTF" in st.entry_type else h1_bars
        if not entry_tf_bars:
            entry_tf_bars = h1_bars
        
        emas = get_ema_levels(entry_tf_bars)
        st.ema20 = emas.get("ema_20", 0.0)
        st.ema200 = emas.get("ema_200", 0.0)
        st.ema800 = emas.get("ema_800", 0.0)
        
        pv = price_vs_ema20(entry_tf_bars)
        st.ema_deviation = pv.get("deviation", 0.0)
        
        # ema_aligned: entry is near EMA20 (returning to mean) OR price stretched >2 ATR
        entry = st.entry_price
        if st.ema20 > 0 and entry > 0:
            dist_from_ema = abs(entry - st.ema20) / st.ema20 * 100
            # Aligned if entry is within 0.5 ATR of EMA20 (returning) OR >2 ATR stretched
            atr_local = _calc_atr(entry_tf_bars, 14)
            in_ema_zone = abs(entry - st.ema20) <= atr_local * 0.5 if atr_local > 0 else False
            st.ema_aligned = in_ema_zone or st.ema_deviation > 2.0
            
            # Add to confluence score
            st.confluence.ema_aligned = st.ema_aligned
            
            # Boost confidence: +5 if returning to EMA20, +3 if stretched
            if in_ema_zone:
                st.confidence = min(100, st.confidence + 5)
                st.reasons.append(f"EMA20 return: entry {entry:.5f} within 0.5×ATR of EMA20 {st.ema20:.5f}")
            elif st.ema_deviation > 2.0:
                st.confidence = min(100, st.confidence + 3)
                st.reasons.append(f"EMA20 stretched: price {st.ema_deviation:.1f}×ATR from EMA20 — mean reversion likely")
            
            # Add macro EMA alignment to reasons
            if ema_trend_aligned:
                if d1_bias == h4_struct[:4] if len(h4_struct) >= 4 else True:
                    st.reasons.append(f"EMA trend aligned: D1/H4 EMA20 both {'above' if d1_bias == 'BULLISH' else 'below'} price")
        
        # Add EMA levels to reasons for visibility
        if st.ema20 > 0:
            st.reasons.append(f"EMA20={st.ema20:.5f} EMA200={st.ema200:.5f if st.ema200 else 0:.5f}")
        
        # ── Multi-TF EMAs (20, 200, 800) for all timeframes ─────────
        all_tf_emas = {}
        for tf_bars, tf_name in [
            (w1_bars, "W1"), (d1_bars, "D1"), (h4_bars, "H4"),
            (h1_bars, "H1"), (m15_bars, "M15"), (m5_bars, "M5")
        ]:
            if tf_bars and len(tf_bars) >= 20:
                ema20_tf = _ema_from_bars(tf_bars, 20)
                if ema20_tf > 0:
                    emas_tf = {"ema20": round(ema20_tf, 5)}
                    if len(tf_bars) >= 200:
                        ema200_tf = _ema_from_bars(tf_bars, 200)
                        if ema200_tf > 0:
                            emas_tf["ema200"] = round(ema200_tf, 5)
                    if len(tf_bars) >= 800:
                        ema800_tf = _ema_from_bars(tf_bars, 800)
                        if ema800_tf > 0:
                            emas_tf["ema800"] = round(ema800_tf, 5)
                    all_tf_emas[tf_name] = emas_tf
        st.tf_emas = all_tf_emas
        # Add condensed multi-TF EMA to reasons
        if all_tf_emas:
            tf_lines = []
            for tf, vals in sorted(all_tf_emas.items(), key=lambda x: ["W1","D1","H4","H1","M15","M5"].index(x[0]) if x[0] in ["W1","D1","H4","H1","M15","M5"] else 99):
                line = f"{tf} EMA20={vals['ema20']:.5f}"
                if 'ema200' in vals:
                    line += f" EMA200={vals['ema200']:.5f}"
                if 'ema800' in vals:
                    line += f" EMA800={vals['ema800']:.5f}"
                tf_lines.append(line)
            st.reasons.append("Multi-TF EMAs: " + " | ".join(tf_lines))

    # Sort by confidence
    setups.sort(key=lambda x: x.confidence, reverse=True)
    return setups



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


def calculate_pivot_points(high: float, low: float, close: float) -> dict:
    """
    Standard and Fibonacci pivot points from the prior period (day/week).

    Standard:  PP = (H+L+C)/3,  R1/R2/R3,  S1/S2/S3
    Fibonacci: PP same, levels use 0.382/0.618/1.0 ratios

    Returns dict with keys: pp, r1, r2, r3, s1, s2, s3,
                             fib_r1, fib_r2, fib_r3, fib_s1, fib_s2, fib_s3
    Filters out zero values.
    """
    if high <= 0 or low <= 0 or close <= 0 or high <= low:
        return {}
    rng = high - low
    pp  = (high + low + close) / 3.0
    r1  = 2 * pp - low
    r2  = pp + rng
    r3  = high + 2 * (pp - low)
    s1  = 2 * pp - high
    s2  = pp - rng
    s3  = low - 2 * (high - pp)

    fib_r1 = pp + rng * 0.382
    fib_r2 = pp + rng * 0.618
    fib_r3 = pp + rng * 1.000
    fib_s1 = pp - rng * 0.382
    fib_s2 = pp - rng * 0.618
    fib_s3 = pp - rng * 1.000

    return {
        "pp":     round(pp, 5),
        "r1":     round(r1, 5),  "r2": round(r2, 5),  "r3": round(r3, 5),
        "s1":     round(s1, 5),  "s2": round(s2, 5),  "s3": round(s3, 5),
        "fib_r1": round(fib_r1, 5), "fib_r2": round(fib_r2, 5), "fib_r3": round(fib_r3, 5),
        "fib_s1": round(fib_s1, 5), "fib_s2": round(fib_s2, 5), "fib_s3": round(fib_s3, 5),
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
# TRIPLE TOP / TRIPLE BOTTOM
# ═══════════════════════════════════════════════════════════════════════════════

def detect_triple_top(bars: list[Bar], tolerance_pct: float = 0.004,
                      min_separation: int = 4) -> Optional[dict]:
    """Triple Top: three peaks at roughly same level → strong SELL signal."""
    if len(bars) < min_separation * 2 + 5:
        return None

    highs = [(i, b.h) for i, b in enumerate(bars[:40])]
    highs.sort(key=lambda x: -x[1])
    if len(highs) < 3:
        return None

    # Find three peaks at similar levels, each separated
    p1_idx, p1_h = highs[0]
    peaks = [(p1_idx, p1_h)]
    for idx, h in highs[1:]:
        if abs(h - p1_h) / p1_h > tolerance_pct:
            continue
        # Must be separated from all existing peaks
        if all(abs(idx - pi) >= min_separation for pi, _ in peaks):
            peaks.append((idx, h))
        if len(peaks) == 3:
            break

    if len(peaks) < 3:
        return None

    peaks.sort(key=lambda x: x[0])
    avg_peak = sum(p[1] for p in peaks) / 3
    lo, hi = min(p[0] for p in peaks), max(p[0] for p in peaks)
    neckline = min(b.l for b in bars[lo:hi + 1])
    pattern_height = avg_peak - neckline
    current = bars[0].c
    breakdown = current <= neckline * 1.005

    return {
        "pattern": "TRIPLE_TOP",
        "direction": "SELL",
        "peaks": [round(p[1], 5) for p in peaks],
        "neckline": round(neckline, 5),
        "target": round(neckline - pattern_height, 5),
        "breakdown": breakdown,
        "confidence_bonus": 20 if breakdown else 12,
        "reason": (f"Triple Top: peaks at {avg_peak:.5g} | "
                   f"neckline {neckline:.5g} | target {neckline - pattern_height:.5g}"),
    }


def detect_triple_bottom(bars: list[Bar], tolerance_pct: float = 0.004,
                         min_separation: int = 4) -> Optional[dict]:
    """Triple Bottom: three troughs at roughly same level → strong BUY signal."""
    if len(bars) < min_separation * 2 + 5:
        return None

    lows = [(i, b.l) for i, b in enumerate(bars[:40]) if b.l > 0]
    lows.sort(key=lambda x: x[1])
    if len(lows) < 3:
        return None

    b1_idx, b1_l = lows[0]
    troughs = [(b1_idx, b1_l)]
    for idx, l in lows[1:]:
        if abs(l - b1_l) / max(b1_l, 0.0001) > tolerance_pct:
            continue
        if all(abs(idx - ti) >= min_separation for ti, _ in troughs):
            troughs.append((idx, l))
        if len(troughs) == 3:
            break

    if len(troughs) < 3:
        return None

    troughs.sort(key=lambda x: x[0])
    avg_low = sum(t[1] for t in troughs) / 3
    lo, hi = min(t[0] for t in troughs), max(t[0] for t in troughs)
    neckline = max(b.h for b in bars[lo:hi + 1])
    pattern_height = neckline - avg_low
    current = bars[0].c
    breakout = current >= neckline * 0.995

    return {
        "pattern": "TRIPLE_BOTTOM",
        "direction": "BUY",
        "troughs": [round(t[1], 5) for t in troughs],
        "neckline": round(neckline, 5),
        "target": round(neckline + pattern_height, 5),
        "breakout": breakout,
        "confidence_bonus": 20 if breakout else 12,
        "reason": (f"Triple Bottom: lows at {avg_low:.5g} | "
                   f"neckline {neckline:.5g} | target {neckline + pattern_height:.5g}"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# FLAGS & PENNANTS
# ═══════════════════════════════════════════════════════════════════════════════

def detect_bull_flag(bars: list[Bar], min_bars: int = 8) -> Optional[dict]:
    """
    Bull Flag: sharp rally (flagpole) followed by downward-sloping consolidation (flag).
    Continuation pattern → BUY signal.
    """
    if len(bars) < min_bars:
        return None
    recent = bars[:min(25, len(bars))]
    n = len(recent)

    # Find the flagpole: a strong up-move in the earlier candles
    # Split into pole (first 40%) and flag (last 60%)
    pole_end = max(3, n * 2 // 5)
    pole_bars = recent[pole_end:]  # older bars (pole)
    flag_bars = recent[:pole_end]  # newer bars (flag)

    if not pole_bars or not flag_bars:
        return None

    pole_low = min(b.l for b in pole_bars)
    pole_high = max(b.h for b in pole_bars)
    pole_range = pole_high - pole_low
    if pole_range <= 0:
        return None

    # Pole must be bullish (close higher than open overall)
    if pole_bars[0].c < pole_bars[-1].o:
        return None

    # Flag should slope downward (lower highs, lower lows) — mild pullback
    flag_highs = [b.h for b in flag_bars]
    flag_lows = [b.l for b in flag_bars]
    flag_range = max(flag_highs) - min(flag_lows)

    # Flag range should be < 50% of pole range (consolidation, not reversal)
    if flag_range > pole_range * 0.50:
        return None

    # Flag should drift downward (negative slope on highs)
    xs = list(range(len(flag_highs)))
    n_f = len(flag_highs)
    if n_f < 2:
        return None
    x_mean = sum(xs) / n_f
    y_mean = sum(flag_highs) / n_f
    den = sum((x - x_mean) ** 2 for x in xs)
    h_slope = (sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, flag_highs)) / den) if den else 0

    if h_slope >= 0:  # Flag should slope down (or flat)
        return None

    target = pole_high + pole_range  # Measured move
    return {
        "pattern": "BULL_FLAG",
        "direction": "BUY",
        "pole_high": round(pole_high, 5),
        "pole_low": round(pole_low, 5),
        "flag_low": round(min(flag_lows), 5),
        "target": round(target, 5),
        "confidence_bonus": 14,
        "reason": (f"Bull Flag: pole {pole_low:.5g}→{pole_high:.5g} | "
                   f"flag consolidating | target {target:.5g}"),
    }


def detect_bear_flag(bars: list[Bar], min_bars: int = 8) -> Optional[dict]:
    """
    Bear Flag: sharp decline (flagpole) followed by upward-sloping consolidation.
    Continuation pattern → SELL signal.
    """
    if len(bars) < min_bars:
        return None
    recent = bars[:min(25, len(bars))]
    n = len(recent)

    pole_end = max(3, n * 2 // 5)
    pole_bars = recent[pole_end:]
    flag_bars = recent[:pole_end]

    if not pole_bars or not flag_bars:
        return None

    pole_low = min(b.l for b in pole_bars)
    pole_high = max(b.h for b in pole_bars)
    pole_range = pole_high - pole_low
    if pole_range <= 0:
        return None

    # Pole must be bearish
    if pole_bars[0].c > pole_bars[-1].o:
        return None

    flag_highs = [b.h for b in flag_bars]
    flag_lows = [b.l for b in flag_bars]
    flag_range = max(flag_highs) - min(flag_lows)

    if flag_range > pole_range * 0.50:
        return None

    xs = list(range(len(flag_lows)))
    n_f = len(flag_lows)
    if n_f < 2:
        return None
    x_mean = sum(xs) / n_f
    y_mean = sum(flag_lows) / n_f
    den = sum((x - x_mean) ** 2 for x in xs)
    l_slope = (sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, flag_lows)) / den) if den else 0

    if l_slope <= 0:  # Flag should drift upward
        return None

    target = pole_low - pole_range
    return {
        "pattern": "BEAR_FLAG",
        "direction": "SELL",
        "pole_high": round(pole_high, 5),
        "pole_low": round(pole_low, 5),
        "flag_high": round(max(flag_highs), 5),
        "target": round(target, 5),
        "confidence_bonus": 14,
        "reason": (f"Bear Flag: pole {pole_high:.5g}→{pole_low:.5g} | "
                   f"flag consolidating | target {target:.5g}"),
    }


def detect_pennant(bars: list[Bar], min_bars: int = 8) -> Optional[dict]:
    """
    Pennant: sharp move followed by converging trendlines (symmetrical triangle).
    Direction depends on the prior move (pole direction).
    """
    if len(bars) < min_bars:
        return None
    recent = bars[:min(25, len(bars))]
    n = len(recent)

    pole_end = max(3, n * 2 // 5)
    pole_bars = recent[pole_end:]
    flag_bars = recent[:pole_end]

    if not pole_bars or len(flag_bars) < 3:
        return None

    pole_low = min(b.l for b in pole_bars)
    pole_high = max(b.h for b in pole_bars)
    pole_range = pole_high - pole_low
    if pole_range <= 0:
        return None

    # Determine pole direction
    pole_bullish = pole_bars[0].c > pole_bars[-1].o

    # Pennant: highs slope down AND lows slope up (converging)
    flag_highs = [b.h for b in flag_bars]
    flag_lows = [b.l for b in flag_bars]
    n_f = len(flag_highs)
    xs = list(range(n_f))
    x_mean = sum(xs) / n_f
    den = sum((x - x_mean) ** 2 for x in xs)
    if den == 0:
        return None

    h_slope = sum((x - x_mean) * (y - sum(flag_highs) / n_f) for x, y in zip(xs, flag_highs)) / den
    l_slope = sum((x - x_mean) * (y - sum(flag_lows) / n_f) for x, y in zip(xs, flag_lows)) / den

    # Highs must fall, lows must rise (converging)
    if h_slope >= 0 or l_slope <= 0:
        return None

    flag_range = max(flag_highs) - min(flag_lows)
    if flag_range > pole_range * 0.50:
        return None

    direction = "BUY" if pole_bullish else "SELL"
    if direction == "BUY":
        target = pole_high + pole_range
    else:
        target = pole_low - pole_range

    return {
        "pattern": "PENNANT",
        "direction": direction,
        "pole_direction": "BULLISH" if pole_bullish else "BEARISH",
        "target": round(target, 5),
        "confidence_bonus": 13,
        "reason": (f"{'Bullish' if pole_bullish else 'Bearish'} Pennant: "
                   f"converging after {'rally' if pole_bullish else 'decline'} | target {target:.5g}"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TRIANGLES (Ascending, Descending, Symmetrical)
# ═══════════════════════════════════════════════════════════════════════════════

def _linear_slope(values: list[float]) -> float:
    """Helper: compute linear regression slope."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(values) / n
    den = sum((x - x_mean) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / den


def detect_ascending_triangle(bars: list[Bar], min_bars: int = 10) -> Optional[dict]:
    """
    Ascending Triangle: flat resistance (highs) + rising lows → bullish breakout.
    """
    if len(bars) < min_bars:
        return None
    recent = bars[:min(25, len(bars))]
    hs = [b.h for b in recent]
    ls = [b.l for b in recent]

    h_slope = _linear_slope(hs)
    l_slope = _linear_slope(ls)

    # Flat highs (near-zero slope), rising lows (positive slope)
    avg_range = sum(b.range for b in recent) / len(recent) if recent else 1
    if abs(h_slope) > avg_range * 0.03:  # Highs must be roughly flat
        return None
    if l_slope <= avg_range * 0.005:  # Lows must be rising
        return None

    resistance = max(hs)
    triangle_height = resistance - min(ls)
    target = resistance + triangle_height
    current = bars[0].c
    breakout = current >= resistance * 0.998

    return {
        "pattern": "ASCENDING_TRIANGLE",
        "direction": "BUY",
        "resistance": round(resistance, 5),
        "target": round(target, 5),
        "breakout": breakout,
        "confidence_bonus": 15 if breakout else 10,
        "reason": (f"Ascending Triangle: flat resistance at {resistance:.5g}, "
                   f"rising lows | target {target:.5g}"),
    }


def detect_descending_triangle(bars: list[Bar], min_bars: int = 10) -> Optional[dict]:
    """
    Descending Triangle: flat support (lows) + falling highs → bearish breakdown.
    """
    if len(bars) < min_bars:
        return None
    recent = bars[:min(25, len(bars))]
    hs = [b.h for b in recent]
    ls = [b.l for b in recent]

    h_slope = _linear_slope(hs)
    l_slope = _linear_slope(ls)

    avg_range = sum(b.range for b in recent) / len(recent) if recent else 1
    if abs(l_slope) > avg_range * 0.03:  # Lows must be roughly flat
        return None
    if h_slope >= -avg_range * 0.005:  # Highs must be falling
        return None

    support = min(ls)
    triangle_height = max(hs) - support
    target = support - triangle_height
    current = bars[0].c
    breakdown = current <= support * 1.002

    return {
        "pattern": "DESCENDING_TRIANGLE",
        "direction": "SELL",
        "support": round(support, 5),
        "target": round(target, 5),
        "breakdown": breakdown,
        "confidence_bonus": 15 if breakdown else 10,
        "reason": (f"Descending Triangle: flat support at {support:.5g}, "
                   f"falling highs | target {target:.5g}"),
    }


def detect_symmetrical_triangle(bars: list[Bar], min_bars: int = 10) -> Optional[dict]:
    """
    Symmetrical Triangle: converging highs (falling) and lows (rising) — bilateral.
    Direction determined by prior trend.
    """
    if len(bars) < min_bars:
        return None
    recent = bars[:min(25, len(bars))]
    hs = [b.h for b in recent]
    ls = [b.l for b in recent]

    h_slope = _linear_slope(hs)
    l_slope = _linear_slope(ls)

    # Highs must fall, lows must rise (converging)
    if h_slope >= 0 or l_slope <= 0:
        return None

    # Check prior trend (bars before the triangle) to determine direction
    pre_bars = bars[len(recent):len(recent) + 10] if len(bars) > len(recent) + 5 else []
    if pre_bars:
        prior_bullish = pre_bars[-1].c < pre_bars[0].c  # price rose into triangle
    else:
        prior_bullish = bars[-1].c < bars[0].c

    direction = "BUY" if prior_bullish else "SELL"
    apex_high = max(hs)
    apex_low = min(ls)
    triangle_height = apex_high - apex_low

    if direction == "BUY":
        target = apex_high + triangle_height * 0.75
    else:
        target = apex_low - triangle_height * 0.75

    return {
        "pattern": "SYMMETRICAL_TRIANGLE",
        "direction": direction,
        "apex_high": round(apex_high, 5),
        "apex_low": round(apex_low, 5),
        "target": round(target, 5),
        "confidence_bonus": 10,
        "reason": (f"Symmetrical Triangle: converging H/L | "
                   f"{'bullish' if direction == 'BUY' else 'bearish'} bias | target {target:.5g}"),
    }


def detect_rectangle(bars: list[Bar], min_bars: int = 10,
                     tolerance_pct: float = 0.005) -> Optional[dict]:
    """
    Rectangle / Range: price bouncing between flat support and flat resistance.
    Breakout direction determines signal.
    """
    if len(bars) < min_bars:
        return None
    recent = bars[:min(25, len(bars))]
    hs = [b.h for b in recent]
    ls = [b.l for b in recent]

    h_slope = _linear_slope(hs)
    l_slope = _linear_slope(ls)
    avg_range = sum(b.range for b in recent) / len(recent) if recent else 1

    # Both highs and lows must be roughly flat
    if abs(h_slope) > avg_range * 0.025 or abs(l_slope) > avg_range * 0.025:
        return None

    resistance = max(hs)
    support = min(ls)
    rect_height = resistance - support
    if rect_height <= 0:
        return None

    current = bars[0].c
    if current >= resistance * (1 - tolerance_pct):
        # Breakout upward
        return {
            "pattern": "RECTANGLE_BREAKOUT",
            "direction": "BUY",
            "resistance": round(resistance, 5),
            "support": round(support, 5),
            "target": round(resistance + rect_height, 5),
            "confidence_bonus": 12,
            "reason": f"Rectangle breakout UP: {support:.5g}–{resistance:.5g} | target {resistance + rect_height:.5g}",
        }
    elif current <= support * (1 + tolerance_pct):
        # Breakdown downward
        return {
            "pattern": "RECTANGLE_BREAKDOWN",
            "direction": "SELL",
            "resistance": round(resistance, 5),
            "support": round(support, 5),
            "target": round(support - rect_height, 5),
            "confidence_bonus": 12,
            "reason": f"Rectangle breakdown DOWN: {support:.5g}–{resistance:.5g} | target {support - rect_height:.5g}",
        }
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# CANDLESTICK PATTERNS
# ═══════════════════════════════════════════════════════════════════════════════

def detect_engulfing(bars: list[Bar]) -> Optional[dict]:
    """
    Bullish Engulfing: bearish candle followed by larger bullish candle that engulfs it.
    Bearish Engulfing: bullish candle followed by larger bearish candle that engulfs it.
    Checks the most recent 2 candles.
    """
    if len(bars) < 2:
        return None

    curr = bars[0]  # most recent
    prev = bars[1]

    # Bullish Engulfing
    if prev.bearish and curr.bullish:
        if curr.o <= prev.c and curr.c >= prev.o and curr.body > prev.body:
            return {
                "pattern": "BULLISH_ENGULFING",
                "direction": "BUY",
                "confidence_bonus": 12,
                "reason": f"Bullish Engulfing: body {curr.body:.5g} engulfs prior {prev.body:.5g}",
            }

    # Bearish Engulfing
    if prev.bullish and curr.bearish:
        if curr.o >= prev.c and curr.c <= prev.o and curr.body > prev.body:
            return {
                "pattern": "BEARISH_ENGULFING",
                "direction": "SELL",
                "confidence_bonus": 12,
                "reason": f"Bearish Engulfing: body {curr.body:.5g} engulfs prior {prev.body:.5g}",
            }
    return None


def detect_pin_bar(bars: list[Bar]) -> Optional[dict]:
    """
    Pin Bar (Hammer/Shooting Star):
    - Bullish pin: small body at top, long lower wick (≥2× body)
    - Bearish pin: small body at bottom, long upper wick (≥2× body)
    """
    if len(bars) < 1:
        return None
    b = bars[0]
    if b.range <= 0:
        return None

    body_pct = b.body / b.range

    # Bullish pin bar (hammer): lower wick ≥ 2× body, upper wick small
    if body_pct < 0.35 and b.lower_wick >= b.body * 2 and b.upper_wick < b.body * 1.2:
        return {
            "pattern": "BULLISH_PIN_BAR",
            "direction": "BUY",
            "lower_wick": round(b.lower_wick, 5),
            "body": round(b.body, 5),
            "confidence_bonus": 10,
            "reason": f"Bullish Pin Bar (Hammer): lower wick {b.lower_wick:.5g} ≥ 2× body {b.body:.5g}",
        }

    # Bearish pin bar (shooting star): upper wick ≥ 2× body, lower wick small
    if body_pct < 0.35 and b.upper_wick >= b.body * 2 and b.lower_wick < b.body * 1.2:
        return {
            "pattern": "BEARISH_PIN_BAR",
            "direction": "SELL",
            "upper_wick": round(b.upper_wick, 5),
            "body": round(b.body, 5),
            "confidence_bonus": 10,
            "reason": f"Bearish Pin Bar (Shooting Star): upper wick {b.upper_wick:.5g} ≥ 2× body {b.body:.5g}",
        }
    return None


def detect_doji(bars: list[Bar]) -> Optional[dict]:
    """
    Doji: open ≈ close (body < 10% of range). Signals indecision / reversal.
    Direction determined by context (prior trend).
    """
    if len(bars) < 3:
        return None
    b = bars[0]
    if b.range <= 0:
        return None

    body_pct = b.body / b.range
    if body_pct >= 0.10:  # Body must be < 10% of range
        return None

    # Determine direction from prior 2 candles
    prior_bull = sum(1 for bb in bars[1:3] if bb.bullish)
    if prior_bull >= 2:
        # Doji after up-move = potential bearish reversal
        direction = "SELL"
        reason_ctx = "after bullish run — potential top"
    elif prior_bull == 0:
        # Doji after down-move = potential bullish reversal
        direction = "BUY"
        reason_ctx = "after bearish run — potential bottom"
    else:
        return None  # Mixed context, skip

    return {
        "pattern": "DOJI",
        "direction": direction,
        "body_pct": round(body_pct * 100, 1),
        "confidence_bonus": 8,
        "reason": f"Doji ({body_pct * 100:.1f}% body) {reason_ctx}",
    }


def detect_morning_evening_star(bars: list[Bar]) -> Optional[dict]:
    """
    Morning Star (bullish): large bearish → small body (star) → large bullish.
    Evening Star (bearish): large bullish → small body (star) → large bearish.
    Checks the 3 most recent bars.
    """
    if len(bars) < 3:
        return None

    c1 = bars[2]  # oldest of the three
    c2 = bars[1]  # middle (star)
    c3 = bars[0]  # newest

    avg_body = (c1.body + c3.body) / 2
    if avg_body <= 0:
        return None

    # Star must have small body (< 40% of average of c1 and c3)
    if c2.body >= avg_body * 0.40:
        return None

    # Morning Star: c1 bearish, c3 bullish, c3 closes above c1 midpoint
    if c1.bearish and c3.bullish:
        c1_mid = (c1.o + c1.c) / 2
        if c3.c >= c1_mid and c1.body > c1.range * 0.4 and c3.body > c3.range * 0.4:
            return {
                "pattern": "MORNING_STAR",
                "direction": "BUY",
                "confidence_bonus": 15,
                "reason": "Morning Star: bearish → indecision → bullish reversal",
            }

    # Evening Star: c1 bullish, c3 bearish, c3 closes below c1 midpoint
    if c1.bullish and c3.bearish:
        c1_mid = (c1.o + c1.c) / 2
        if c3.c <= c1_mid and c1.body > c1.range * 0.4 and c3.body > c3.range * 0.4:
            return {
                "pattern": "EVENING_STAR",
                "direction": "SELL",
                "confidence_bonus": 15,
                "reason": "Evening Star: bullish → indecision → bearish reversal",
            }
    return None


def detect_inside_bar(bars: list[Bar]) -> Optional[dict]:
    """
    Inside Bar: current bar's entire range is within previous bar's range.
    Signals consolidation before breakout — direction from context.
    """
    if len(bars) < 3:
        return None

    curr = bars[0]
    prev = bars[1]

    if curr.h < prev.h and curr.l > prev.l:
        # Inside bar confirmed — direction from prior trend
        prior_trend_bull = sum(1 for bb in bars[2:5] if bb.bullish) > sum(1 for bb in bars[2:5] if bb.bearish)
        direction = "BUY" if prior_trend_bull else "SELL"

        return {
            "pattern": "INSIDE_BAR",
            "direction": direction,
            "mother_high": round(prev.h, 5),
            "mother_low": round(prev.l, 5),
            "confidence_bonus": 8,
            "reason": (f"Inside Bar: range [{curr.l:.5g}–{curr.h:.5g}] inside "
                       f"mother [{prev.l:.5g}–{prev.h:.5g}] — {'bullish' if direction == 'BUY' else 'bearish'} bias"),
        }
    return None


def detect_tweezer(bars: list[Bar], tolerance_pct: float = 0.001) -> Optional[dict]:
    """
    Tweezer Top: two candles with nearly equal highs at a swing high → SELL.
    Tweezer Bottom: two candles with nearly equal lows at a swing low → BUY.
    """
    if len(bars) < 2:
        return None

    curr = bars[0]
    prev = bars[1]

    # Tweezer Top: nearly equal highs, second candle bearish
    if (abs(curr.h - prev.h) / max(curr.h, 0.0001) < tolerance_pct
            and prev.bullish and curr.bearish):
        return {
            "pattern": "TWEEZER_TOP",
            "direction": "SELL",
            "level": round((curr.h + prev.h) / 2, 5),
            "confidence_bonus": 10,
            "reason": f"Tweezer Top at {(curr.h + prev.h) / 2:.5g} — reversal signal",
        }

    # Tweezer Bottom: nearly equal lows, second candle bullish
    if (abs(curr.l - prev.l) / max(curr.l, 0.0001) < tolerance_pct
            and prev.bearish and curr.bullish):
        return {
            "pattern": "TWEEZER_BOTTOM",
            "direction": "BUY",
            "level": round((curr.l + prev.l) / 2, 5),
            "confidence_bonus": 10,
            "reason": f"Tweezer Bottom at {(curr.l + prev.l) / 2:.5g} — reversal signal",
        }
    return None


def detect_three_white_soldiers(bars: list[Bar]) -> Optional[dict]:
    """Three consecutive bullish candles with higher closes and opens → strong BUY."""
    if len(bars) < 3:
        return None
    c1, c2, c3 = bars[2], bars[1], bars[0]  # oldest to newest
    if not (c1.bullish and c2.bullish and c3.bullish):
        return None
    if not (c2.c > c1.c and c3.c > c2.c):
        return None
    if not (c2.o > c1.o and c3.o > c2.o):
        return None
    # Bodies should be substantial
    avg_range = (c1.range + c2.range + c3.range) / 3
    if any(c.body < avg_range * 0.4 for c in [c1, c2, c3]):
        return None
    return {
        "pattern": "THREE_WHITE_SOLDIERS",
        "direction": "BUY",
        "confidence_bonus": 14,
        "reason": "Three White Soldiers: 3 strong bullish candles with rising closes",
    }


def detect_three_black_crows(bars: list[Bar]) -> Optional[dict]:
    """Three consecutive bearish candles with lower closes and opens → strong SELL."""
    if len(bars) < 3:
        return None
    c1, c2, c3 = bars[2], bars[1], bars[0]
    if not (c1.bearish and c2.bearish and c3.bearish):
        return None
    if not (c2.c < c1.c and c3.c < c2.c):
        return None
    if not (c2.o < c1.o and c3.o < c2.o):
        return None
    avg_range = (c1.range + c2.range + c3.range) / 3
    if any(c.body < avg_range * 0.4 for c in [c1, c2, c3]):
        return None
    return {
        "pattern": "THREE_BLACK_CROWS",
        "direction": "SELL",
        "confidence_bonus": 14,
        "reason": "Three Black Crows: 3 strong bearish candles with falling closes",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MASTER PATTERN DETECTOR — runs all patterns at once
# ═══════════════════════════════════════════════════════════════════════════════

def detect_all_patterns(bars: list[Bar], short_bars: list[Bar] = None) -> list[dict]:
    """
    Run all pattern detectors on a set of bars.
    `bars` = H1 (or primary TF), `short_bars` = M15 (optional short-term).
    Returns list of detected pattern dicts.
    """
    patterns = []

    if not bars or len(bars) < 3:
        return patterns

    recent30 = bars[:30]
    recent25 = bars[:25]
    recent20 = bars[:20]

    # Classic Reversal
    for fn in [detect_double_top, detect_double_bottom]:
        r = fn(recent30)
        if r:
            patterns.append(r)
    for fn in [detect_head_shoulders, detect_inverse_head_shoulders]:
        r = fn(recent30)
        if r:
            patterns.append(r)
    for fn in [detect_triple_top, detect_triple_bottom]:
        r = fn(recent30)
        if r:
            patterns.append(r)
    r = detect_wedge(recent20)
    if r:
        patterns.append(r)

    # Continuation
    for fn in [detect_bull_flag, detect_bear_flag, detect_pennant]:
        r = fn(recent25)
        if r:
            patterns.append(r)

    # Triangles
    for fn in [detect_ascending_triangle, detect_descending_triangle,
               detect_symmetrical_triangle]:
        r = fn(recent25)
        if r:
            patterns.append(r)

    # Rectangle
    r = detect_rectangle(recent25)
    if r:
        patterns.append(r)

    # Candlestick (short-term)
    for fn in [detect_engulfing, detect_pin_bar, detect_doji,
               detect_morning_evening_star, detect_inside_bar,
               detect_tweezer, detect_three_white_soldiers,
               detect_three_black_crows]:
        r = fn(bars)
        if r:
            patterns.append(r)

    # Also check short-term bars if provided
    if short_bars and len(short_bars) >= 3:
        for fn in [detect_double_top, detect_double_bottom]:
            r = fn(short_bars[:30])
            if r:
                r["pattern"] = r["pattern"] + "_M15"
                patterns.append(r)
        for fn in [detect_engulfing, detect_pin_bar, detect_morning_evening_star,
                   detect_inside_bar, detect_tweezer]:
            r = fn(short_bars)
            if r:
                r["pattern"] = r["pattern"] + "_M15"
                patterns.append(r)

    return patterns


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
# DISPLACEMENT CANDLE & IOFED MODEL
# ═══════════════════════════════════════════════════════════════════════════════

def detect_displacement_candle(bars: list, direction: str, lookback: int = 10) -> dict:
    """
    Detect the most recent displacement candle in `direction` within `lookback` bars.

    A displacement candle:
    - Has a wide body (body/range > 0.60 — candle is mostly body, little wick)
    - Leaves a Fair Value Gap (the candle's range does not overlap the bar two steps away)
    - Is the strongest such candle (largest body) in the window

    Returns:
        found     : bool — was a displacement candle detected?
        leg_high  : float — high of the displacement candle (used as Fibonacci anchor)
        leg_low   : float — low of the displacement candle
        bar_index : int — position in bars list
        body_pct  : float — body as % of range (0–1)
    """
    null = {"found": False, "leg_high": 0.0, "leg_low": 0.0, "bar_index": -1, "body_pct": 0.0}
    if len(bars) < lookback + 2:
        return null

    best = None
    best_body = 0.0

    for i in range(lookback):
        b = bars[i]
        rng = b.range
        if rng <= 0:
            continue
        bpct = b.body / rng
        if bpct < 0.60:
            continue

        # Direction check
        if direction == "BUY" and not b.bullish:
            continue
        if direction == "SELL" and not b.bearish:
            continue

        # Leaves an FVG: gap between bar[i] and bar[i+2]
        if i + 2 >= len(bars):
            continue
        b2 = bars[i + 2]
        if direction == "BUY":
            has_fvg = b.l > b2.h    # bullish FVG below displacement
        else:
            has_fvg = b.h < b2.l    # bearish FVG above displacement

        if not has_fvg:
            continue

        if b.body > best_body:
            best_body = b.body
            best = {"found": True, "leg_high": b.h, "leg_low": b.l,
                    "bar_index": i, "body_pct": round(bpct, 3)}

    return best if best else null


def analyze_iofed(bars: list, direction: str, fvg_low: float, fvg_high: float) -> dict:
    """
    Institutional Order Flow Entry Drill (IOFED).

    Checks whether a given FVG (or OB) sits within the OTE (Optimal Trade Entry)
    zone of the most recent displacement candle.  This is the A+ confluence factor:
    institutions leave imbalances at the 61.8–79% retracement of their own displacement,
    then return to fill those gaps at precisely those Fibonacci levels.

    OTE for BUY (after a bullish displacement):
        Price retraces 61.8–79% of the displacement leg DOWN → FVG in that zone = optimal.
        ote_low  = leg_high - (leg_high - leg_low) * 0.79
        ote_high = leg_high - (leg_high - leg_low) * 0.618

    OTE for SELL (after a bearish displacement):
        Price retraces 61.8–79% of displacement leg UP → FVG in that zone = optimal.
        ote_low  = leg_low + (leg_high - leg_low) * 0.618
        ote_high = leg_low + (leg_high - leg_low) * 0.79

    Returns:
        iofed      : bool — displacement found and FVG inside OTE
        fvg_in_ote : bool — same as iofed (alias for clarity)
        ote_low    : float
        ote_high   : float
        reason     : str
    """
    disp = detect_displacement_candle(bars, direction)
    null_result = {"iofed": False, "fvg_in_ote": False,
                   "ote_low": 0.0, "ote_high": 0.0, "reason": "No displacement candle found"}

    if not disp["found"]:
        return null_result

    leg_high = disp["leg_high"]
    leg_low  = disp["leg_low"]
    leg_rng  = leg_high - leg_low
    if leg_rng <= 0:
        return null_result

    if direction == "BUY":
        # Retracement into OTE: 61.8–79% down from the high
        ote_high = leg_high - leg_rng * 0.618
        ote_low  = leg_high - leg_rng * 0.79
    else:  # SELL
        # Retracement into OTE: 61.8–79% up from the low
        ote_low  = leg_low + leg_rng * 0.618
        ote_high = leg_low + leg_rng * 0.79

    # Check overlap: FVG overlaps OTE zone
    fvg_in_ote = fvg_low <= ote_high and fvg_high >= ote_low

    reason = (
        f"IOFED: displacement {leg_low:.5g}–{leg_high:.5g} "
        f"({disp['body_pct']*100:.0f}% body) | OTE {ote_low:.5g}–{ote_high:.5g} | "
        f"FVG {fvg_low:.5g}–{fvg_high:.5g} {'IN OTE ✓' if fvg_in_ote else 'outside OTE'}"
    )

    return {
        "iofed":      fvg_in_ote,
        "fvg_in_ote": fvg_in_ote,
        "ote_low":    round(ote_low,  5),
        "ote_high":   round(ote_high, 5),
        "reason":     reason,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPORT / RESISTANCE ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def find_key_levels(bars: list[Bar], tolerance_pct: float = 0.002,
                    min_touches: int = 2, symbol: str = "") -> dict:
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
    round_levels = _get_round_levels(current_price, symbol)

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


def _get_round_levels(price: float, symbol: str = "") -> list[float]:
    """
    Identify nearby psychological round number levels, symbol-aware.
    JPY pairs (100–200 range) use step=1.0 (not 10.0) for key levels like 150.00, 151.00.
    Gold uses step=50 (1800, 1850, 1900). Silver uses step=1.0 or 0.50.
    """
    if price <= 0:
        return []

    sym = symbol.upper()
    # Symbol-specific overrides
    if "XAU" in sym or "GOLD" in sym:
        step = 50.0    # Gold: 1800, 1850, 1900, 1950
    elif "XAG" in sym or "SILVER" in sym:
        step = 0.50    # Silver: 22.50, 23.00, 23.50
    elif "JPY" in sym:
        step = 1.0     # JPY: 149.00, 150.00, 151.00 (not 140, 150, 160)
    elif any(x in sym for x in ("NAS", "US30", "DOW", "SPX", "GER", "UK100", "DAX")):
        step = 500.0   # Indices at 18000–40000: use 500-point psychological levels
    elif price >= 1000:
        step = 50.0    # Crude oil / other commodities
    elif price >= 100:
        step = 10.0
    elif price >= 10:
        step = 1.0
    elif price >= 1:
        step = 0.10
    else:
        step = 0.010

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
                          push_exh: dict = None,
                          eqh_eql: dict = None) -> tuple[int, list[str]]:
    """
    Full enhanced buy setup scoring with all ICT factors.
    Returns (score, extra_reasons).
    """
    score = 35
    reasons = []

    # Base ICT factors — hierarchical TF weighting (W1 > D1 > H4 > session > AMD)
    if d1_bias == "BULLISH":    score += 20; reasons.append("D1 Bias: BULLISH")
    elif d1_bias == "NEUTRAL":  score += 5
    if h4_struct == "BOS_BULLISH":  score += 18; reasons.append("H4 BOS Bullish")
    elif h4_struct == "CHOCH_BULL": score += 15; reasons.append("H4 CHoCH Bullish")
    if amd == "DISTRIBUTION":   score += 20; reasons.append("AMD: Distribution (NY active)")
    if amd == "MANIPULATION":   score += 18; reasons.append("AMD: Manipulation (sweep phase)")
    if amd == "LATE_DIST":      score += 12; reasons.append("AMD: Late Distribution (Silver Bullet)")
    if amd == "ACCUMULATION":   score += 5;  reasons.append("AMD: Accumulation (Asia — prepare)")
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

        # OTE BUY zone: 21–38% of period range (61.8–79% pullback from high)
        pct = quarter_info.get("pct", 50)
        if 21 <= pct <= 38:
            ote_l = quarter_info.get("ote_low", 0)
            ote_h = quarter_info.get("ote_high", 0)
            score += 15
            reasons.append(f"Price in OTE Fibonacci BUY zone ({ote_l:.5g}–{ote_h:.5g}) — highest precision entry")

    # Technical Patterns — capped to prevent score inflation
    # Single pattern cap: 8 pts. Total pattern contribution cap: 16 pts.
    _pat_total_buy = 0
    for p in (patterns or []):
        if isinstance(p, dict) and _pat_total_buy < 16:
            pat_dir = p.get("direction", "")
            bonus   = min(8, p.get("confidence_bonus", 8))
            name    = p.get("pattern", "")
            if pat_dir == "BUY":
                actual = min(bonus, 16 - _pat_total_buy)
                score += actual
                _pat_total_buy += actual
                reasons.append(f"Pattern: {name} (+{actual} confluence)")

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

    # EQH/EQL draw-on-liquidity: resting BSL above = confirms target exists
    if eqh_eql:
        eqh = eqh_eql.get("eqh", [])
        if eqh:
            score += 12
            reasons.append(f"EQH resting BSL above at {eqh[0]:.5g} — draw on liquidity confirmed (+12)")

    return min(score, 100), reasons


def _score_sell_setup_full(d1_bias: str, h4_struct: str, amd: str, session: str,
                           level: float, pdh: float, pwh: float,
                           quarter_info: dict = None,
                           patterns: list = None,
                           sr_info: dict = None,
                           push_exh: dict = None,
                           eqh_eql: dict = None) -> tuple[int, list[str]]:
    """Full enhanced sell setup scoring."""
    score = 35
    reasons = []

    # Base ICT factors — hierarchical TF weighting (W1 > D1 > H4 > session > AMD)
    if d1_bias == "BEARISH":    score += 20; reasons.append("D1 Bias: BEARISH")
    elif d1_bias == "NEUTRAL":  score += 5
    if h4_struct == "BOS_BEARISH":  score += 18; reasons.append("H4 BOS Bearish")
    elif h4_struct == "CHOCH_BEAR": score += 15; reasons.append("H4 CHoCH Bearish")
    if amd == "DISTRIBUTION":   score += 20; reasons.append("AMD: Distribution")
    if amd == "MANIPULATION":   score += 18; reasons.append("AMD: Manipulation")
    if amd == "LATE_DIST":      score += 12; reasons.append("AMD: Late Distribution (Silver Bullet)")
    if amd == "ACCUMULATION":   score += 5;  reasons.append("AMD: Accumulation (Asia — prepare)")
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

        # OTE SELL zone: 62–79% of period range from low (21–38% from high)
        pct = quarter_info.get("pct", 50)
        if 62 <= pct <= 79:
            p_high = quarter_info.get("period_high", 0)
            p_low  = quarter_info.get("period_low", 0)
            if p_high and p_low:
                rng = p_high - p_low
                ote_sell_l = p_high - rng * 0.38
                ote_sell_h = p_high - rng * 0.21
                score += 15
                reasons.append(f"Price in OTE Fibonacci SELL zone ({ote_sell_l:.5g}–{ote_sell_h:.5g}) — highest precision entry")

    _pat_total_sell = 0
    for p in (patterns or []):
        if isinstance(p, dict) and _pat_total_sell < 16:
            pat_dir = p.get("direction", "")
            bonus   = min(8, p.get("confidence_bonus", 8))
            name    = p.get("pattern", "")
            if pat_dir == "SELL":
                actual = min(bonus, 16 - _pat_total_sell)
                score += actual
                _pat_total_sell += actual
                reasons.append(f"Pattern: {name} (+{actual} confluence)")

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

    # EQH/EQL draw-on-liquidity: resting SSL below = confirms target exists
    if eqh_eql:
        eql = eqh_eql.get("eql", [])
        if eql:
            score += 12
            reasons.append(f"EQL resting SSL below at {eql[0]:.5g} — draw on liquidity confirmed (+12)")

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
    LuxAlgo auto-threshold: skip FVGs smaller than 30% of ATR (noise filter).
    """
    fvgs = []
    current_price = bars[0].c if bars else 0
    atr_val = _calc_atr(bars)
    min_size = atr_val * 0.3 if atr_val > 0 else 0  # LuxAlgo cumulative mean threshold
    for i in range(len(bars) - 2):
        b0, b2 = bars[i], bars[i + 2]
        if b0.l > b2.h:
            low  = b2.h
            high = b0.l
            if min_size > 0 and (high - low) < min_size:
                continue  # Filter noise FVG below ATR threshold
            ce   = fvg_consequent_encroachment(low, high)
            if current_price > low:
                if current_price > high:
                    continue
            if current_price > low:
                mitigation_pct = min(100.0, (current_price - low) / (high - low) * 100)
            else:
                mitigation_pct = 0.0
            fvgs.append({
                "type":        "BISI",
                "direction":   "BUY",
                "low":         round(low, 5),
                "high":        round(high, 5),
                "ce":          ce,
                "size":        round(high - low, 5),
                "bar_index":   i,
                "mitigated":   current_price < low,
                "mitigation_pct": round(mitigation_pct, 1),
            })
        if len(fvgs) >= max_fvgs:
            break
    return fvgs


def find_all_bearish_fvgs(bars: list[Bar], max_fvgs: int = 5) -> list[dict]:
    """
    Find all unmitigated bearish FVGs (SIBI — Sellside Imbalance Buyside Inefficiency).
    LuxAlgo auto-threshold: skip FVGs smaller than 30% of ATR (noise filter).
    """
    fvgs = []
    current_price = bars[0].c if bars else 0
    atr_val = _calc_atr(bars)
    min_size = atr_val * 0.3 if atr_val > 0 else 0
    for i in range(len(bars) - 2):
        b0, b2 = bars[i], bars[i + 2]
        if b0.h < b2.l:
            low  = b0.h
            high = b2.l
            if min_size > 0 and (high - low) < min_size:
                continue
            ce   = fvg_consequent_encroachment(low, high)
            if current_price < high:
                if current_price < low:
                    continue
            if current_price < high:
                mitigation_pct = min(100.0, (high - current_price) / (high - low) * 100)
            else:
                mitigation_pct = 0.0
            fvgs.append({
                "type":        "SIBI",
                "direction":   "SELL",
                "low":         round(low, 5),
                "high":        round(high, 5),
                "ce":          ce,
                "size":        round(high - low, 5),
                "bar_index":   i,
                "mitigated":   current_price > high,
                "mitigation_pct": round(mitigation_pct, 1),
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
    if 15 <= gmt_hour < 16:
        return "LONDON_CLOSE"
    if 12 <= gmt_hour < 14:
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


# SMT pairs mirrored from ict_engine so ict_precision is self-contained
_SMT_PAIRS = [
    ("EURUSD", "GBPUSD", "bullish"),
    ("AUDUSD", "NZDUSD", "bullish"),
    ("XAUUSD", "XAGUSD", "bullish"),
    ("EURUSD", "USDCHF", "bearish"),
    ("GBPUSD", "USDCAD", "bearish"),
]


def _check_smt_for_symbol(symbol: str, direction: str, prices_dict: dict) -> bool:
    """
    Return True if any SMT pair involving `symbol` shows divergence aligned with `direction`.
    Uses the same logic as ict_engine._check_smt but returns a simple bool.
    """
    for sym_a, sym_b, correlation in _SMT_PAIRS:
        if symbol not in (sym_a, sym_b):
            continue
        if sym_a not in prices_dict or sym_b not in prices_dict:
            continue
        a = prices_dict[sym_a]
        b = prices_dict[sym_b]
        trend_a = a.get("trend", "NEUTRAL")
        trend_b = b.get("trend", "NEUTRAL")

        if correlation == "bullish":
            # SMT divergence: one bullish, one bearish
            if direction == "BUY":
                if symbol == sym_a and trend_a == "BULLISH" and trend_b == "BEARISH":
                    return True
                if symbol == sym_b and trend_b == "BULLISH" and trend_a == "BEARISH":
                    return True
            else:  # SELL
                if symbol == sym_a and trend_a == "BEARISH" and trend_b == "BULLISH":
                    return True
                if symbol == sym_b and trend_b == "BEARISH" and trend_a == "BULLISH":
                    return True
        elif correlation == "bearish":
            # Inverse pairs: both moving same direction = divergence from expected inverse
            if direction == "SELL":
                if trend_a == "BULLISH" and trend_b == "BULLISH":
                    return True
            if direction == "BUY":
                if trend_a == "BEARISH" and trend_b == "BEARISH":
                    return True
    return False


def _build_confluence(
    symbol:       str,
    direction:    str,        # "BUY" or "SELL"
    d1_bias:      str,
    h4_struct:    str,
    amd_phase:    str,
    gmt_hour:     int,
    gmt_min:      int,
    entry_type:   str,
    liq_swept:    bool,       # True if this is a sweep setup
    h4_eq_highs:  list,       # H4 equal highs (resting BSL)
    h4_eq_lows:   list,       # H4 equal lows  (resting SSL)
    current_price: float,
    fvg_low:      float,
    fvg_high:     float,
    h1_bars:      list,       # For IOFED displacement check
    ltf_confirmed: bool,      # From get_ltf_entry
    prices_dict:  dict,       # Full prices dict from JSON for SMT check
) -> ConfluenceScore:
    """
    Populate all 7 confluence layers for a setup and return a ConfluenceScore.
    """
    c = ConfluenceScore()

    # L1 — HTF bias aligned: D1 and H4 both point in trade direction
    if direction == "BUY":
        c.htf_bias_aligned = (d1_bias == "BULLISH" and h4_struct in ("BOS_BULLISH", "RANGING", "CHOCH_BULL"))
    else:
        c.htf_bias_aligned = (d1_bias == "BEARISH" and h4_struct in ("BOS_BEARISH", "RANGING", "CHOCH_BEAR"))

    # L2 — AMD phase supports trade
    c.amd_phase_aligned = amd_phase in ("DISTRIBUTION", "MANIPULATION")

    # L3 — Kill Zone or Silver Bullet active
    c.killzone_active = bool(get_killzone(gmt_hour)) or bool(is_silver_bullet_window(gmt_hour, gmt_min))

    # L4 — Liquidity swept (external draw on liquidity was taken)
    if liq_swept:
        c.liquidity_swept = True
    elif direction == "BUY":
        # Check if price broke above an H4 equal high (stop hunt of buy stops)
        c.liquidity_swept = any(current_price > lvl for lvl in h4_eq_highs[:5])
    else:
        # Check if price broke below an H4 equal low (stop hunt of sell stops)
        c.liquidity_swept = any(current_price < lvl for lvl in h4_eq_lows[:5])

    # L5 — FVG/OB in OTE (IOFED) — checked on H1 bars for setup zone
    if fvg_low > 0 and fvg_high > 0 and h1_bars:
        iofed = analyze_iofed(h1_bars, direction, fvg_low, fvg_high)
        c.fvg_in_ote = iofed["fvg_in_ote"]
    else:
        c.fvg_in_ote = False

    # L6 — MSS / CHoCH confirmed on LTF (M15 or M5)
    c.mss_confirmed = ltf_confirmed

    # L7 — SMT divergence on a correlated pair
    c.smt_divergence = _check_smt_for_symbol(symbol, direction, prices_dict)

    return c


def scan_all_primary_symbols() -> list[ICTSetup]:
    """Scan all configured symbols and return sorted setups.
    Results are cached per data-file mtime — repeated calls within the same
    MT5 update cycle return instantly without re-scanning.
    """
    data = _load()
    if not data:
        return []

    cur_mtime = _DATA_CACHE["mtime"]
    if cur_mtime and cur_mtime == _SCAN_CACHE["mtime"] and _SCAN_CACHE["result"]:
        return _SCAN_CACHE["result"]

    # Use config watchlist, filtered to symbols actually present in MT5 data
    charts = data.get("charts", {})
    try:
        from config import cfg
        configured = cfg.TRADE_SYMBOLS
    except Exception:
        configured = []
    _FALLBACK = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "AUDUSD", "USDCAD"]
    primary = [s for s in (configured or _FALLBACK) if s in charts]
    if not primary:
        primary = [s for s in _FALLBACK if s in charts]

    # Optional asset-rotation filter — drop symbols that are out-of-session
    # (priority 0). Keeps the rest in their original order. Belt-and-braces:
    # if every symbol gets filtered out, fall back to the unfiltered list so
    # we never silently scan nothing.
    try:
        from asset_rotation_manager import AssetRotationManager
        _arm = AssetRotationManager(symbols=primary)
        _rot = [s for s in primary if not _arm.should_skip_asset(s)]
        if _rot:
            primary = _rot
    except Exception:
        pass  # rotation is opt-in; never block trading

    all_setups = []
    for sym in primary:
        try:
            setups = scan_symbol(sym, data)
            # ── Pivot confidence booster (optional, defensive) ──────────
            # Adds 0–15 confidence points based on price proximity to
            # multi-TF pivot levels with structural confluence.
            if setups:
                try:
                    from pivot_confidence_booster import boost_setup
                    sym_charts = charts.get(sym, {})
                    bars_dict = {
                        tf: _parse_bars(sym_charts.get(tf, []))
                        for tf in ("M5", "M15", "H1", "H4", "D1")
                    }
                    bars_dict = {k: v for k, v in bars_dict.items() if v}
                    if bars_dict:
                        for s in setups:
                            try:
                                boost, reasons, meta = boost_setup(
                                    s, sym, bars_dict, atr=getattr(s, "atr", 0.001) or 0.001,
                                )
                                if boost > 0:
                                    s.confidence = min(100, int(s.confidence) + int(boost))
                                    if hasattr(s, "reasons") and isinstance(s.reasons, list):
                                        s.reasons.extend(reasons)
                                    if hasattr(s, "metadata") and isinstance(s.metadata, dict):
                                        s.metadata.setdefault("pivot", {}).update(meta)
                            except Exception:
                                pass  # one setup failing must not affect others
                except ImportError:
                    pass  # optional; modules may not be present in older installs
                except Exception as e:
                    print(f"[ICT] pivot booster non-fatal error for {sym}: {e}")
            all_setups.extend(setups)
        except Exception as e:
            print(f"[ICT] Error scanning {sym}: {e}")

    all_setups.sort(key=lambda x: x.confidence, reverse=True)
    _SCAN_CACHE["result"] = all_setups
    _SCAN_CACHE["mtime"]  = cur_mtime
    return all_setups


def get_symbol_info(symbol: str) -> dict:
    """Get symbol tick/contract info for lot sizing."""
    data = _load()
    charts = data.get("charts", {})
    return charts.get(symbol, {})
