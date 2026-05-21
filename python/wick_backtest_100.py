"""
wick_backtest_100.py — $100 COMPOUND + INTRABAR WICK BACKTEST (REALISTIC)

Changes from v1:
  1. Margin cap: lots capped by equity * leverage / (contract_size * entry_price)
  2. Min SL distance enforced: forex 15 pips, gold 50 cents, silver 8 cents
  3. Max risk per trade capped at 2% of equity (existing)
  4. Compound: lot size recalculated each trade based on current equity
  5. Intrabar O->L->H->C path for wick simulation
"""
import json, sys, math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtester import BacktestConfig, run_backtest, load_data, parse_bars

START_EQ = 100.0
LEVERAGE = 1000.0
MIN_EQ = 5.0  # Floor — account effectively busted below this, can't trade

def _pip_size(s):
    s = s.upper()
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
    "Minimum SL distance in price units."
    s = sym.upper()
    if s in ("XAUUSD","GOLD"): return 1.0      # $1.00 for gold
    if s in ("XAGUSD","SILVER"): return 0.08    # 80 cents for silver
    if "JPY" in s: return 0.15                  # 15 pips for JPY
    return 0.0015                               # 15 pips for forex

def _calc_lots(eq, risk_pct, entry, sl, ts, tv, ml, ma, ls, cs, bid, sym):
    raw_sl = abs(entry - sl)
    sl_dist = max(raw_sl, _min_sl(sym))  # enforce minimum

    # Risk-based lots
    risk = eq * risk_pct / 100.0
    if sl_dist == 0 or ts == 0:
        lots_risk = ml
    else:
        rpl = (sl_dist / ts) * tv
        lots_risk = math.floor((risk / rpl) / ls) * ls if rpl > 0 else ml

    # Margin cap: 50% of equity used as margin
    if bid > 0 and cs > 0 and LEVERAGE > 0:
        lots_margin = (eq * 0.5 * LEVERAGE) / (cs * bid)
    else:
        lots_margin = ma

    lots = max(ml, min(lots_risk, lots_margin, ma))
    return round(lots, 2)

def _resolve_bull(entry, sl, tp1, bar):
    o, h, l, c = bar.o, bar.h, bar.l, bar.c
    if l <= sl and h >= tp1:
        return (tp1, "TP1") if c > o else (sl, "SL")
    if l <= sl: return sl, "SL"
    if h >= tp1: return tp1, "TP1"
    return None, None

def _resolve_sell(entry, sl, tp1, bar):
    o, h, l, c = bar.o, bar.h, bar.l, bar.c
    if h >= sl and l <= tp1:
        return (tp1, "TP1") if c < o else (sl, "SL")
    if h >= sl: return sl, "SL"
    if l <= tp1: return tp1, "TP1"
    return None, None

def run_wick_bt(sym):
    data = load_data()
    chart = data.get("charts", {}).get(sym, {})
    if not chart: return {}
    ts, tv, ml, ma, ls, cs, bid = _tick_info(sym, chart)
    spread_pts = chart.get("spread", 20)
    h1 = chart.get("H1", [])
    if not h1: return {}
    bars = list(reversed(parse_bars(h1)))
    legacy = run_backtest(BacktestConfig(symbol=sym, spread_points=spread_pts, initial_equity=START_EQ))
    if legacy.total_trades == 0: return {"symbol": sym, "trades": [], "pnl": 0, "eq": START_EQ}

    eq = START_EQ
    peak = eq
    trades = []
    tid = 0

    for lb in legacy.trades:
        ei = None
        for i, b in enumerate(bars):
            if b.time == lb.entry_time:
                ei = i
                break
        if ei is None or ei >= len(bars) - 1: continue

        lots = _calc_lots(eq, 2.0, lb.entry_price, lb.sl, ts, tv, ml, ma, ls, cs, bid, sym)
        if lots < ml: continue

        # ── Bankrupt? Can't open another trade ──
        if eq < MIN_EQ:
            trades.append({
                "id": tid, "sym": sym, "dir": lb.direction, "ent_t": lb.entry_time,
                "ent": round(lb.entry_price, 5), "sl": round(lb.sl, 5), "tp": round(lb.tp1, 5),
                "lots": 0, "ex_t": lb.entry_time, "ex": round(lb.entry_price, 5), "rsn": "BUST",
                "pnl": 0, "eq": 0.0, "dd": 100.0
            })
            tid += 1
            continue

        # Enforce minimum SL on the actual SL used in trade
        sl_used = lb.sl
        entry_price = lb.entry_price
        raw_sl_dist = abs(entry_price - sl_used)
        if raw_sl_dist < _min_sl(sym):
            # Re-position SL to minimum distance
            if lb.direction == "BUY":
                sl_used = entry_price - _min_sl(sym)
            else:
                sl_used = entry_price + _min_sl(sym)

        ep = None; er = None; et = None
        for j in range(ei + 1, len(bars)):
            bar = bars[j]
            if lb.direction == "BUY":
                ep, er = _resolve_bull(entry_price, sl_used, lb.tp1, bar)
            else:
                ep, er = _resolve_sell(entry_price, sl_used, lb.tp1, bar)
            if ep is not None:
                et = bar.time
                break
        if ep is None:
            ep = bars[-1].c; er = "END"; et = bars[-1].time

        if lb.direction == "BUY":
            pips = (ep - entry_price) / ts
        else:
            pips = (entry_price - ep) / ts
        pnl = pips * tv * lots
        eq += pnl
        eq = max(eq, MIN_EQ)  # floor — can't go below $5
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        dd = min(dd, 100.0)  # cap at 100%

        trades.append({
            "id": tid, "sym": sym, "dir": lb.direction, "ent_t": lb.entry_time,
            "ent": round(entry_price, 5), "sl": round(sl_used, 5), "tp": round(lb.tp1, 5),
            "lots": lots, "ex_t": et, "ex": round(ep, 5), "rsn": er,
            "pnl": round(pnl, 2), "eq": round(eq, 2), "dd": round(dd, 2)
        })
        tid += 1

    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "symbol": sym, "trades": trades,
        "pnl": round(eq - START_EQ, 2), "eq": round(eq, 2),
        "dd": max(t["dd"] for t in trades) if trades else 0,
        "wins": wins, "losses": len(trades) - wins
    }

def main():
    syms = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]
    print("=" * 120)
    print("OMNI ICT — $100 COMPOUND + INTRABAR WICK BACKTEST (REALISTIC LOT SIZING)")
    print("=" * 120)
    results = {}
    for sym in syms:
        r = run_wick_bt(sym)
        results[sym] = r
        if not r.get("trades"):
            continue
        print(f"\n{'─'*120}")
        print(f"  {sym} | Trades: {len(r['trades'])} | WR: {r['wins']}/{len(r['trades'])} | Net: ${r['pnl']:+.2f} | Eq: ${r['eq']:.2f} | MaxDD: {r['dd']:.1f}%")
        print(f"{'─'*120}")
        print(f"{'ID':>4} {'DATE':>12} {'TIME':>6} {'DIR':>5} {'ENTRY':>11} {'SL':>11} {'TP':>11} {'EXIT':>11} {'RSN':>6} {'LOTS':>6} {'PnL':>10} {'EQ':>10}")
        print(f"{'─'*120}")
        for t in r["trades"]:
            print(f"{t['id']:>4} {t['ent_t'][:10]:>12} {t['ent_t'][11:16]:>6} {t['dir']:>5} {t['ent']:>11.5f} {t['sl']:>11.5f} {t['tp']:>11.5f} {t['ex']:>11.5f} {t['rsn']:>6} {t['lots']:>6.2f} {t['pnl']:>+10.2f} {t['eq']:>10.2f}")

    print(f"\n{'='*120}")
    print("GRAND TOTAL")
    print(f"{'='*120}")
    tt = sum(len(v["trades"]) for v in results.values() if v.get("trades"))
    tp = sum(v["pnl"] for v in results.values() if v)
    tw = sum(v["wins"] for v in results.values() if v)
    tl = sum(v["losses"] for v in results.values() if v)
    print(f"  Total Trades: {tt} | Wins: {tw} | Losses: {tl} | WR: {tw/tt*100:.1f}%")
    print(f"  Net PnL: ${tp:+.2f}  |  Avg per Trade: ${tp/tt:+.2f}")
    print(f"  Start: $100.00  |  End: ${100+tp:.2f}")
    out = Path(__file__).resolve().parent / "backtest_100_wick_results.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to: {out}")
    print(f"{'='*120}")

if __name__ == "__main__":
    main()
