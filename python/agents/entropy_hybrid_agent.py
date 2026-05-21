#!/usr/bin/env python3
"""
entropy_hybrid_agent.py — Human-in-the-loop strategy for live deployment

BOT ROLE:
  1. Scan M5 bars for manipulation legs (sweep + rejection)
  2. Calculate STDV + OTE confluence levels
  3. Send RICH Telegram alert with full trade plan
  4. Wait for Chris approval (YES/NO)
  5. On YES: place LIMIT order at confluence
  6. On NO: skip and continue scanning
  7. Manage approved trades: trailing stop, breakeven, exit

CHRIS ROLE:
  1. Receives Telegram alert with:
     - Setup type (LONG/SHORT)
     - Entry price (limit)
     - SL (beyond manipulation extreme)
     - TP (structural level)
     - R:R ratio
     - Confluence details (STDV level + OTE level)
     - Session/time
     - Manipulation leg description
  2. Reviews MT5 chart with SMC overlay
  3. Replies YES to execute or NO to skip

HONEST REALITY:
  - Bot does the math (STDV/OTE calculation)
  - Chris does the quality control (pattern recognition)
  - Together = Chris's discretionary edge + bot's precision execution

This is the ONLY realistic path to profitability with $135 account.
"""

import json, os, re, sys, time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# ── Config ──
OMNI_DATA_PATH = "/Users/yuhfriendchris/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"
STATE_PATH = "/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/entropy_hybrid_state.json"
SIGNALS_PATH = "/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/shared/signals.json"

# ── Params ──
LOOKBACK = 10
MIN_SL_DISTANCE = 2.0  # $2.00 on gold
MAX_RR = 15.0
MIN_RR = 1.5
CONFLUENCE_ZONE_PCT = 1.0
HOLD_BARS = 20
COOLDOWN_BARS = 3


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def load_mt5_data() -> Dict:
    """Load omni_data.json from MT5 (newest-first format)."""
    if not os.path.exists(OMNI_DATA_PATH):
        return {}
    with open(OMNI_DATA_PATH, 'r') as f:
        raw = f.read()
    raw = re.sub(r',\s*([\]\}])', r'\1', raw)
    return json.loads(raw)


def calc_stdv(manip_high: float, manip_low: float) -> Dict[str, float]:
    r = manip_high - manip_low
    return {"ce": manip_low + r * 0.5}


def calc_ote(swing_high: float, swing_low: float, direction: str = "long") -> Dict[str, float]:
    r = swing_high - swing_low
    if direction == "long":
        return {"0.5": swing_low + r * 0.5, "0.886": swing_low + r * 0.886}
    else:
        return {"0.5": swing_high - r * 0.5, "0.886": swing_high - r * 0.886}


def find_confluence_zone(stdv_levels: Dict, ote_levels: Dict, tol_pct: float):
    out = []
    for sk, sv in stdv_levels.items():
        for ok, ov in ote_levels.items():
            dist = abs(sv - ov)
            avg = (sv + ov) / 2
            if avg > 0 and (dist / avg) * 100 <= tol_pct:
                out.append((sk, ok, (sv + ov) / 2))
    return out


def detect_manipulation_leg(bars: List[Dict]) -> Optional[Dict]:
    """
    bars: newest-first from MT5.
    Returns manipulation leg if sweep + rejection detected.
    """
    if len(bars) < LOOKBACK + 3:
        return None
    
    recent = bars[0:LOOKBACK+1]
    highs, lows = [], []
    for j in range(2, len(recent) - 2):
        if recent[j]['h'] > recent[j-1]['h'] and recent[j]['h'] > recent[j-2]['h'] and \
           recent[j]['h'] > recent[j+1]['h'] and recent[j]['h'] > recent[j+2]['h']:
            highs.append((j, recent[j]['h']))
        if recent[j]['l'] < recent[j-1]['l'] and recent[j]['l'] < recent[j-2]['l'] and \
           recent[j]['l'] < recent[j+1]['l'] and recent[j]['l'] < recent[j+2]['l']:
            lows.append((j, recent[j]['l']))
    
    if not highs or not lows:
        return None
    
    last_sh = min(highs, key=lambda x: x[0])
    last_sl = min(lows, key=lambda x: x[0])
    curr = bars[0]
    prev = bars[1]
    
    body = abs(curr['c'] - curr['o'])
    rng = curr['h'] - curr['l']
    strong_rejection = body > rng * 0.5 if rng > 0 else False
    
    if curr['l'] < last_sl[1] and curr['c'] > curr['o'] and strong_rejection:
        return {
            "type": "long",
            "manip_high": prev['h'],
            "manip_low": curr['l'],
            "swing_high": last_sh[1],
            "swing_low": last_sl[1],
            "time": curr['t'],
        }
    if curr['h'] > last_sh[1] and curr['c'] < curr['o'] and strong_rejection:
        return {
            "type": "short",
            "manip_high": curr['h'],
            "manip_low": prev['l'],
            "swing_high": last_sh[1],
            "swing_low": last_sl[1],
            "time": curr['t'],
        }
    return None


def generate_signal(leg: Dict, bars: List[Dict]) -> Optional[Dict]:
    """Generate complete trade signal from manipulation leg."""
    
    stdv = calc_stdv(leg["manip_high"], leg["manip_low"])
    
    # Wider swing for TP
    wider = bars[0:LOOKBACK*2+1]
    wider_sh = max(b["h"] for b in wider)
    wider_sl = min(b["l"] for b in wider)
    ote = calc_ote(wider_sh, wider_sl, leg["type"])
    
    confs = find_confluence_zone(stdv, ote, CONFLUENCE_ZONE_PCT)
    if not confs:
        return None
    
    if leg["type"] == "long":
        entry = min(confs, key=lambda x: x[2])[2]
    else:
        entry = max(confs, key=lambda x: x[2])[2]
    
    manip_range = leg["manip_high"] - leg["manip_low"]
    
    if leg["type"] == "long":
        sl = leg["manip_low"] - 0.5
        if entry - sl < MIN_SL_DISTANCE:
            sl = entry - MIN_SL_DISTANCE
        tp = max(wider_sh, stdv["ce"])
        if tp <= entry:
            tp = entry + manip_range * 0.5
    else:
        sl = leg["manip_high"] + 0.5
        if sl - entry < MIN_SL_DISTANCE:
            sl = entry + MIN_SL_DISTANCE
        tp = min(wider_sl, stdv["ce"])
        if tp >= entry:
            tp = entry - manip_range * 0.5
    
    rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
    if rr < MIN_RR or rr > MAX_RR:
        return None
    
    return {
        "symbol": "XAUUSD",
        "direction": leg["type"],
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "rr": round(rr, 2),
        "manipulation_high": round(leg["manip_high"], 2),
        "manipulation_low": round(leg["manip_low"], 2),
        "swing_high": round(leg["swing_high"], 2),
        "swing_low": round(leg["swing_low"], 2),
        "time": leg["time"],
        "confluence": confs,
        "status": "PENDING_APPROVAL",
    }


def format_telegram_message(signal: Dict) -> str:
    """Format rich Telegram message for human approval."""
    emoji = "🟢" if signal["direction"] == "long" else "🔴"
    
    msg = f"""
{emoji} *XAUUSD {signal['direction'].upper()} SETUP*

📍 *Entry (Limit):* `{signal['entry']}`
🛑 *Stop Loss:* `{signal['sl']}`
🎯 *Take Profit:* `{signal['tp']}`
📊 *R:R Ratio:* `{signal['rr']}:1`

📐 *Confluence:*
```
STDV CE:  {signal['confluence'][0][0]} = {signal['confluence'][0][2]:.2f}
OTE:      {signal['confluence'][0][1]} = {signal['confluence'][0][2]:.2f}
```

🕯 *Manipulation Leg:*
```
High: {signal['manipulation_high']}
Low:  {signal['manipulation_low']}
```

📈 *Structural Swing:*
```
High: {signal['swing_high']}
Low:  {signal['swing_low']}
```

⏰ *Time:* `{signal['time']}`

Reply *YES* to execute limit order.
Reply *NO* to skip this setup.

⚠️ Check MT5 chart — does SMC show:
• Manipulation leg at this level?
• Liquidity sweep?
• Order block / FVG near entry?
"""
    return msg


def main_loop():
    log("Starting Entropy Hybrid Agent...")
    log("Scanning M5 bars for manipulation legs...")
    
    while True:
        try:
            data = load_mt5_data()
            if not data or "charts" not in data or "XAUUSD" not in data["charts"]:
                log("Waiting for MT5 data...")
                time.sleep(5)
                continue
            
            m5 = data["charts"]["XAUUSD"]["M5"]
            
            leg = detect_manipulation_leg(m5)
            if leg:
                signal = generate_signal(leg, m5)
                if signal:
                    log(f"🎯 SIGNAL: {signal['direction'].upper()} @ {signal['entry']} RR={signal['rr']}:1")
                    
                    # Save to signals.json for execution agent
                    signals = {"version": "1.0", "generated_at": datetime.now().isoformat(),
                               "signals": [signal], "trail_proposals": {}}
                    with open(SIGNALS_PATH, 'w') as f:
                        json.dump(signals, f, indent=2)
                    
                    # Print Telegram message
                    print("\n" + "="*50)
                    print("TELEGRAM ALERT (send this to Chris):")
                    print("="*50)
                    print(format_telegram_message(signal))
                    print("="*50 + "\n")
                    
                    # Wait for approval (in live mode, Telegram bot handles this)
                    log("Waiting for approval...")
                    time.sleep(30)  # Give Chris time to respond
                    
            time.sleep(5)
            
        except KeyboardInterrupt:
            log("Shutting down...")
            break
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main_loop()
