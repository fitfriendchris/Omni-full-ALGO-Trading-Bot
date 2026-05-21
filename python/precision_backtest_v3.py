"""
precision_backtest_v3.py — Multi-Timeframe ICT Backtester v3.0

Uses: H1 for signal generation (from existing backtester)
      M15 for wick-based SL/TP resolution (intrabar simulation)
      D1 for trend bias filter

Rules enforced:
  - Session gate (only London Open 07-09 UTC, NY Open 12-14 UTC)
  - HTF alignment (D1 direction)
  - Quarter theory
  - Minimum SL distances
  - Compound lot sizing with margin cap
  - No negative equity (margin call at $0)
  - 3-loss circuit breaker
  - Equity tier gates for metals

Output: per-trade detail + equity curve + summary JSON
"""
import json, sys, math, os
from pathlib import Path
from collections import namedtuple
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtester import BacktestConfig, run_backtest, load_data, parse_bars

Bar = namedtuple("Bar", ["time","o","h","l","c"])
START_EQ = 100.0
LEVERAGE = 1000.0

def _to_bar(raw):
    return Bar(raw["t"], raw["o"], raw["h"], raw["l"], raw["c"])

def _parse(tf_dict):
    return [_to_bar(b) for b in reversed(tf_dict)]

def _hour_utc(ts: str):
    try:
        return int(ts[11:13])
    except:
        return 0

def _is_valid_session(ts):
    h = _hour_utc(ts)
    # London Open 07-09, NY Open 12-14
    return h in (7, 8, 9, 12, 13, 14)

def _pip_size(s):
    s=s.upper()
    if "JPY" in s: return 0.01
    if s in ("XAUUSD","GOLD"): return 0.01
    if s in ("XAGUSD","SILVER"): return 0.001
    return 0.0001

def _tick_info(sym, chart):
    t = float(chart.get("tick_size", _pip_size(sym)))
    tv = float(chart.get("tick_value", 1.0))
    ml = float(chart.get("min_lot", 0.01))
    ma = float(chart.get("max_lot", 1000.0))
    ls = float(chart.get("lot_step", 0.01))
    cs = float(chart.get("contract_size", 100000.0))
    bid = float(chart.get("bid", 0) or 0)
    return t, tv, ml, ma, ls, cs, bid

def _min_sl(sym):
    s = sym.upper()
    if s in ("XAUUSD","GOLD"): return 1.0
    if s in ("XAGUSD","SILVER"): return 0.08
    if "JPY" in s: return 0.15
    return 0.0015

def _calc_lots(eq, risk_pct, entry, sl, ts, tv, ml, ma, ls, cs, bid):
    sl_dist = max(abs(entry-sl), _min_sl(entry))
    risk = eq * risk_pct / 100.0
    if sl_dist == 0 or ts == 0: return ml
    rpl = (sl_dist / ts) * tv
    lots_risk = math.floor((risk / rpl) / ls) * ls if rpl > 0 else ml
    if bid > 0 and cs > 0 and LEVERAGE > 0:
        lots_margin = (eq * 0.5 * LEVERAGE) / (cs * bid)
    else:
        lots_margin = ma
    lots = max(ml, min(lots_risk, lots_margin, ma))
    return round(lots, 2)

def _get_d1_bias(d1_bars, entry_time):
    """Return 'bull','bear','neutral' based on D1 candle at entry."""
    if not d1_bars:
        return "neutral"
    et = entry_time[:10]
    for b in d1_bars:
        if b.time[:10] == et:
            return "bull" if b.c > b.o else "bear" if b.c < b.o else "neutral"
    return "neutral"

def _resolve_m15(entry, sl, tp, direction, m15_bars, entry_idx):
    """Walk M15 bars after entry, return (exit_price, exit_reason, exit_time)."""
    for j in range(entry_idx, len(m15_bars)):
        bar = m15_bars[j]
        if direction == "BUY":
            if bar.l <= sl and bar.h >= tp:
                return (tp, "TP1", bar.time) if bar.c > bar.o else (sl, "SL", bar.time)
            if bar.l <= sl: return sl, "SL", bar.time
            if bar.h >= tp: return tp, "TP1", bar.time
            if bar.h >= entry + abs(entry-sl)*2 and bar.c < bar.h:  # 2R hit, trail
                return bar.c, "TRAIL", bar.time
        else:
            if bar.h >= sl and bar.l <= tp:
                return (tp, "TP1", bar.time) if bar.c < bar.o else (sl, "SL", bar.time)
            if bar.h >= sl: return sl, "SL", bar.time
            if bar.l <= tp: return tp, "TP1", bar.time
            if bar.l <= entry - abs(entry-sl)*2 and bar.c > bar.l:
                return bar.c, "TRAIL", bar.time
    return m15_bars[-1].c, "END", m15_bars[-1].time

def run_precision_bt(sym, equity_tiers=True):
    data = load_data()
    chart = data.get("charts", {}).get(sym, {})
    if not chart: return {}
    
    ts, tv, ml, ma, ls, cs, bid = _tick_info(sym, chart)
    spread_pts = chart.get("spread", 20)
    
    h1_raw = chart.get("H1", [])
    m15_raw = chart.get("M15", [])
    d1_raw = chart.get("D1", [])
    if not h1_raw or not m15_raw: return {}
    
    h1_bars = parse_bars(h1_raw)  # already reversed oldest-first
    m15_bars = _parse(m15_raw)
    d1_bars = _parse(d1_raw) if d1_raw else []
    
    # Generate H1 signals
    legacy = run_backtest(BacktestConfig(symbol=sym, spread_points=spread_pts, initial_equity=START_EQ))
    if legacy.total_trades == 0: return {"symbol": sym, "trades": [], "pnl": 0, "eq": START_EQ}
    
    # Load rules for equity gates
    rules_path = Path(__file__).resolve().parent / "rules.json"
    rules = json.load(open(rules_path)) if rules_path.exists() else {}
    gate = rules.get("symbol_overrides", {}).get(sym, {}).get("equity_gate")
    
    eq = START_EQ
    peak = eq
    trades = []
    tid = 0
    consecutive_losses = 0
    
    for lb in legacy.trades:
        if not _is_valid_session(lb.entry_time):
            continue
        
        # D1 bias filter
        d1_bias = _get_d1_bias(d1_bars, lb.entry_time)
        if d1_bias == "bull" and lb.direction == "SELL": continue
        if d1_bias == "bear" and lb.direction == "BUY": continue
        
        # Equity tier gate
        if equity_tiers and gate:
            min_eq = float(gate.get("min_equity_usd", 0))
            conf = lb.confidence if hasattr(lb, 'confidence') else 50
            if eq < min_eq and conf < gate.get("override_confidence", 999):
                continue
        
        # Circuit breaker
        if consecutive_losses >= 3:
            break
        
        # Find entry in M15
        ei = None
        for i, b in enumerate(m15_bars):
            if b.time[:13] == lb.entry_time[:13]:  # match hour
                ei = i
                break
        if ei is None or ei >= len(m15_bars)-1:
            continue
        
        # Enforce minimum SL
        raw_sl = lb.sl
        if abs(lb.entry_price - raw_sl) < _min_sl(sym):
            raw_sl = lb.entry_price - _min_sl(sym) if lb.direction=="BUY" else lb.entry_price + _min_sl(sym)
        
        lots = _calc_lots(eq, 2.0, lb.entry_price, raw_sl, ts, tv, ml, ma, ls, cs, bid)
        if lots < ml: continue
        
        ep, er, et = _resolve_m15(lb.entry_price, raw_sl, lb.tp1, lb.direction, m15_bars, ei+1)
        
        if lb.direction == "BUY":
            pips = (ep - lb.entry_price) / ts
        else:
            pips = (lb.entry_price - ep) / ts
        pnl = pips * tv * lots
        
        eq += pnl
        if eq < 0:
            eq = 0
            pnl = -eq_at_risk
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        
        trades.append({
            "id": tid, "sym": sym, "dir": lb.direction, "ent_t": lb.entry_time,
            "ent": round(lb.entry_price,5), "sl": round(raw_sl,5), "tp": round(lb.tp1,5),
            "lots": lots, "ex_t": et, "ex": round(ep,5), "rsn": er,
            "pnl": round(pnl,2), "eq": round(eq,2), "dd": round(dd,2),
            "d1_bias": d1_bias, "session": _hour_utc(lb.entry_time)
        })
        
        if pnl < 0:
            consecutive_losses += 1
        else:
            consecutive_losses = 0
        tid += 1
    
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "symbol": sym, "trades": trades,
        "pnl": round(eq - START_EQ, 2), "eq": round(eq, 2),
        "dd": max((t["dd"] for t in trades), default=0),
        "wins": wins, "losses": len(trades)-wins
    }

def main():
    syms = ["EURUSD","GBPUSD","AUDUSD","USDCAD","USDJPY"]
    print("="*130)
    print("OMNI ICT PRECISION BACKTEST v3.0 — $100 COMPOUND + MULTI-TIMEFRAME")
    print("Rules: D1 bias filter, Session gate (London/NY), M15 intrabar, Margin cap, No neg equity")
    print("="*130)
    
    results = {}
    for sym in syms:
        r = run_precision_bt(sym, equity_tiers=True)
        results[sym] = r
        if not r.get("trades"): continue
        print(f"\n{'─'*130}")
        print(f"  {sym} | {len(r['trades'])} trades | WR: {r['wins']}/{len(r['trades'])} | Net: ${r['pnl']:+.2f} | Eq: ${r['eq']:.2f} | MaxDD: {r['dd']:.1f}%")
        print(f"{'─'*130}")
        print(f"{'ID':>4} {'DATE':>12} {'HR':>4} {'DIR':>5} {'ENTRY':>11} {'SL':>11} {'TP':>11} {'EXIT':>11} {'RSN':>6} {'Lots':>6} {'PnL':>10} {'EQ':>10} {'SESSION':>8}")
        print(f"{'─'*130}")
        for t in r["trades"]:
            print(f"{t['id']:>4} {t['ent_t'][:10]:>12} {_hour_utc(t['ent_t']):>4} {t['dir']:>5} {t['ent']:>11.5f} {t['sl']:>11.5f} {t['tp']:>11.5f} {t['ex']:>11.5f} {t['rsn']:>6} {t['lots']:>6.2f} {t['pnl']:>+10.2f} {t['eq']:>10.2f} {'LO' if t['session'] in (7,8,9) else 'NY':>8}")
    
    # Metals (will be gated off at $100)
    for sym in ["XAUUSD","XAGUSD"]:
        r = run_precision_bt(sym, equity_tiers=True)
        results[sym] = r
    
    print(f"\n{'='*130}")
    print("GRAND TOTAL (FOREX + GATED METALS)")
    print(f"{'='*130}")
    tt = sum(len(v["trades"]) for v in results.values() if v.get("trades"))
    tp = sum(v["pnl"] for v in results.values() if v)
    tw = sum(v["wins"] for v in results.values() if v)
    print(f"  Total Trades: {tt} | Wins: {tw} | Losses: {tt-tw} | WR: {tw/tt*100:.1f}% | Net: ${tp:+.2f}")
    print(f"  Start: $100.00  |  End: ${100+tp:.2f}")
    
    out = Path(__file__).resolve().parent / "precision_backtest_v3_results.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to: {out}")

if __name__ == "__main__":
    main()
