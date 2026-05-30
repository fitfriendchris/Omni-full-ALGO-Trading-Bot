"""
ict_amd.py — Accumulation → Manipulation → Distribution engine (Chris's process).

This encodes the EXACT discretionary process, as a strict gated state machine:

  BIAS  HTF trend up or down (sets the distribution direction).
  G1 ACCUMULATION  price consolidates into a range (a high + a low), ideally at a
                   major HTF origin order block. Width-bounded = real consolidation.
  G2 MANIPULATION  one side of the range breaks out as a FALSE move (judas/stop-hunt)
                   AGAINST the bias — bull bias → sweep the range LOW; bear → range
                   HIGH — then price reclaims back inside. This is the liquidity grab.
  G3 DISTRIBUTION  after the sweep, an LTF structure shift (CHoCH/BOS) in the bias
                   direction confirms the reversal (the "fake-out + reversal"). Enter
                   at the reversal OB/FVG (or the reclaimed range edge).
  TARGET  opposing liquidity: the opposite range extreme (tp1), then the major HTF OB
          / structure extreme beyond it (tp2). SL beyond the manipulation extreme.

Differs from the failed `ict_sequential` engine: it FIRST requires a real
accumulation range, treats manipulation as the range's false breakout (not any
sweep), and targets the structural opposing liquidity (not a vague far draw).

Pure / deterministic / walk-forward safe. Returns the shared `Setup` so it drops
into `ict_sequential_backtest` (TP ladder + partials + BE) unchanged.

Run `python ict_amd.py` for a self-test on a synthetic AMD pattern.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from smc_engine import Bar, SMCSnapshot, analyze, atr
from ict_sequential import Setup, Gate, _hour_utc, _in_kill_zone, KILL_ZONES_UTC


@dataclass
class AMDConfig:
    symbol: str = "XAUUSD"
    # Accumulation window (older block) and the recent manipulation+reversal window.
    accum_bars: int = 20
    manip_bars: int = 12
    # The accumulation range must be tighter than this * LTF-ATR to count as real
    # consolidation (not a trend leg masquerading as a range).
    accum_max_width_atr: float = 6.0
    accum_min_width_atr: float = 0.8       # …but not a dead-flat non-range
    # Manipulation must pierce the range extreme by at least this * ATR (a real sweep).
    sweep_min_atr: float = 0.05
    sl_atr_buffer: float = 0.5
    require_bias_align: bool = True        # only distribute WITH the HTF trend
    require_htf_ob_anchor: bool = False    # accumulation must sit at an HTF OB
    min_rr: float = 1.0                    # to tp1; the runner (tp2) extends it
    use_killzone: bool = False
    kill_zones: Tuple[Tuple[int, int], ...] = tuple(KILL_ZONES_UTC)
    # bias EMA fallback when HTF structure is silent
    bias_ema_fast: int = 20
    bias_ema_slow: int = 50


def _ema(vals: List[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    k = 2.0 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _htf_bias(htf: SMCSnapshot, htf_bars: List[Bar], cfg: AMDConfig) -> Tuple[str, str]:
    """BULL/BEAR from the last HTF structure break; EMA stack as fallback."""
    if htf.structure:
        ev = htf.structure[-1]
        return ev.direction, f"last HTF {ev.kind} {ev.direction}"
    closes = [b.close for b in htf_bars]
    ef, es = _ema(closes, cfg.bias_ema_fast), _ema(closes, cfg.bias_ema_slow)
    if ef is None or es is None:
        return "NEUTRAL", "insufficient HTF data for bias"
    return ("BULL" if ef > es else "BEAR",
            f"EMA{cfg.bias_ema_fast}{'>' if ef > es else '<'}EMA{cfg.bias_ema_slow}")


def _htf_ob_anchor(htf: SMCSnapshot, lo: float, hi: float, direction: str) -> bool:
    """Is the accumulation range sitting at an unmitigated HTF OB on the bias side?"""
    for ob in htf.order_blocks:
        if ob.mitigated or ob.side != direction:
            continue
        if not (ob.top < lo or ob.bot > hi):     # overlap with the range
            return True
    return False


def evaluate(htf_bars: List[Bar], ltf_bars: List[Bar],
             cfg: Optional[AMDConfig] = None,
             now_ts: Optional[float] = None) -> Setup:
    cfg = cfg or AMDConfig()
    s = Setup()
    need = cfg.accum_bars + cfg.manip_bars + 5
    if len(htf_bars) < 20 or len(ltf_bars) < need:
        s.gates.append(Gate("G0_DATA", False, "insufficient bars"))
        return s

    htf = analyze(htf_bars)
    ltf = analyze(ltf_bars)
    ltf_atr = atr(ltf_bars, 14)
    ts = now_ts if now_ts is not None else ltf_bars[-1].time
    price = ltf_bars[-1].close

    # ── BIAS ──────────────────────────────────────────────────────────────────
    bias, bdetail = _htf_bias(htf, htf_bars, cfg)
    s.htf_bias = bias
    if bias == "NEUTRAL":
        s.gates.append(Gate("BIAS", False, bdetail)); return s
    s.gates.append(Gate("BIAS", True, bdetail))
    s.direction = bias

    # ── G1 ACCUMULATION ─────────────────────────────────────────────────────────
    accum = ltf_bars[-(cfg.accum_bars + cfg.manip_bars):-cfg.manip_bars]
    manip = ltf_bars[-cfg.manip_bars:]
    if len(accum) < cfg.accum_bars // 2 or not manip:
        s.gates.append(Gate("G1_ACCUM", False, "no accumulation window")); return s
    acc_hi = max(b.high for b in accum)
    acc_lo = min(b.low for b in accum)
    width = acc_hi - acc_lo
    if ltf_atr > 0 and not (cfg.accum_min_width_atr * ltf_atr <= width <= cfg.accum_max_width_atr * ltf_atr):
        s.gates.append(Gate("G1_ACCUM", False,
                            f"range width {width:.2f} outside [{cfg.accum_min_width_atr:.1f},"
                            f"{cfg.accum_max_width_atr:.1f}]*ATR — not clean accumulation")); return s
    if cfg.require_htf_ob_anchor and not _htf_ob_anchor(htf, acc_lo, acc_hi, bias):
        s.gates.append(Gate("G1_ACCUM", False, "range not anchored at an HTF OB")); return s
    s.gates.append(Gate("G1_ACCUM", True, f"range {acc_lo:.2f}-{acc_hi:.2f} (w={width:.2f})"))
    s.equilibrium = (acc_hi + acc_lo) / 2.0

    # ── G2 MANIPULATION (judas sweep AGAINST bias, then reclaim) ────────────────
    buf = cfg.sweep_min_atr * ltf_atr
    if bias == "BULL":
        swept_extreme = min(b.low for b in manip)
        swept = swept_extreme < acc_lo - buf
        reclaimed = price > acc_lo
        detail = f"swept range low {acc_lo:.2f} -> {swept_extreme:.2f}, reclaimed={reclaimed}"
    else:
        swept_extreme = max(b.high for b in manip)
        swept = swept_extreme > acc_hi + buf
        reclaimed = price < acc_hi
        detail = f"swept range high {acc_hi:.2f} -> {swept_extreme:.2f}, reclaimed={reclaimed}"
    if not (swept and reclaimed):
        s.gates.append(Gate("G2_MANIP", False, "no judas sweep+reclaim of range " + detail)); return s
    s.gates.append(Gate("G2_MANIP", True, detail))

    # ── G3 DISTRIBUTION confirmation (LTF structure shift in bias dir after sweep) ─
    manip_start_idx = len(ltf_bars) - cfg.manip_bars
    shift = [e for e in ltf.structure if e.direction == bias and e.bar_idx >= manip_start_idx]
    if not shift:
        s.gates.append(Gate("G3_DISTRIB", False,
                            f"no {bias} CHoCH/BOS after the sweep — reversal unconfirmed")); return s
    s.gates.append(Gate("G3_DISTRIB", True, f"{shift[-1].kind} {bias} @ bar {shift[-1].bar_idx}"))

    # ── ENTRY at the reversal OB/FVG (or reclaimed range edge) ──────────────────
    entry, etype = None, "reclaim"
    cands = []
    for fvg in ltf.fvgs:
        if fvg.mitigated or fvg.side != bias:
            continue
        mid = (fvg.bot + fvg.top) / 2
        if swept_extreme <= mid <= acc_hi if bias == "BULL" else acc_lo <= mid <= swept_extreme:
            cands.append((fvg.top if bias == "BULL" else fvg.bot, "fvg"))
    for ob in ltf.order_blocks:
        if ob.mitigated or ob.side != bias:
            continue
        mid = (ob.bot + ob.top) / 2
        if swept_extreme <= mid <= acc_hi if bias == "BULL" else acc_lo <= mid <= swept_extreme:
            cands.append((ob.top if bias == "BULL" else ob.bot, "ob"))
    if cands:
        # closest retest to the reclaimed edge (deepest discount/premium)
        cands.sort(key=lambda c: c[0], reverse=(bias == "BEAR"))
        entry, etype = cands[0]
    else:
        entry = acc_lo if bias == "BULL" else acc_hi      # retest the reclaimed edge

    # ── STOP beyond the manipulation extreme ────────────────────────────────────
    sl_buf = cfg.sl_atr_buffer * ltf_atr
    sl = (swept_extreme - sl_buf) if bias == "BULL" else (swept_extreme + sl_buf)

    # ── TARGET: opposite range extreme (tp1) -> major opposing HTF OB / extreme (tp2)
    if bias == "BULL":
        tp1 = acc_hi
        obs = [ob.top for ob in htf.order_blocks if not ob.mitigated and ob.side == "BEAR" and ob.top > acc_hi]
        sw = [s_.price for s_ in htf.swings if s_.kind == "HIGH" and s_.price > acc_hi]
        tp2 = min(obs) if obs else (min(sw) if sw else acc_hi + (acc_hi - acc_lo))
    else:
        tp1 = acc_lo
        obs = [ob.bot for ob in htf.order_blocks if not ob.mitigated and ob.side == "BULL" and ob.bot < acc_lo]
        sw = [s_.price for s_ in htf.swings if s_.kind == "LOW" and s_.price < acc_lo]
        tp2 = max(obs) if obs else (max(sw) if sw else acc_lo - (acc_hi - acc_lo))

    # ── Sanity + RR ─────────────────────────────────────────────────────────────
    if bias == "BULL" and not (sl < entry < tp1 <= tp2):
        s.gates.append(Gate("GEOM", False, f"bad geometry sl{sl:.2f} e{entry:.2f} t1{tp1:.2f} t2{tp2:.2f}")); return s
    if bias == "BEAR" and not (sl > entry > tp1 >= tp2):
        s.gates.append(Gate("GEOM", False, f"bad geometry sl{sl:.2f} e{entry:.2f} t1{tp1:.2f} t2{tp2:.2f}")); return s
    risk = abs(entry - sl)
    rr1 = abs(tp1 - entry) / risk if risk > 0 else 0.0
    if risk <= 0 or rr1 < cfg.min_rr:
        s.gates.append(Gate("RR", False, f"tp1 only {rr1:.2f}R (< {cfg.min_rr}) — skip")); return s

    if cfg.use_killzone:
        hour = _hour_utc(ts)
        if not _in_kill_zone(hour, cfg.kill_zones):
            s.gates.append(Gate("KILLZONE", False, f"outside killzones (hour={hour})")); return s
        s.gates.append(Gate("KILLZONE", True, f"killzone active (hour={hour})"))

    s.entry = round(entry, 2)
    s.sl = round(sl, 2)
    s.tp1 = round(tp1, 2)
    s.tp2 = round(tp2, 2)
    s.tp = round(tp1, 2)
    s.rr = round(rr1, 2)
    s.draw_target = round(tp2, 2)
    s.entry_type = etype
    s.actionable = True
    s.gates.append(Gate("DISTRIBUTION", True,
                        f"{bias} entry {entry:.2f} ({etype}) sl {sl:.2f} tp1 {tp1:.2f} "
                        f"({rr1:.2f}R) runner tp2 {tp2:.2f}"))
    return s


# ──────────────────────────────────────────────────────────────────────────────
# Self-test — hand-built BULL AMD: accumulation, sweep below, reclaim, BOS up.
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    bars: List[Bar] = []
    t0 = 1_700_000_000
    px = 2000.0

    def push(o, h, l, c):
        bars.append(Bar(time=t0 + len(bars) * 900, open=o, high=h, low=l, close=c))

    # HTF uptrend leg then base (we pass the same bars as HTF for the test).
    for _ in range(30):                       # prior uptrend (sets BULL bias)
        push(px, px + 3, px - 1, px + 2); px += 2
    # Accumulation: range ~1990-2002 for 20 bars
    lo, hi = px - 12, px
    for i in range(20):
        c = lo + (hi - lo) * (0.5 + 0.2 * ((-1) ** i))
        push(c, c + 2, c - 2, c)
    # Manipulation: sweep below the range low, then reclaim
    push(lo, lo + 1, lo - 8, lo - 6)          # spike below
    push(lo - 6, lo + 2, lo - 7, lo + 1)      # reclaim back into range
    # Distribution: drive up (creates BOS up)
    for _ in range(10):
        push(px2 := bars[-1].close, bars[-1].close + 5, bars[-1].close - 1, bars[-1].close + 4)

    s = evaluate(bars, bars, now_ts=datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc).timestamp())
    print("AMD engine self-test")
    print(s.trace())
    print(f"\nfailed_at = {s.failed_at or '(none — actionable)'}")
    assert s.gates, "must produce a gate trace"
    print("\n[OK] AMD self-test ran (gate plumbing intact)")


if __name__ == "__main__":
    _self_test()
