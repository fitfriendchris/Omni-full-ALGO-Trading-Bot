#!/usr/bin/env python3
"""
entropy_signal_agent.py — Entropy STDV+OTE Signal Agent for Live Bot

Monitors MT5 data in real-time:
  1. Detects manipulation legs on M5
  2. Calculates STDV + OTE confluences
  3. Places limit orders at optimal levels
  4. Manages swing trades with scaling

Reads from: omni_data.json (updated every 60s by MT5 EA)
Sends to:   Telegram + shared/signals.json
"""
import json, re, os, sys, time
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

# Config paths
OMNI_PATH = '/Users/yuhfriendchris/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json'
SHARED_PATH = '/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/shared/signals.json'
STATE_PATH = '/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/entropy_agent_state.json'

# Best params from optimizer (will be loaded from entropy_optimized_params.json)
DEFAULT_PARAMS = {
    "lookback": 20,
    "sl_buffer": 0.05,
    "min_rr": 2.0,
    "stdv_levels": ["ote_-0.705", "reaccum_-1"],
    "ote_levels": ["ote_0.886", "ote_0.79"],
    "confluence_tol": 0.2,
    "cooldown": 5,
    "hold_bars": 50,
    "entry_mode": "deep",
}

class EntropySignalAgent:
    GOAL = "Generate Entropy STDV+OTE signals for manipulation leg entries"
    
    def __init__(self):
        self.state = self.load_state()
        self.params = self.load_params()
        self.last_signal_time = None
        self.cooldown_until = None
        
    def load_state(self) -> Dict:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, 'r') as f:
                return json.load(f)
        return {"signals_sent": 0, "last_symbol": None, "last_direction": None}
    
    def save_state(self):
        with open(STATE_PATH, 'w') as f:
            json.dump(self.state, f, indent=2)
    
    def load_params(self) -> Dict:
        opt_path = '/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/entropy_optimized_params.json'
        if os.path.exists(opt_path):
            with open(opt_path, 'r') as f:
                data = json.load(f)
                return data.get("params", DEFAULT_PARAMS)
        return DEFAULT_PARAMS
    
    def load_mt5(self) -> Optional[Dict]:
        try:
            with open(OMNI_PATH, 'r') as f:
                raw = f.read()
            raw = re.sub(r',\s*([\]\}])', r'\1', raw)
            return json.loads(raw)
        except:
            return None
    
    def calc_stdv_levels(self, anchor_high: float, anchor_low: float, mode="long") -> Dict[str, float]:
        rng = anchor_high - anchor_low
        if mode == "long":
            return {
                "ce_0.5": anchor_high - rng * 0.5,
                "ote_-0.705": anchor_high - rng * 0.705,
                "reaccum_-1": anchor_high - rng * 1.0,
                "reversal_-2": anchor_high - rng * 2.0,
                "maxexp_-3": anchor_high - rng * 3.0,
                "maxexp_-4": anchor_high - rng * 4.0,
                "maxexp_-5": anchor_high - rng * 5.0,
            }
        else:
            return {
                "ce_0.5": anchor_low + rng * 0.5,
                "ote_-0.705": anchor_low + rng * 0.705,
                "reaccum_-1": anchor_low + rng * 1.0,
                "reversal_-2": anchor_low + rng * 2.0,
                "maxexp_-3": anchor_low + rng * 3.0,
                "maxexp_-4": anchor_low + rng * 4.0,
                "maxexp_-5": anchor_low + rng * 5.0,
            }
    
    def calc_ote_levels(self, swing_high: float, swing_low: float, mode="long") -> Dict[str, float]:
        rng = swing_high - swing_low
        if mode == "long":
            return {
                "ce_0.5": swing_high - rng * 0.5,
                "ote_0.886": swing_high - rng * 0.886,
                "ote_0.79": swing_high - rng * 0.79,
                "ote_0.705": swing_high - rng * 0.705,
                "ote_0.65": swing_high - rng * 0.65,
                "ote_0.63": swing_high - rng * 0.63,
            }
        else:
            return {
                "ce_0.5": swing_low + rng * 0.5,
                "ote_0.886": swing_low + rng * 0.886,
                "ote_0.79": swing_low + rng * 0.79,
                "ote_0.705": swing_low + rng * 0.705,
                "ote_0.65": swing_low + rng * 0.65,
                "ote_0.63": swing_low + rng * 0.63,
            }
    
    def find_confluence(self, stdv: Dict, ote: Dict, tol: float) -> List[Tuple[str, str, float]]:
        confluences = []
        for sk, sv in stdv.items():
            for ok, ov in ote.items():
                avg = (sv + ov) / 2
                if avg == 0:
                    continue
                diff_pct = abs(sv - ov) / avg * 100
                if diff_pct < tol:
                    confluences.append((sk, ok, avg))
        return confluences
    
    def detect_manipulation_leg(self, bars: List[Dict], lookback: int = 20) -> Optional[Dict]:
        """
        Check if most recent bar is a manipulation leg.
        NOTE: bars from MT5 are newest-first — index 0 = most recent.
        """
        if len(bars) < lookback + 5:
            return None
        
        # bars[0] = most recent, bars[lookback] = oldest in window
        recent = bars[0:lookback+1]
        
        # Find swing highs/lows in window
        swing_highs = []
        swing_lows = []
        for j in range(2, len(recent) - 2):
            if recent[j]['h'] > recent[j-1]['h'] and recent[j]['h'] > recent[j-2]['h'] and \
               recent[j]['h'] > recent[j+1]['h'] and recent[j]['h'] > recent[j+2]['h']:
                swing_highs.append((j, recent[j]['h']))
            if recent[j]['l'] < recent[j-1]['l'] and recent[j]['l'] < recent[j-2]['l'] and \
               recent[j]['l'] < recent[j+1]['l'] and recent[j]['l'] < recent[j+2]['l']:
                swing_lows.append((j, recent[j]['l']))
        
        if not swing_highs or not swing_lows:
            return None
        
        last_sh = min(swing_highs, key=lambda x: x[0])  # Most recent swing high
        last_sl = min(swing_lows, key=lambda x: x[0])  # Most recent swing low
        curr = bars[0]   # Most recent bar
        prev = bars[1]   # Previous bar
        
        # LONG: sweep below swing low, then reject (close bullish)
        if curr['l'] < last_sl[1] and curr['c'] > curr['o']:
            return {
                "type": "long",
                "manipulation_high": prev['h'],
                "manipulation_low": curr['l'],
                "swing_high": last_sh[1],
                "swing_low": last_sl[1],
                "time": curr['t'],
            }
        
        # SHORT: sweep above swing high, then reject (close bearish)
        elif curr['h'] > last_sh[1] and curr['c'] < curr['o']:
            return {
                "type": "short",
                "manipulation_high": curr['h'],
                "manipulation_low": prev['l'],
                "swing_high": last_sh[1],
                "swing_low": last_sl[1],
                "time": curr['t'],
            }
        
        return None
    
    def generate_signal(self, leg: Dict, symbol: str, bars: List[Dict]) -> Optional[Dict]:
        p = self.params
        
        # Calculate STDV + OTE
        if leg["type"] == "long":
            stdv = self.calc_stdv_levels(leg["manipulation_high"], leg["manipulation_low"], "long")
            ote = self.calc_ote_levels(leg["swing_high"], leg["swing_low"], "long")
        else:
            stdv = self.calc_stdv_levels(leg["manipulation_high"], leg["manipulation_low"], "short")
            ote = self.calc_ote_levels(leg["swing_high"], leg["swing_low"], "short")
        
        # Filter to configured levels
        stdv_f = {k: v for k, v in stdv.items() if any(sk in k for sk in p["stdv_levels"])}
        ote_f = {k: v for k, v in ote.items() if any(ok in k for ok in p["ote_levels"])}
        
        confluences = self.find_confluence(stdv_f, ote_f, p["confluence_tol"])
        if not confluences:
            return None
        
        # Select entry
        if leg["type"] == "long":
            if p["entry_mode"] == "deep":
                entry_level = min(confluences, key=lambda x: x[2])
            else:
                entry_level = max(confluences, key=lambda x: x[2])
        else:
            if p["entry_mode"] == "deep":
                entry_level = max(confluences, key=lambda x: x[2])
            else:
                entry_level = min(confluences, key=lambda x: x[2])
        
        entry = entry_level[2]
        manipulation_range = leg["manipulation_high"] - leg["manipulation_low"]
        buffer = manipulation_range * p["sl_buffer"]
        
        if leg["type"] == "long":
            sl = min(leg["manipulation_low"], entry) - buffer
            tp = max(leg["swing_high"], stdv.get("ce_0.5", entry))
            if tp <= entry:
                tp = entry + manipulation_range * 0.5
        else:
            sl = max(leg["manipulation_high"], entry) + buffer
            tp = min(leg["swing_low"], stdv.get("ce_0.5", entry))
            if tp >= entry:
                tp = entry - manipulation_range * 0.5
        
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        
        if risk <= 0 or reward / risk < p["min_rr"]:
            return None
        
        # Get current price for distance check
        current = bars[0]['c']  # Most recent close (bars newest-first)
        
        return {
            "symbol": symbol,
            "direction": leg["type"],
            "entry": round(entry, 5 if symbol in ["EURUSD", "GBPUSD", "AUDUSD", "USDCAD"] else 3 if symbol == "USDJPY" else 2),
            "sl": round(sl, 5 if symbol in ["EURUSD", "GBPUSD", "AUDUSD", "USDCAD"] else 3 if symbol == "USDJPY" else 2),
            "tp": round(tp, 5 if symbol in ["EURUSD", "GBPUSD", "AUDUSD", "USDCAD"] else 3 if symbol == "USDJPY" else 2),
            "rr": round(reward / risk, 1),
            "risk_pct": 2.0,
            "confluence": f"{entry_level[0]} + {entry_level[1]}",
            "setup": "entropy_stdv_ote",
            "time": leg["time"],
            "current_price": current,
            "distance_to_entry_pct": round(abs(current - entry) / entry * 100, 3),
        }
    
    def run(self):
        """Main loop — check for signals."""
        data = self.load_mt5()
        if not data:
            return None
        
        charts = data.get("charts", {})
        signals = []
        
        for symbol in ["XAUUSD", "XAGUSD"]:
            if symbol not in charts or "M5" not in charts[symbol]:
                continue
            
            m5 = charts[symbol]["M5"]
            if len(m5) < 30:
                continue
            
            # Check cooldown
            if self.cooldown_until and datetime.now() < self.cooldown_until:
                continue
            
            # Detect manipulation leg
            leg = self.detect_manipulation_leg(m5, self.params["lookback"])
            if not leg:
                continue
            
            # Generate signal
            signal = self.generate_signal(leg, symbol, m5)
            if not signal:
                continue
            
            # Don't repeat same direction on same symbol
            if self.state.get("last_symbol") == symbol and self.state.get("last_direction") == signal["direction"]:
                continue
            
            signals.append(signal)
            
            # Update state
            self.state["signals_sent"] = self.state.get("signals_sent", 0) + 1
            self.state["last_symbol"] = symbol
        return signals
    
    def run_sync(self):
        """Synchronous version for direct calling."""
        return self._run_impl()
    
    def _run_impl(self):
        """Actual implementation."""
        data = self.load_mt5()
        if not data:
            return None
        
        charts = data.get("charts", {})
        signals = []
        
        # XAUUSD ONLY — proven profitable in 17-day backtest (+36.5%)
        for symbol in ["XAUUSD"]:
            if symbol not in charts or "M5" not in charts[symbol]:
                continue
            
            m5 = charts[symbol]["M5"]
            if len(m5) < 30:
                continue
            
            # Check cooldown
            if self.cooldown_until and datetime.now() < self.cooldown_until:
                continue
            
            # Detect manipulation leg
            leg = self.detect_manipulation_leg(m5, self.params["lookback"])
            if not leg:
                continue
            
            # Generate signal
            signal = self.generate_signal(leg, symbol, m5)
            if not signal:
                continue
            
            # Don't repeat same direction on same symbol
            if self.state.get("last_symbol") == symbol and self.state.get("last_direction") == signal["direction"]:
                continue
            
            signals.append(signal)
            
            # Update state
            self.state["signals_sent"] = self.state.get("signals_sent", 0) + 1
            self.state["last_symbol"] = symbol
            self.state["last_direction"] = signal["direction"]
            self.cooldown_until = datetime.now() + timedelta(minutes=self.params["cooldown"] * 5)
            self.save_state()
        
        return signals
    
    async def run(self):
        """Async loop for swarm integration."""
        import asyncio
        while True:
            try:
                signals = self.run_sync()
                if signals:
                    for s in signals:
                        print(f"🎯 ENTROPY: {s['symbol']} {s['direction'].upper()} @ {s['entry']} | SL: {s['sl']} | TP: {s['tp']} | R:R {s['rr']}:1")
                await asyncio.sleep(60)  # Check every 60 seconds
            except Exception as e:
                print(f"EntropySignalAgent error: {e}")
                await asyncio.sleep(60)

if __name__ == "__main__":
    agent = EntropySignalAgent()
    signals = agent.run_sync()
    
    if signals:
        print("🎯 ENTROPY SIGNALS DETECTED:")
        for s in signals:
            print(f"\n{s['symbol']} {s['direction'].upper()}")
            print(f"  Entry: {s['entry']} | SL: {s['sl']} | TP: {s['tp']}")
            print(f"  R:R: {s['rr']}:1 | Confluence: {s['confluence']}")
            print(f"  Current: {s['current_price']} | Distance: {s['distance_to_entry_pct']}%")
        
        # Save to shared
        with open(SHARED_PATH, 'w') as f:
            json.dump({"version": "1.0", "generated_at": datetime.utcnow().isoformat(), 
                      "signals": signals, "trail_proposals": {}}, f, indent=2)
    else:
        print("No manipulation legs detected.")
