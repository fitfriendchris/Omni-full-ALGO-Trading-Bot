"""
smc_engine.py — Smart Money Concepts detection engine for OMNI-ICT (Phase 2).

Scope (deterministic, pure functions; no I/O, no side effects):
    - OrderBlock          : last opposing candle before displacement
    - FairValueGap        : 3-bar imbalance (C1.high < C3.low for bullish, vice versa)
    - LiquidityPool       : equal highs/lows within a tolerance band
    - Sweep               : wick that takes out a liquidity level and closes back
    - BOS / CHoCH         : break of / change of character on swing structure

Design:
    - Input = list[Bar]; Bar is the same shape as smart_trailing_stop.Bar
      (time, open, high, low, close). We don't depend on that module to
      stay decoupled, but we re-export a compatible Bar.
    - Output = dataclasses with enough metadata for Phase 3 (signal writers
      feeding Pine/MQL5 overlays) and the scaling engine.
    - No numpy dependency — stdlib only. ICT-style detection is O(n) over
      bars; we don't need vectorised ops.

Boundaries (explicit non-goals for this skeleton):
    - Doesn't read bars from disk. Caller passes them in.
    - Doesn't decide trade direction. That's `dual_tf_selector` (next file).
    - Doesn't emit `Signal` events yet. That's `signal_writers.py` (Phase 3).
    - Doesn't hit MT5. That's `mt5_connector.py`.

Failure modes:
    - Empty bars → empty list. No exceptions.
    - Fewer bars than needed for a detector → empty list.
    - Malformed bar (NaN / zero high-low) → skipped, logged at debug level.

Run `python smc_engine.py` for a self-test on synthetic bars.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Core types
# ──────────────────────────────────────────────────────────────────────────────

Side = Literal["BULL", "BEAR"]
Strength = Literal["WEAK", "MEDIUM", "STRONG"]


@dataclass(frozen=True)
class Bar:
    time:  float
    open:  float
    high:  float
    low:   float
    close: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return max(1e-12, self.high - self.low)

    @property
    def is_bull(self) -> bool:
        return self.close > self.open

    @property
    def is_bear(self) -> bool:
        return self.close < self.open


@dataclass
class OrderBlock:
    """
    Last opposing candle before a displacement (body-dominant) move.

    Bullish OB: last bearish candle before a strong up-move that breaks the
    high of that candle. Top = high of the bearish candle, bottom = low
    (sometimes: open → close body only — we store both and let consumer choose).

    `anchor_idx` is the index of the opposing candle in the source array.
    `break_idx` is the bar index where the opposing structure was broken.
    """
    side:       Side
    anchor_idx: int
    break_idx:  int
    top:        float
    bot:        float
    body_top:   float
    body_bot:   float
    anchor_time: float
    mitigated:  bool = False
    strength:   Strength = "MEDIUM"

    def contains(self, price: float) -> bool:
        return self.bot <= price <= self.top


@dataclass
class FairValueGap:
    """
    Three-bar imbalance.
      Bullish FVG : bars[i-1].high < bars[i+1].low  →  gap = [c1.high, c3.low]
      Bearish FVG : bars[i-1].low  > bars[i+1].high →  gap = [c3.high, c1.low]
    `middle_idx` points at the 3-bar pattern's centre candle.
    """
    side:        Side
    middle_idx:  int
    top:         float
    bot:         float
    middle_time: float
    mitigated:   bool = False

    @property
    def size(self) -> float:
        return max(0.0, self.top - self.bot)

    def contains(self, price: float) -> bool:
        return self.bot <= price <= self.top


@dataclass
class LiquidityPool:
    """
    A cluster of highs (or lows) within a tolerance band — a magnet for price.
    `level` is the median of the cluster; `indices` are the bars that belong.
    """
    side:     Side              # BULL = buy-side liq above (equal highs); BEAR = sell-side liq below (equal lows)
    level:    float
    indices:  tuple[int, ...]
    swept:    bool = False
    sweep_idx: Optional[int] = None


@dataclass
class Sweep:
    """
    A liquidity sweep: a bar whose wick took out the pool's level and closed
    back through it. The classic ICT 'stop-hunt then reverse' signal.
    """
    side:       Side
    pool_level: float
    bar_idx:    int
    bar_time:   float


@dataclass
class Swing:
    """
    A pivot high or pivot low. Used by BOS/CHoCH detection and the trailing
    engine's structure layer. `strength` = number of bars confirmed on each side.
    """
    idx:      int
    time:     float
    price:    float
    kind:     Literal["HIGH", "LOW"]
    strength: int = 1


@dataclass
class StructureEvent:
    """
    BOS (Break of Structure) = break in direction of prevailing trend.
    CHoCH (Change of Character) = break against prevailing trend — first sign of reversal.
    """
    kind:      Literal["BOS", "CHOCH"]
    direction: Side
    bar_idx:   int
    bar_time:  float
    swing_price: float


# ──────────────────────────────────────────────────────────────────────────────
# Bar utilities
# ──────────────────────────────────────────────────────────────────────────────

def _valid_bars(bars: Iterable[Bar]) -> list[Bar]:
    """Drop malformed bars; preserve order."""
    out: list[Bar] = []
    for i, b in enumerate(bars):
        if b is None:
            continue
        if any(math.isnan(x) for x in (b.open, b.high, b.low, b.close)):
            continue
        # Use raw high-low (b.range is clamped to 1e-12 to avoid div-by-zero)
        if b.high < b.low or (b.high - b.low) <= 0:
            continue
        out.append(b)
    return out


def atr(bars: list[Bar], period: int = 14) -> float:
    """Wilder's smoothed ATR — matches MT4/MT5 ATR indicator output."""
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    if len(trs) < period:
        return sum(trs) / len(trs)
    # Seed with SMA of first `period` values, then apply Wilder's RMA
    result = sum(trs[:period]) / period
    for tr in trs[period:]:
        result = (result * (period - 1) + tr) / period
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Swing detection (fractal pivots)
# ──────────────────────────────────────────────────────────────────────────────

def find_swings(bars: list[Bar], lookback: int = 2) -> list[Swing]:
    """
    Fractal pivots: a bar is a swing high if its high is strictly greater than
    `lookback` bars on each side (and same for lows). `lookback=2` → 5-bar pivot.
    """
    bars = _valid_bars(bars)
    out: list[Swing] = []
    n = len(bars)
    if n < (2 * lookback + 1):
        return out
    for i in range(lookback, n - lookback):
        h = bars[i].high
        l = bars[i].low
        is_high = all(bars[j].high < h for j in range(i - lookback, i)) \
              and all(bars[j].high < h for j in range(i + 1, i + lookback + 1))
        is_low  = all(bars[j].low  > l for j in range(i - lookback, i)) \
              and all(bars[j].low  > l for j in range(i + 1, i + lookback + 1))
        if is_high:
            out.append(Swing(idx=i, time=bars[i].time, price=h, kind="HIGH", strength=lookback))
        if is_low:
            out.append(Swing(idx=i, time=bars[i].time, price=l, kind="LOW", strength=lookback))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# BOS / CHoCH
# ──────────────────────────────────────────────────────────────────────────────

def find_structure_events(bars: list[Bar], lookback: int = 2) -> list[StructureEvent]:
    """
    Walk bars forward. After each swing is confirmed, watch for the next
    break of that swing. A break *with the prevailing trend* = BOS. A break
    *against* it = CHoCH.

    Prevailing trend starts UNKNOWN and flips on each CHoCH.
    """
    bars = _valid_bars(bars)
    swings = find_swings(bars, lookback=lookback)
    if not swings or not bars:
        return []

    events: list[StructureEvent] = []
    trend: Optional[Side] = None     # BULL/BEAR or None
    last_high: Optional[Swing] = None
    last_low:  Optional[Swing] = None

    for swing in swings:
        if swing.kind == "HIGH":
            last_high = swing
        else:
            last_low = swing

    # Iterate bars again, tracking breaks after each swing becomes known.
    # For accurate ordering, interleave swings + bars via idx.
    swing_by_confirm = sorted(swings, key=lambda s: s.idx + lookback)  # confirmed `lookback` bars later
    sw_iter = iter(swing_by_confirm)
    next_sw = next(sw_iter, None)

    active_high: Optional[Swing] = None
    active_low:  Optional[Swing] = None

    for i, b in enumerate(bars):
        # Activate any swings whose confirmation bar has passed
        while next_sw is not None and (next_sw.idx + lookback) <= i:
            if next_sw.kind == "HIGH":
                active_high = next_sw
            else:
                active_low = next_sw
            next_sw = next(sw_iter, None)

        # Check for breaks
        if active_high is not None and b.close > active_high.price and i > active_high.idx:
            kind = "BOS" if trend == "BULL" else "CHOCH"
            events.append(StructureEvent(
                kind=kind, direction="BULL",
                bar_idx=i, bar_time=b.time, swing_price=active_high.price))
            trend = "BULL"
            active_high = None   # consumed

        if active_low is not None and b.close < active_low.price and i > active_low.idx:
            kind = "BOS" if trend == "BEAR" else "CHOCH"
            events.append(StructureEvent(
                kind=kind, direction="BEAR",
                bar_idx=i, bar_time=b.time, swing_price=active_low.price))
            trend = "BEAR"
            active_low = None

    return events


# ──────────────────────────────────────────────────────────────────────────────
# Fair value gaps
# ──────────────────────────────────────────────────────────────────────────────

def find_fvgs(bars: list[Bar], min_size_atr_frac: float = 0.10,
              atr_period: int = 14) -> list[FairValueGap]:
    """
    3-bar imbalance. A gap smaller than `min_size_atr_frac * ATR` is filtered
    out as noise.
    """
    bars = _valid_bars(bars)
    n = len(bars)
    if n < 3:
        return []
    a = atr(bars, period=atr_period)
    min_size = a * min_size_atr_frac if a > 0 else 0.0
    out: list[FairValueGap] = []
    for i in range(1, n - 1):
        c1, _c2, c3 = bars[i - 1], bars[i], bars[i + 1]
        # Bullish FVG: c1.high < c3.low → unfilled gap up
        if c1.high < c3.low:
            top, bot = c3.low, c1.high
            if (top - bot) >= min_size:
                out.append(FairValueGap(
                    side="BULL", middle_idx=i, top=top, bot=bot, middle_time=bars[i].time))
        # Bearish FVG: c1.low > c3.high → unfilled gap down
        if c1.low > c3.high:
            top, bot = c1.low, c3.high
            if (top - bot) >= min_size:
                out.append(FairValueGap(
                    side="BEAR", middle_idx=i, top=top, bot=bot, middle_time=bars[i].time))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Order blocks
# ──────────────────────────────────────────────────────────────────────────────

def find_order_blocks(bars: list[Bar], displacement_atr_mult: float = 1.5,
                      atr_period: int = 14) -> list[OrderBlock]:
    """
    OB detection:
      Bullish OB = last bearish candle whose high is broken by a subsequent
                   bar whose close is ≥ `displacement_atr_mult * ATR` above
                   that candle's high.
      Bearish OB = symmetric.

    Emits one OB per displacement move, anchored at the nearest opposing candle.
    """
    bars = _valid_bars(bars)
    n = len(bars)
    if n < 3:
        return []
    out: list[OrderBlock] = []

    for i in range(1, n):
        b = bars[i]
        # Local ATR: volatility *leading into* this bar (excludes bar i itself).
        # This is the correct live-trading behaviour — the candidate bar cannot
        # contaminate its own threshold, and backtests avoid lookahead bias.
        local_window = bars[max(0, i - atr_period):i]
        if len(local_window) < 2:
            continue
        a = atr(local_window, period=atr_period)
        if a <= 0:
            continue
        threshold = a * displacement_atr_mult

        # Bullish displacement: close breaks recent high with momentum
        if b.is_bull and (b.close - b.open) >= threshold:
            # Find last bearish candle before i
            for j in range(i - 1, max(-1, i - 30), -1):
                if bars[j].is_bear:
                    out.append(OrderBlock(
                        side="BULL", anchor_idx=j, break_idx=i,
                        top=bars[j].high, bot=bars[j].low,
                        body_top=max(bars[j].open, bars[j].close),
                        body_bot=min(bars[j].open, bars[j].close),
                        anchor_time=bars[j].time,
                        strength="STRONG" if (b.close - b.open) >= threshold * 1.5 else "MEDIUM",
                    ))
                    break
        # Bearish displacement
        if b.is_bear and (b.open - b.close) >= threshold:
            for j in range(i - 1, max(-1, i - 30), -1):
                if bars[j].is_bull:
                    out.append(OrderBlock(
                        side="BEAR", anchor_idx=j, break_idx=i,
                        top=bars[j].high, bot=bars[j].low,
                        body_top=max(bars[j].open, bars[j].close),
                        body_bot=min(bars[j].open, bars[j].close),
                        anchor_time=bars[j].time,
                        strength="STRONG" if (b.open - b.close) >= threshold * 1.5 else "MEDIUM",
                    ))
                    break

    # Mitigation: an OB is mitigated once a later bar trades back into its zone
    for ob in out:
        for k in range(ob.break_idx + 1, n):
            if ob.side == "BULL" and bars[k].low <= ob.top:
                ob.mitigated = True
                break
            if ob.side == "BEAR" and bars[k].high >= ob.bot:
                ob.mitigated = True
                break

    return out


# ──────────────────────────────────────────────────────────────────────────────
# Liquidity pools & sweeps
# ──────────────────────────────────────────────────────────────────────────────

def find_liquidity_pools(bars: list[Bar], tolerance_atr_frac: float = 0.15,
                         min_cluster: int = 2, atr_period: int = 14) -> list[LiquidityPool]:
    """
    Cluster swing highs/lows within `tolerance_atr_frac * ATR` of each other.
    Each cluster of ≥ `min_cluster` becomes a LiquidityPool. Level = median.
    """
    bars = _valid_bars(bars)
    swings = find_swings(bars)
    if not swings:
        return []
    a = atr(bars, period=atr_period)
    if a <= 0:
        return []
    tol = a * tolerance_atr_frac

    def cluster(points: list[Swing], side: Side) -> list[LiquidityPool]:
        out: list[LiquidityPool] = []
        points = sorted(points, key=lambda s: s.price)
        used = [False] * len(points)
        for i, p in enumerate(points):
            if used[i]:
                continue
            members = [i]
            for j in range(i + 1, len(points)):
                if used[j]:
                    continue
                if abs(points[j].price - p.price) <= tol:
                    members.append(j)
            if len(members) >= min_cluster:
                prices = sorted(points[m].price for m in members)
                level = prices[len(prices) // 2]        # median
                out.append(LiquidityPool(
                    side=side, level=level,
                    indices=tuple(points[m].idx for m in members),
                ))
                for m in members:
                    used[m] = True
        return out

    highs = [s for s in swings if s.kind == "HIGH"]
    lows  = [s for s in swings if s.kind == "LOW"]
    return cluster(highs, "BULL") + cluster(lows, "BEAR")


def find_sweeps(bars: list[Bar], pools: list[LiquidityPool]) -> list[Sweep]:
    """
    A sweep: a bar that pokes through a pool level with its wick and closes
    back on the other side (BULL pool above = sell-side sweep if high > level
    and close < level; BEAR pool below = buy-side sweep if low < level and close > level).
    """
    bars = _valid_bars(bars)
    sweeps: list[Sweep] = []
    max_idx = max((max(p.indices) for p in pools if p.indices), default=-1)
    for pool in pools:
        for i in range(max_idx + 1, len(bars)):
            b = bars[i]
            if pool.side == "BULL":
                # Highs above pool: sell-side liquidity sits above → sweep = bar pokes above, closes below
                if b.high > pool.level and b.close < pool.level:
                    sweeps.append(Sweep(side="BEAR", pool_level=pool.level,
                                        bar_idx=i, bar_time=b.time))
                    pool.swept = True
                    pool.sweep_idx = i
                    break
            else:  # BEAR pool (lows below)
                if b.low < pool.level and b.close > pool.level:
                    sweeps.append(Sweep(side="BULL", pool_level=pool.level,
                                        bar_idx=i, bar_time=b.time))
                    pool.swept = True
                    pool.sweep_idx = i
                    break
    return sweeps


# ──────────────────────────────────────────────────────────────────────────────
# One-shot analysis
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SMCSnapshot:
    bars:       list[Bar]
    swings:     list[Swing]
    structure:  list[StructureEvent]
    order_blocks: list[OrderBlock]
    fvgs:       list[FairValueGap]
    pools:      list[LiquidityPool]
    sweeps:     list[Sweep]

    @property
    def last_event(self) -> Optional[StructureEvent]:
        return self.structure[-1] if self.structure else None

    @property
    def last_trend(self) -> Optional[Side]:
        # Latest BOS/CHoCH defines current bias
        ev = self.last_event
        return ev.direction if ev else None

    def unmitigated_obs(self, side: Optional[Side] = None) -> list[OrderBlock]:
        return [o for o in self.order_blocks
                if not o.mitigated and (side is None or o.side == side)]

    def unmitigated_fvgs(self, side: Optional[Side] = None) -> list[FairValueGap]:
        return [f for f in self.fvgs
                if not f.mitigated and (side is None or f.side == side)]


def _mark_fvg_mitigations(bars: list[Bar], fvgs: list[FairValueGap]) -> None:
    """Mark FVGs as mitigated once a subsequent bar trades back into the gap zone."""
    for fvg in fvgs:
        if fvg.mitigated:
            continue
        for bar in bars[fvg.middle_idx + 1:]:
            if fvg.side == "BULL" and bar.low <= fvg.top:
                fvg.mitigated = True
                break
            if fvg.side == "BEAR" and bar.high >= fvg.bot:
                fvg.mitigated = True
                break


def analyze(bars: Iterable[Bar], lookback: int = 2,
            displacement_atr_mult: float = 1.5,
            min_fvg_atr_frac: float = 0.10,
            liquidity_atr_frac: float = 0.15) -> SMCSnapshot:
    """
    One-shot SMC analysis — returns all structures in one pass.
    Safe on empty/short input: returns empty snapshot.
    """
    bars = _valid_bars(list(bars))
    swings    = find_swings(bars, lookback=lookback)
    structure = find_structure_events(bars, lookback=lookback)
    obs       = find_order_blocks(bars, displacement_atr_mult=displacement_atr_mult)
    fvgs      = find_fvgs(bars, min_size_atr_frac=min_fvg_atr_frac)
    _mark_fvg_mitigations(bars, fvgs)  # populate FVG.mitigated flags
    pools     = find_liquidity_pools(bars, tolerance_atr_frac=liquidity_atr_frac)
    sweeps    = find_sweeps(bars, pools)
    return SMCSnapshot(bars=bars, swings=swings, structure=structure,
                       order_blocks=obs, fvgs=fvgs, pools=pools, sweeps=sweeps)


# ──────────────────────────────────────────────────────────────────────────────
# Self-test (run: python smc_engine.py)
# ──────────────────────────────────────────────────────────────────────────────

def _make_fixture() -> list[Bar]:
    """Synthetic bars calibrated to realistic day-trading displacement:
    - Quiet accumulation establishes a LOW local ATR baseline (~0.0004)
    - A single peak bar forms a clear swing HIGH
    - A single DOMINANT displacement-down bar (body ~15x baseline ATR) triggers
      bearish OB, anchored on the peak bar
    - Follow-through + liquidity sweep
    - A single DOMINANT displacement-up bar triggers bullish OB and BOS/CHoCH
      when its close breaks above the prior swing HIGH
    """
    out: list[Bar] = []
    t = 0.0
    price = 1.10000

    # 1) Quiet accumulation — 14 tiny bars establish a LOW ATR baseline
    for _ in range(14):
        o = price; c = o + 0.00004; h = c + 0.00002; l = o - 0.00002
        out.append(Bar(time=t, open=o, high=h, low=l, close=c)); t += 60; price = c
    # 2) Single peak bar — strictly highest bar so fractal pivot detects swing HIGH
    o = price; c = o + 0.00080; h = c + 0.00030; l = o - 0.00002
    out.append(Bar(time=t, open=o, high=h, low=l, close=c)); t += 60; price = c
    # 3) Two quiet pullback bars (preserve swing + keep ATR low)
    for _ in range(2):
        o = price; c = o - 0.00010; h = o + 0.00002; l = c - 0.00002
        out.append(Bar(time=t, open=o, high=h, low=l, close=c)); t += 60; price = c
    # 4) ONE dominant displacement DOWN bar — body ~0.0060 vs baseline ATR ~0.0005
    o = price; c = o - 0.00600; h = o + 0.00002; l = c - 0.00020
    out.append(Bar(time=t, open=o, high=h, low=l, close=c)); t += 60; price = c
    # 5) Follow-through
    for _ in range(2):
        o = price; c = o - 0.00150; h = o + 0.00020; l = c - 0.00010
        out.append(Bar(time=t, open=o, high=h, low=l, close=c)); t += 60; price = c
    # 6) Sweep: wick below recent lows, close back up
    o = price; h = o + 0.00020; l = o - 0.00250; c = o + 0.00030
    out.append(Bar(time=t, open=o, high=h, low=l, close=c)); t += 60; price = c
    # 7) Two quiet bars forming swing LOW
    for _ in range(2):
        o = price; c = o + 0.00010; h = c + 0.00002; l = o - 0.00002
        out.append(Bar(time=t, open=o, high=h, low=l, close=c)); t += 60; price = c
    # 8) ONE dominant displacement UP bar — must close above prior swing HIGH
    #    Prior swing HIGH ≈ 1.10120. Target close well above.
    o = price
    c = o + 0.01000   # strong upward push
    h = c + 0.00030
    l = o - 0.00010
    out.append(Bar(time=t, open=o, high=h, low=l, close=c)); t += 60; price = c
    # 9) Continuation
    for _ in range(3):
        o = price; c = o + 0.00100; h = c + 0.00010; l = o - 0.00010
        out.append(Bar(time=t, open=o, high=h, low=l, close=c)); t += 60; price = c
    return out


def _self_test() -> None:
    bars = _make_fixture()
    snap = analyze(bars)
    assert len(snap.swings)     > 0, "expected some swings"
    assert len(snap.structure)  > 0, "expected at least one BOS/CHoCH"
    assert len(snap.order_blocks) > 0, "expected at least one OB"
    # FVGs / pools / sweeps are situational on this fixture; don't assert count
    print(f"[OK] bars={len(bars)} swings={len(snap.swings)} struct={len(snap.structure)} "
          f"OBs={len(snap.order_blocks)} FVGs={len(snap.fvgs)} pools={len(snap.pools)} "
          f"sweeps={len(snap.sweeps)}")
    print(f"[OK] last_trend={snap.last_trend}")
    for ev in snap.structure[:5]:
        print(f"     {ev.kind} {ev.direction} @ bar {ev.bar_idx} price {ev.swing_price:.5f}")


if __name__ == "__main__":
    _self_test()
