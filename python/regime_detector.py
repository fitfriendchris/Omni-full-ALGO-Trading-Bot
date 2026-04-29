"""
Market Regime Detector

Classifies the current market into discrete regimes so the bot can switch
parameter profiles. A trending high-volatility market needs different thresholds
than a ranging low-volatility one — fixed parameters bleed in regime drift.

Regimes:
- TRENDING_HIGH_VOL    Strong directional move + ATR > 1.5× 30-day avg
- TRENDING_LOW_VOL     Directional move + ATR ≈ avg
- RANGING_HIGH_VOL     No trend + high ATR (chop, news-driven)
- RANGING_LOW_VOL      No trend + low ATR (quiet, mean-reverting)
- BREAKOUT             ATR spike > 2.0× avg + price breaks recent range
- REVERSAL             Trend exhaustion + counter-trend pattern emerging

Inputs: H1 bars (last 100). The detector is symbol-agnostic — call it per symbol
and aggregate, OR run it on a benchmark like DXY for forex / BTC for crypto.

Public API:
- detect_regime(bars, baseline_atr=None) → RegimeResult
- RegimeResult.regime, .confidence, .features
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import math
import logging

log = logging.getLogger(__name__)


@dataclass
class RegimeResult:
    regime: str
    confidence: float
    features: Dict[str, float] = field(default_factory=dict)
    reason: str = ""


def _atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Simple ATR — Wilder's smoothing."""
    if len(highs) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1]),
        )
        trs.append(tr)
    # Initial ATR = simple mean of first `period` TRs
    atr = sum(trs[:period]) / period
    # Wilder's smoothing for the rest
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _slope(values: List[float]) -> float:
    """Linear regression slope — normalized by mean(values) so it's scale-free."""
    n = len(values)
    if n < 5:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    if mean_y == 0:
        return 0.0
    num = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    raw = num / den
    return raw / mean_y  # Normalized — comparable across symbols


def _adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Average Directional Index (Wilder's). Range [0, 100]."""
    if len(highs) < period * 2:
        return 0.0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(highs)):
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i-1]),
                       abs(lows[i] - closes[i-1])))
    # Smooth with Wilder
    def smooth(seq):
        s = sum(seq[:period])
        out = [s]
        for v in seq[period:]:
            s = s - (s / period) + v
            out.append(s)
        return out
    str_ = smooth(trs)
    pdi = [100 * (p / s) if s else 0 for p, s in zip(smooth(plus_dm), str_)]
    mdi = [100 * (m / s) if s else 0 for m, s in zip(smooth(minus_dm), str_)]
    if not pdi or not mdi:
        return 0.0
    dxs = [100 * abs(p - m) / (p + m) if (p + m) else 0 for p, m in zip(pdi, mdi)]
    if len(dxs) < period:
        return sum(dxs) / max(1, len(dxs))
    # ADX = smoothed DX
    adx = sum(dxs[:period]) / period
    for d in dxs[period:]:
        adx = (adx * (period - 1) + d) / period
    return adx


def _bb_width(closes: List[float], period: int = 20) -> float:
    """Bollinger Band width as fraction of price."""
    if len(closes) < period:
        return 0.0
    window = closes[-period:]
    mean = sum(window) / period
    var = sum((c - mean) ** 2 for c in window) / period
    sd = math.sqrt(var)
    return (sd * 4) / mean if mean else 0.0  # 2σ above + 2σ below = 4σ wide


def detect_regime(
    bars: List[Dict],
    baseline_atr: Optional[float] = None,
) -> RegimeResult:
    """
    Classify the current market regime.

    Args:
        bars: list of dicts with at least {high, low, close} keys.
              Should be H1 timeframe with 80+ bars for stable ADX/ATR.
        baseline_atr: optional 30-day average ATR to compare current ATR against.
                      If None, uses a window from earlier in the same bars (less ideal).

    Returns: RegimeResult with regime label, confidence 0-1, and feature dict.
    """
    if not bars or len(bars) < 30:
        return RegimeResult("UNKNOWN", 0.0, reason="not enough bars")

    highs = [float(b["high"]) for b in bars]
    lows  = [float(b["low"])  for b in bars]
    closes = [float(b["close"]) for b in bars]

    atr_now = _atr(highs[-30:], lows[-30:], closes[-30:], period=14)
    if baseline_atr is None or baseline_atr <= 0:
        # Use earlier window as baseline if not provided
        if len(bars) >= 60:
            baseline_atr = _atr(highs[-90:-30] if len(bars) >= 90 else highs[:-30],
                               lows[-90:-30]  if len(bars) >= 90 else lows[:-30],
                               closes[-90:-30] if len(bars) >= 90 else closes[:-30],
                               period=14)
        else:
            baseline_atr = atr_now
    if baseline_atr <= 0:
        baseline_atr = atr_now or 0.0001

    atr_ratio = atr_now / baseline_atr if baseline_atr > 0 else 1.0
    slope = _slope(closes[-50:]) * 100  # express as %
    adx = _adx(highs[-50:], lows[-50:], closes[-50:])
    bbw = _bb_width(closes[-50:])

    features = {
        "atr_now":      round(atr_now, 6),
        "atr_baseline": round(baseline_atr, 6),
        "atr_ratio":    round(atr_ratio, 3),
        "slope_pct":    round(slope, 4),
        "adx":          round(adx, 2),
        "bb_width":     round(bbw, 5),
    }

    # ── Classification logic ──────────────────────────────────────────
    # Volatility tier
    high_vol = atr_ratio >= 1.4
    low_vol  = atr_ratio <= 0.7
    # Trend tier — combine ADX + slope
    trending = adx >= 25 and abs(slope) >= 0.05
    breakout = atr_ratio >= 2.0 and adx >= 20

    if breakout:
        regime, conf, reason = "BREAKOUT", min(1.0, atr_ratio / 2.5), \
            f"ATR spike {atr_ratio:.2f}× + ADX {adx:.0f}"
    elif trending and high_vol:
        regime, conf, reason = "TRENDING_HIGH_VOL", min(1.0, (adx / 50) + 0.3), \
            f"ADX {adx:.0f}, slope {slope:+.2f}%, ATR {atr_ratio:.2f}×"
    elif trending and not high_vol:
        regime, conf, reason = "TRENDING_LOW_VOL", min(1.0, adx / 50), \
            f"ADX {adx:.0f}, slope {slope:+.2f}%, vol normal"
    elif high_vol:
        regime, conf, reason = "RANGING_HIGH_VOL", min(1.0, atr_ratio / 2.0), \
            f"ATR {atr_ratio:.2f}× but ADX only {adx:.0f} — chop"
    elif low_vol:
        regime, conf, reason = "RANGING_LOW_VOL", min(1.0, 1.0 - atr_ratio), \
            f"ATR {atr_ratio:.2f}×, ADX {adx:.0f} — quiet"
    else:
        regime, conf, reason = "RANGING_LOW_VOL", 0.5, "default — no strong signal"

    # Reversal overlay — if trending but slope just flipped
    if len(closes) >= 20:
        recent_slope = _slope(closes[-10:]) * 100
        if trending and (slope * recent_slope < 0) and abs(recent_slope) > 0.03:
            regime = "REVERSAL"
            conf = min(1.0, abs(recent_slope) * 5)
            reason = f"Trend slope flipping: prior {slope:+.2f}% → recent {recent_slope:+.2f}%"

    return RegimeResult(regime=regime, confidence=conf, features=features, reason=reason)
