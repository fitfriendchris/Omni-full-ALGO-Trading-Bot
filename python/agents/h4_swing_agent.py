#!/usr/bin/env python3
"""
h4_swing_agent.py — H4/D1 swing auto-trader with OB retest entries.

STRATEGY:
  Scan H4 bars for unmitigated Order Blocks (OBs).
  When price retraces to OB body (50% retest level), place LIMIT order.
  SL beyond OB extreme + 2×ATR buffer.
  TP at next opposing H4 OB (minimum 1:2 R:R, target 1:3).
  Only trade if ADX < 25 (ranging/choppy) AND session = London/NY.
  Risk 2% per trade, 0.01 lot.

This is the ONLY strategy Chris has proven to work manually.
"""

import sys, json, os, re, math, time, asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SIGNALS_PATH = PROJECT_ROOT / "shared" / "signals.json"
MT5_DATA = Path.home() / "Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"

RISK_PCT = 2.0
MIN_RR = 2.0
TARGET_RR = 3.0
LOT = 0.01
MAX_ACTIVE_TRADES = 2
HOLD_BARS_H4 = 24  # 4 days of H4 bars before cancelling
ADX_THRESHOLD = 25.0


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] H4SWING: {msg}", flush=True)


def _load_mt5() -> Optional[Dict]:
    """Load MT5 data with retry."""
    for _ in range(3):
        try:
            with open(MT5_DATA, 'r') as f:
                raw = f.read()
            raw = re.sub(r',\s*([\]\}])', r'\1', raw)
            return json.loads(raw)
        except Exception:
            time.sleep(0.5)
    return None


class Bar:
    """Simple bar for H4 analysis."""
    def __init__(self, time: str, open: float, high: float, low: float, close: float):
        self.time = time
        self.open = open
        self.high = high
        self.low = low
        self.close = close


def _parse_bars(raw_bars: List[Dict]) -> List[Bar]:
    """Parse raw MT5 bars newest-first into Bar list newest-first."""
    out = []
    for b in raw_bars:
        out.append(Bar(time=b['t'], open=b['o'], high=b['h'], low=b['l'], close=b['c']))
    return out


def _ema(closes: List[float], period: int) -> List[float]:
    """Exponential moving average."""
    if len(closes) < period:
        return closes[:]
    multiplier = 2.0 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema


def _atr(bars: List[Bar], period: int = 14) -> float:
    """Average True Range."""
    if len(bars) < period + 1:
        return (sum(b.high - b.low for b in bars) / len(bars)) if bars else 0
    trs = []
    for i in range(1, min(period + 1, len(bars))):
        tr = max(bars[i-1].high, bars[i].close) - min(bars[i-1].low, bars[i].close)
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0


def _adx(bars: List[Bar], period: int = 14) -> float:
    """Average Directional Index. Returns 0-100+."""
    if len(bars) < period * 3:
        return 0.0
    
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(bars)):
        up_move = bars[i-1].high - bars[i].high
        down_move = bars[i].low - bars[i-1].low
        
        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0)
        
        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0)
        
        tr = max(bars[i-1].high, bars[i].close) - min(bars[i-1].low, bars[i].close)
        trs.append(tr)
    
    # Smooth
    def smooth(values, period):
        if len(values) < period:
            return []
        s = [sum(values[:period])]
        for v in values[period:]:
            s.append(s[-1] - s[-1]/period + v)
        return s
    
    s_plus = smooth(plus_dm, period)
    s_minus = smooth(minus_dm, period)
    s_tr = smooth(trs, period)
    
    if not s_plus or not s_minus or not s_tr:
        return 0.0
    
    dx_vals = []
    for i in range(len(s_plus)):
        sum_dm = s_plus[i] + s_minus[i]
        if sum_dm == 0:
            dx_vals.append(0)
        else:
            diff = abs(s_plus[i] - s_minus[i])
            dx_vals.append((diff / sum_dm) * 100)
    
    if len(dx_vals) < period:
        return sum(dx_vals) / len(dx_vals) if dx_vals else 0
    
    adx = sum(dx_vals[:period]) / period
    for dx in dx_vals[period:]:
        adx = (adx * (period - 1) + dx) / period
    
    return adx


class OrderBlock:
    def __init__(self, anchor_idx: int, top: float, bot: float, side: str, 
                 mitigated: bool = False, strength: str = "WEAK"):
        self.anchor_idx = anchor_idx
        self.top = top
        self.bot = bot
        self.side = side
        self.mitigated = mitigated
        self.strength = strength
        self.retest_count = 0


def _find_order_blocks(bars: List[Bar]) -> List[OrderBlock]:
    """Find bullish and bearish order blocks on H4."""
    obs = []
    
    for i in range(3, len(bars) - 1):
        # Bullish OB: bearish candle before swing low, followed by strong bullish
        prev = bars[i-1]
        curr = bars[i]
        
        # Check for swing structure
        is_swing_high = (bars[i-2].high < prev.high and bars[i].high < prev.high)
        is_swing_low = (bars[i-2].low > prev.low and bars[i].low > prev.low)
        
        # Bullish OB: prior to bullish move (bearish candle, then strong bullish)
        if prev.close < prev.open and curr.close > curr.open and curr.close > prev.open:
            top = max(prev.open, prev.close)
            bot = min(prev.open, prev.close)
            strength = "STRONG" if (curr.close - curr.open) > (prev.open - prev.close) else "WEAK"
            obs.append(OrderBlock(anchor_idx=i, top=top, bot=bot, side="BULL", strength=strength))
        
        # Bearish OB: prior to bearish move (bullish candle, then strong bearish)
        if prev.close > prev.open and curr.close < curr.open and curr.close < prev.open:
            top = max(prev.open, prev.close)
            bot = min(prev.open, prev.close)
            strength = "STRONG" if (prev.close - prev.open) > (curr.open - curr.close) else "WEAK"
            obs.append(OrderBlock(anchor_idx=i, top=top, bot=bot, side="BEAR", strength=strength))
    
    return obs


def _check_mitigated(obs: List[OrderBlock], bars: List[Bar]) -> None:
    """Mark OBs as mitigated only if body CLOSED through the zone (not just wick)."""
    for ob in obs:
        # Check bars AFTER the OB (newer = lower index since newest-first)
        for i in range(ob.anchor_idx - 1, -1, -1):
            bar = bars[i]
            
            # True mitigation: body closed through the OB
            body_through = (bar.close > ob.top and bar.open > ob.top) or \
                           (bar.close < ob.bot and bar.open < ob.bot)
            
            if body_through:
                ob.mitigated = True
                break
            
            # Sweep (wick through but body didn't): counts as retest, not mitigation
            wick_through = (bar.low < ob.bot and bar.close > ob.bot) or \
                           (bar.high > ob.top and bar.close < ob.top)
            if wick_through:
                ob.retest_count += 1


def _is_london_or_ny() -> bool:
    """Check if current time is London (08:00-11:00 UTC) or NY (13:00-16:00 UTC)."""
    now = datetime.now(datetime.now().astimezone().tzinfo)
    hour = now.hour
    return (8 <= hour < 11) or (13 <= hour < 16)


class H4SwingAgent:
    """H4 swing trading agent."""
    
    def __init__(self):
        self.active_signals: List[Dict] = []
        self.state = {"signals_sent": 0, "last_direction": None}
    
    def run(self) -> Optional[Dict]:
        """Main scan — returns signal dict or None."""
        data = _load_mt5()
        if not data or 'charts' not in data:
            return None
        
        xau = data['charts'].get('XAUUSD')
        if not xau or 'H4' not in xau:
            return None
        
        h4_raw = xau['H4']
        if not isinstance(h4_raw, list) or len(h4_raw) < 30:
            return None
        
        bars = _parse_bars(h4_raw)
        
        # Current price
        current = bars[0].close
        
        # Regime check
        adx = _adx(bars)
        if adx >= ADX_THRESHOLD:
            log(f"ADX={adx:.1f} >= {ADX_THRESHOLD} — skipping (trending)")
            return None
        
        # Session check
        if not _is_london_or_ny():
            log("Outside London/NY session — skipping")
            return None
        
        # Find OBs
        obs = _find_order_blocks(bars)
        _check_mitigated(obs, bars)
        
        # Filter: unmitigated OR recently mitigated with low retest count
        valid_obs = [ob for ob in obs if (not ob.mitigated) or ob.retest_count <= 2]
        valid_obs.sort(key=lambda o: o.anchor_idx, reverse=True)
        
        if not valid_obs:
            log(f"No valid unmitigated OBs (found {len(obs)} total)")
            return None
        
        log(f"ADX={adx:.1f} | {len(valid_obs)} valid OBs | Current={current:.2f}")
        
        atr = _atr(bars)
        
        for ob in valid_obs[:5]:
            # Calculate retest level (50% of OB body)
            ob_body = ob.top - ob.bot
            retest = ob.bot + ob_body * 0.5
            
            # Check if price is near the OB retest level (within 1.5 ATR)
            dist = abs(current - retest)
            if dist > atr * 1.5:
                log(f"{ob.side} OB@{ob.anchor_idx}: retest={retest:.2f} dist={dist:.2f} ({dist/atr:.1f}x ATR) — too far")
                continue
            
            # For BULL OB: price must be AT or BELOW retest (coming down to it)
            if ob.side == "BULL" and current > retest * 1.05:
                log(f"BULL OB@{ob.anchor_idx}: price={current:.2f} > retest={retest:.2f} — already above")
                continue
            
            # For BEAR OB: price must be AT or ABOVE retest (coming up to it)
            if ob.side == "BEAR" and current < retest * 0.95:
                log(f"BEAR OB@{ob.anchor_idx}: price={current:.2f} < retest={retest:.2f} — already below")
                continue
            
            # Generate signal for this OB
            if ob.side == "BULL":
                sl = ob.bot - atr * 2.0
                tp = self._find_tp(ob, valid_obs, current, atr)
                if not tp or (tp - retest) / (retest - sl) < MIN_RR:
                    log(f"BULL OB@{ob.anchor_idx}: R:R too low — skipping")
                    continue
                return self._make_signal("BULL", retest, sl, tp, atr, adx, ob)
            else:
                sl = ob.top + atr * 2.0
                tp = self._find_tp(ob, valid_obs, current, atr)
                if not tp or (retest - tp) / (sl - retest) < MIN_RR:
                    log(f"BEAR OB@{ob.anchor_idx}: R:R too low — skipping")
                    continue
                return self._make_signal("BEAR", retest, sl, tp, atr, adx, ob)
    
    def _find_tp(self, current_ob: OrderBlock, all_obs: List[OrderBlock], 
                 current_price: float, atr: float) -> Optional[float]:
        """Find target at next opposing OB."""
        side = "BEAR" if current_ob.side == "BULL" else "BULL"
        
        # Look for opposing OBs that are unmitigated and past current price
        targets = []
        for ob in all_obs:
            if ob.side == side and not ob.mitigated and ob.anchor_idx != current_ob.anchor_idx:
                targets.append(ob.bot if side == "BEAR" else ob.top)
        
        if not targets:
            # Fallback: use 3x ATR projection
            if current_ob.side == "BULL":
                return current_price + atr * 3.0
            else:
                return current_price - atr * 3.0
        
        targets.sort(reverse=(current_ob.side == "BULL"))
        
        # Pick first target that gives MIN_RR
        if current_ob.side == "BULL":
            for t in targets:
                if t > current_ob.top:
                    return t
        else:
            for t in targets:
                if t < current_ob.bot:
                    return t
        
        return targets[0] if targets else None
    
    def _make_signal(self, direction: str, entry: float, sl: float, tp: float,
                    atr: float, adx: float, ob: OrderBlock) -> Dict:
        """Format signal for execution."""
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        return {
            "id": f"H4SWING-XAUUSD-{int(time.time()*1000000)}",
            "symbol": "XAUUSD",
            "timeframe": "H4",
            "direction": direction,
            "entry_type": "ob_retest",
            "entry_price": round(entry, 2),
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "rr": round(rr, 1),
            "atr": round(atr, 2),
            "adx": round(adx, 1),
            "session": "london" if 8 <= datetime.now().hour < 11 else "ny",
            "ob_side": ob.side,
            "ob_strength": ob.strength,
            "ob_anchor": ob.anchor_idx,
            "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
        }


def main():
    agent = H4SwingAgent()
    signal = agent.run()
    if signal:
        print(json.dumps(signal, indent=2))
    else:
        print("No signal")


if __name__ == "__main__":
    main()
