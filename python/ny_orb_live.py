#!/usr/bin/env python3
"""
ny_orb_live.py — LIVE PAPER runner for the validated NY-Open ORB gold strategy.

Pulls live H1 gold, detects the NY-open breakout per the validated rules, tracks a
PAPER position, and sends Telegram alerts on the existing token. NEVER places a real
order — it does not write to omni_cmd.txt and has no broker connection.

Live data: yfinance GC=F (gold futures, real-time H1) — a live proxy for XAUUSD spot.
(For broker-exact XAUUSD, start MT5 and point this at omni_data.json.)
"""
import os, sys, re, json, time, traceback, urllib.request, urllib.parse, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ny_orb_strategy import DEFAULT, scan_h1, _atr   # validated rules (single source of truth)

ENV   = os.path.join(os.path.dirname(HERE), ".env")
STATE = os.path.join(HERE, "..", "shared", "ny_orb_paper.json")
LOG   = os.path.join(HERE, "..", "logs", "ny_orb_live.log")
RISK_PCT  = float(os.getenv("NYORB_RISK", "0.02"))
START_EQ  = float(os.getenv("NYORB_EQUITY", "160"))
INTERVAL  = int(os.getenv("NYORB_INTERVAL", "300"))     # seconds between checks
SYMBOL    = "XAUUSD (live proxy GC=F)"

def env(k, d=""):
    try:
        m = re.search(rf'^{k}=(.*)$', open(ENV).read(), re.M); return m.group(1).strip() if m else d
    except Exception: return d

TOKEN = env("OMNI_TELEGRAM_TOKEN"); CHAT = env("OMNI_TELEGRAM_CHAT_ID") or "5786598754"

def log(m):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {m}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        open(LOG, "a").write(line + "\n")
    except Exception: pass

def tg(msg):
    if not TOKEN: return
    try:
        data = urllib.parse.urlencode({"chat_id": CHAT, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, timeout=12)
    except Exception as e:
        log(f"telegram send failed: {e}")

def load_state():
    try: return json.load(open(STATE))
    except Exception:
        return {"equity": START_EQ, "position": None, "last_signal_date": None,
                "trades": 0, "wins": 0, "history": []}

def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, "w"), indent=2, default=str)

def fetch_h1():
    import yfinance as yf
    d = yf.Ticker("GC=F").history(period="6d", interval="60m")
    if not len(d): return None
    d.index = d.index.tz_convert("UTC")
    return d

def bars_for_day(d, day):
    out = []
    for ts, r in d.iterrows():
        if ts.date() == day:
            out.append({"open": float(r["Open"]), "high": float(r["High"]),
                        "low": float(r["Low"]), "close": float(r["Close"]), "hour": ts.hour, "ts": ts})
    return out

def recent_atr(d, n):
    bars = [{"high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"])}
            for _, r in d.tail(n + 2).iterrows()]
    return _atr(bars, n)

def check_position(s, d):
    """If a paper position is open, check the latest closed bars for SL/TP (intrabar)."""
    p = s["position"]
    if not p: return
    entry_ts = datetime.datetime.fromisoformat(str(p["entry_ts"]))
    for ts, r in d.iterrows():
        if ts <= entry_ts: continue
        lo, hi = float(r["Low"]), float(r["High"])
        out = None
        if lo <= p["sl"]:   out = ("SL", p["sl"])
        elif hi >= p["tp"]: out = ("TP", p["tp"])
        if out:
            reason, px = out
            pnl = (px - p["entry"]) * p["units"]
            s["equity"] = round(s["equity"] + pnl, 2)
            s["trades"] += 1
            if pnl > 0: s["wins"] += 1
            s["history"].append({"exit_ts": str(ts), "reason": reason, "pnl": round(pnl, 2)})
            s["position"] = None
            emoji = "✅" if pnl > 0 else "🛑"
            tg(f"{emoji} <b>NY-ORB paper {reason}</b>\n{SYMBOL} closed @ {px:.2f}\n"
               f"P&amp;L: <b>${pnl:+.2f}</b> · equity now <b>${s['equity']:.2f}</b>\n"
               f"record: {s['wins']}/{s['trades']} wins · PAPER")
            log(f"EXIT {reason} @ {px:.2f} pnl ${pnl:+.2f} eq ${s['equity']:.2f}")
            return

def look_for_entry(s, d):
    if s["position"]: return
    latest_day = d.index[-1].date()
    if s.get("last_signal_date") == str(latest_day): return   # one trade/day
    today = bars_for_day(d, latest_day)
    if len(today) < DEFAULT["range_bars"] + 1: return
    a5, a14 = recent_atr(d, 5), recent_atr(d, 14)
    sig = scan_h1(today, DEFAULT, atr5=a5, atr14=a14)
    if not sig: return
    sd = sig.entry - sig.sl
    if sd <= 0: return
    units = s["equity"] * RISK_PCT / sd
    s["position"] = {"dir": "BUY", "entry": round(sig.entry, 2), "sl": round(sig.sl, 2),
                     "tp": round(sig.tp, 2), "units": units, "entry_ts": str(today[-1]["ts"])}
    s["last_signal_date"] = str(latest_day)
    risk_usd = s["equity"] * RISK_PCT
    tg(f"🚨 <b>NY-ORB paper SIGNAL — BUY {SYMBOL}</b>\n"
       f"Entry <b>{sig.entry:.2f}</b> · SL {sig.sl:.2f} · TP {sig.tp:.2f} (4R)\n"
       f"Risk ${risk_usd:.2f} ({int(RISK_PCT*100)}%) · ~{units:.2f} oz\n"
       f"{sig.reason}\n<i>PAPER — no real order placed</i>")
    log(f"SIGNAL BUY entry {sig.entry:.2f} sl {sig.sl:.2f} tp {sig.tp:.2f}")

def main():
    log(f"NY-ORB live PAPER runner starting · risk {int(RISK_PCT*100)}% · interval {INTERVAL}s")
    tg(f"🟢 <b>NY-ORB paper bot ONLINE</b>\nWatching {SYMBOL} H1 for NY-open breakouts.\n"
       f"Equity ${load_state()['equity']:.2f} · risk {int(RISK_PCT*100)}% · <i>PAPER mode — alerts only, no live orders</i>")
    while True:
        try:
            d = fetch_h1()
            if d is None or not len(d):
                log("no data this cycle"); time.sleep(INTERVAL); continue
            s = load_state()
            check_position(s, d)
            look_for_entry(s, d)
            s["last_price"] = round(float(d["Close"].iloc[-1]), 2)
            s["updated"] = str(datetime.datetime.now())
            save_state(s)
        except Exception as e:
            log("loop error: " + str(e)); log(traceback.format_exc().splitlines()[-1])
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
