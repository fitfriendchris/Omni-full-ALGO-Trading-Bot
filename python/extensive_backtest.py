"""
extensive_backtest.py
Comprehensive validation suite for the proven ICT engine.
Runs: multi-seed, parameter sweep, Monte Carlo, vs buy-and-hold,
      realistic slippage matrix, monthly breakdown.
"""
import random, statistics, json, time, math
import pandas as pd
from dataclasses import replace as dc_replace
from deterministic_ict_proven_backtest import EngineConfig, Engine, Simulator, fetch_regime, Bar
from datetime import datetime

OUT = []
def log(msg):
    OUT.append(msg)
    print(msg)

def run_config(seed, period, strength, slippage_adj, years=2):
    """Run one backtest configuration. Returns dict of results."""
    random.seed(seed)
    ltf, htf, desc = fetch_regime(period)
    cfg = EngineConfig(
        execution_mode="MARKET_ONLY", sl_cap_pips=200.0, fill_window=96,
        session="LONDON", min_rr=2.0, lookback=50, stop_buffer_pips=2.0,
        max_spread_pips=50.0 + slippage_adj, stricter_slippage=True,
        min_bar_range_pips=0.0, signal_strength_threshold=strength
    )
    eng = Engine("XAUUSD")
    sigs = eng.generate(ltf, htf, cfg)
    random.seed(seed)
    res = Simulator("XAUUSD", 10000, 100, 0.01).run(ltf, sigs, cfg)

    # Real WR (only TP2 hits count as wins)
    real_wins = sum(1 for t in res.trade_log if t['pnl_usd'] > 0 and 'TP2' in t['reason'])
    real_losses = sum(1 for t in res.trade_log if t['pnl_usd'] < 0 and 'SL' in t['reason'])
    wr = real_wins/(real_wins+real_losses)*100 if (real_wins+real_losses) else 0
    return {
        'seed': seed, 'period': period, 'strength': strength,
        'slippage_adj': slippage_adj, 'signals': len(sigs),
        'trades': res.total_trades, 'wins': real_wins, 'losses': real_losses,
        'wr': wr, 'pnl': res.total_pnl_pct, 'dd': res.max_dd_pct,
        'pf': res.profit_factor, 'kelly': res.kelly_fraction,
        'avg_dd': res.avg_dd_pct,
        'ltf': ltf, 'htf': htf, 'cfg': cfg, 'res': res
    }

# ═══════════════════════════════════════════════════════════════════════════════
log("="*100)
log("EXTENSIVE BACKTEST — PROVEN ICT ENGINE v5")
log("="*100)
log(f"Run at: {datetime.now().isoformat()}")
log(f"Base config: XAUUSD, London session, strength>=0.6, 1% risk/trade")
log("")

# ═══════════════════════════════════════════════════════════════════════════════
# 1. BASE CASE (seed 42)
log("TEST 1: BASE CASE (seed 42, 2yr bull regime)")
log("-"*100)
base = run_config(42, "bull_2024_2026", 0.6, 0)
log(f"  Signals:     {base['signals']}")
log(f"  Trades:      {base['trades']}")
log(f"  Wins (TP2):  {base['wins']}")
log(f"  Losses (SL): {base['losses']}")
log(f"  Real WR:     {base['wr']:.1f}%")
log(f"  PnL:         +${base['pnl']:.2f}%")
log(f"  Max DD:      {base['dd']:.1f}%")
log(f"  Avg DD:      {base['avg_dd']:.1f}%")
log(f"  PF:          {base['pf']:.1f}")
log(f"  Kelly:       {base['kelly']:.2f}")
log("")

# ═══════════════════════════════════════════════════════════════════════════════
# 2. MULTI-SEED ROBUSTNESS
log("TEST 2: MULTI-SEED ROBUSTNESS (10 random seeds)")
log("-"*100)
multi_results = []
for seed in [0, 7, 13, 21, 42, 55, 77, 88, 99, 123]:
    r = run_config(seed, "bull_2024_2026", 0.6, 0)
    multi_results.append(r)
    log(f"  Seed {seed:>3}: {r['wins']}W/{r['losses']}L = {r['wr']:.1f}%  |  +{r['pnl']:.1f}%  |  {r['dd']:.1f}% DD  |  PF={r['pf']:.1f}  |  {r['signals']} sigs")

wrs = [m['wr'] for m in multi_results]
pnls = [m['pnl'] for m in multi_results]
dds = [m['dd'] for m in multi_results]
log(f"  Summary: WR range {min(wrs):.1f}%–{max(wrs):.1f}%  avg {statistics.mean(wrs):.1f}% ± {statistics.stdev(wrs):.1f}%")
log(f"           PnL range +{min(pnls):.1f}%–+{max(pnls):.1f}%  avg +{statistics.mean(pnls):.1f}%")
log(f"           DD range {min(dds):.1f}%–{max(dds):.1f}%  avg {statistics.mean(dds):.1f}%")
log("")

# ═══════════════════════════════════════════════════════════════════════════════
# 3. PARAMETER SWEEP (signal strength × slippage)
log("TEST 3: SIGNAL STRENGTH × SLIPPAGE SENSITIVITY")
log("-"*100)
log(f"{'Strength':<10} {'Slippage+':<10} {'Sigs':<6} {'Trades':<8} {'WR':<8} {'PnL':<10} {'DD':<8} {'PF':<8}")
log("-"*100)
sweep_results = []
for strength in [0.0, 0.3, 0.5, 0.6, 0.7, 0.8]:
    for slip_adj in [0, 10, 25, 50]:
        r = run_config(42, "bull_2024_2026", strength, slip_adj)
        sweep_results.append(r)
        log(f"{strength:<10.1f} {'+'+str(slip_adj):10} {r['signals']:<6} {r['trades']:<8} {r['wr']:.1f}%   +{r['pnl']:.1f}%   {r['dd']:.1f}%   {r['pf']:.1f}")

best_wr = max(sweep_results, key=lambda x: x['wr'])
best_pf = max(sweep_results, key=lambda x: x['pf'])
best_dd = min(sweep_results, key=lambda x: x['dd'])
log(f"\n  Best WR:  strength={best_wr['strength']}, slip=+{best_wr['slippage_adj']} → {best_wr['wr']:.1f}%")
log(f"  Best PF:  strength={best_pf['strength']}, slip=+{best_pf['slippage_adj']} → PF={best_pf['pf']:.1f}")
log(f"  Best DD:  strength={best_dd['strength']}, slip=+{best_dd['slippage_adj']} → DD={best_dd['dd']:.1f}%")
log("")

# ═══════════════════════════════════════════════════════════════════════════════
# 4. WALK-FORWARD (3 splits: 50/50, 60/40, 70/30)
log("TEST 4: WALK-FORWARD ANALYSIS")
log("-"*100)
ltf, htf, _ = fetch_regime("bull_2024_2026")

for split_pct, label in [(0.5, "50/50"), (0.6, "60/40"), (0.7, "70/30")]:
    split_idx = int(len(ltf) * split_pct)
    
    # Train: optimize strength
    best_strength = 0.6
    best_train_wr = 0
    for s in [0.0, 0.3, 0.5, 0.6, 0.7, 0.8]:
        cfg = EngineConfig("MARKET_ONLY", 200.0, 96, "LONDON", 2.0, 50, 2.0, 50.0, True, 0.0, s)
        random.seed(42)
        sigs = Engine("XAUUSD").generate(ltf[:split_idx], htf[:min(split_idx, len(htf))], cfg)
        random.seed(42)
        res = Simulator("XAUUSD", 10000, 100, 0.01).run(ltf[:split_idx], sigs, cfg)
        w = sum(1 for t in res.trade_log if t['pnl_usd'] > 0 and 'TP2' in t['reason'])
        l = sum(1 for t in res.trade_log if t['pnl_usd'] < 0 and 'SL' in t['reason'])
        wr = w/(w+l)*100 if w+l else 0
        if wr > best_train_wr:
            best_train_wr = wr
            best_strength = s
    
    # Test: run with optimized strength on test period ONLY
    cfg_te = EngineConfig("MARKET_ONLY", 200.0, 96, "LONDON", 2.0, 50, 2.0, 50.0, True, 0.0, best_strength)
    random.seed(42)
    sigs_te = Engine("XAUUSD").generate(ltf[split_idx:], htf[split_idx:len(htf)], cfg_te)
    # Correct bar_idx for sliced bars
    sigs_te = [dc_replace(s, bar_idx=s.bar_idx) for s in sigs_te]  # bar_idx already relative to slice start
    random.seed(42)
    res_te = Simulator("XAUUSD", 10000, 100, 0.01).run(ltf[split_idx:], sigs_te, cfg_te)
    
    w = sum(1 for t in res_te.trade_log if t['pnl_usd'] > 0 and 'TP2' in t['reason'])
    l = sum(1 for t in res_te.trade_log if t['pnl_usd'] < 0 and 'SL' in t['reason'])
    wr = w/(w+l)*100 if w+l else 0
    log(f"  Split {label}: Train strength={best_strength}, WR={best_train_wr:.1f}%  |  Test: {w}W/{l}L={wr:.1f}%  PnL=+{res_te.total_pnl_pct:.1f}%  DD={res_te.max_dd_pct:.1f}%")

log("")

# ═══════════════════════════════════════════════════════════════════════════════
# 5. MONTE CARLO (shuffle trade order)
log("TEST 5: MONTE CARLO SIMULATION (1000 runs)")
log("-"*100)
trade_pnls = [t['pnl_usd'] for t in base['res'].trade_log if t['pnl_usd'] != 0]

mc_wrs, mc_pnls, mc_dds = [], [], []
for _ in range(1000):
    random.shuffle(trade_pnls)
    eq, peak, mc_dd, wins, losses = 10000, 10000, 0, 0, 0
    for pnl in trade_pnls:
        eq += pnl
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100
        if dd > mc_dd: mc_dd = dd
        if pnl > 0: wins += 1
        elif pnl < 0: losses += 1
    mc_wrs.append(wins/(wins+losses)*100 if wins+losses else 0)
    mc_pnls.append((eq - 10000) / 10000 * 100)
    mc_dds.append(mc_dd)

mc_wrs.sort(); mc_pnls.sort(); mc_dds.sort()
log(f"  Base:    WR={base['wr']:.1f}%  PnL=+{base['pnl']:.1f}%  DD={base['dd']:.1f}%")
log(f"  Median:  WR={statistics.median(mc_wrs):.1f}%  PnL=+{statistics.median(mc_pnls):.1f}%  DD={statistics.median(mc_dds):.1f}%")
log(f"  5th %ile:WR={mc_wrs[50]:.1f}%  PnL=+{mc_pnls[50]:.1f}%  DD={mc_dds[50]:.1f}%")
log(f"  95th %ile:WR={mc_wrs[950]:.1f}%  PnL=+{mc_pnls[950]:.1f}%  DD={mc_dds[950]:.1f}%")
log(f"  Worst:   WR={min(mc_wrs):.1f}%  PnL={min(mc_pnls):.1f}%  DD={max(mc_dds):.1f}%")
log("")

# ═══════════════════════════════════════════════════════════════════════════════
# 6. BUY-AND-HOLD BENCHMARK
log("TEST 6: VS BUY-AND-HOLD")
log("-"*100)
start_price = ltf[0].close
end_price = ltf[-1].close
ba_h_pnl = (end_price - start_price) / start_price * 100
peak = ltf[0].close
ba_h_dd = 0
for b in ltf:
    if b.close > peak: peak = b.close
    dd = (peak - b.close) / peak * 100
    if dd > ba_h_dd: ba_h_dd = dd

log(f"  Buy & Hold: +{ba_h_pnl:.1f}%  DD={ba_h_dd:.1f}%")
log(f"  ICT Engine: +{base['pnl']:.1f}%  DD={base['dd']:.1f}%")
log(f"  Edge: PnL +{base['pnl'] - ba_h_pnl:.1f}pp  DD saved {ba_h_dd - base['dd']:.1f}pp")
log("")

# ═══════════════════════════════════════════════════════════════════════════════
# 7. MONTHLY BREAKDOWN
log("TEST 7: MONTHLY PERFORMANCE")
log("-"*100)
monthly = {}
for t in base['res'].trade_log:
    # Extract month from bar timestamp
    bar_idx = t.get('open_idx', 0)
    if bar_idx < len(ltf):
        bar_time = ltf[bar_idx].time
        if isinstance(bar_time, pd.Timestamp):
            ym = bar_time.strftime("%Y-%m")
        else:
            ym = str(bar_time)[:7]
    else:
        ym = "N/A"
    if ym not in monthly: monthly[ym] = {'wins': 0, 'losses': 0, 'pnl': 0, 'trades': 0}
    monthly[ym]['trades'] += 1
    monthly[ym]['pnl'] += t['pnl_usd']
    if t['pnl_usd'] > 0 and 'TP2' in t['reason']:
        monthly[ym]['wins'] += 1
    elif t['pnl_usd'] < 0 and 'SL' in t['reason']:
        monthly[ym]['losses'] += 1

log(f"{'Month':<10} {'Trades':<10} {'Wins':<6} {'Losses':<8} {'WR':<8} {'PnL':<12}")
log("-"*100)
for m in sorted(monthly):
    d = monthly[m]
    wr = d['wins']/(d['wins']+d['losses'])*100 if d['wins']+d['losses'] else 0
    log(f"{m:<10} {d['trades']:<10} {d['wins']:<6} {d['losses']:<8} {wr:.1f}%  ${d['pnl']:10.2f}")

profitable = sum(1 for d in monthly.values() if d['pnl'] > 0)
log(f"\n  Profitable months: {profitable}/{len(monthly)} ({profitable/len(monthly)*100:.0f}%)")
log("")

# ═══════════════════════════════════════════════════════════════════════════════
# 8. EXPECTANCY & RISK METRICS
log("TEST 8: STATISTICAL SUMMARY")
log("-"*100)
win_sizes = [t['pnl_usd'] for t in base['res'].trade_log if t['pnl_usd'] > 0]
loss_sizes = [abs(t['pnl_usd']) for t in base['res'].trade_log if t['pnl_usd'] < 0]
avg_win = statistics.mean(win_sizes) if win_sizes else 0
avg_loss = statistics.mean(loss_sizes) if loss_sizes else 1
expectancy = (base['wr']/100 * avg_win) - ((100-base['wr'])/100 * avg_loss)
risk_of_ruin = ((100-base['wr'])/100 / (base['wr']/100)) ** 10 if base['wr'] > 0 else 1

log(f"  Trades/year:       ~{base['trades']/2:.0f}")
log(f"  Real WR:           {base['wr']:.1f}%")
log(f"  Avg win:           ${avg_win:.2f}")
log(f"  Avg loss:          ${avg_loss:.2f}")
log(f"  Expectancy/trade:  ${expectancy:.2f}")
log(f"  Profit factor:     {base['pf']:.1f}")
log(f"  Max drawdown:      {base['dd']:.1f}%")
log(f"  Avg drawdown:      {base['avg_dd']:.1f}%")
log(f"  Kelly fraction:    {base['kelly']:.2f}")
log(f"  Risk of ruin:      {risk_of_ruin:.4f}")
log("")

log("="*100)
log("EXTENSIVE BACKTEST COMPLETE")
log("="*100)

report_path = "/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/EXTENSIVE_BACKTEST_REPORT.txt"
with open(report_path, "w") as f:
    f.write("\n".join(OUT))
print(f"\n[Saved to {report_path}]")
