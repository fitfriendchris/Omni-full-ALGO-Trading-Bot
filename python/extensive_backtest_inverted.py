"""
extensive_backtest_inverted.py
Run the proven ICT engine on INVERTED chart data.
This turns a 2yr bull into a 2yr bear (and vice versa).
Tests symmetry and whether the engine is directionally biased.
"""
import random, statistics, json, time, math
from dataclasses import replace as dc_replace
from deterministic_ict_proven_backtest import EngineConfig, Engine, Simulator, fetch_regime, Bar
from datetime import datetime

OUT = []
def log(msg):
    OUT.append(msg)
    print(msg)

def invert_bars(bars):
    """Invert OHLC prices around zero — turns bull into bear and vice versa."""
    from deterministic_ict_proven_backtest import Bar
    inverted = []
    for b in bars:
        inverted.append(Bar(
            time=b.time,
            open=-b.open,
            high=-b.low,
            low=-b.high,
            close=-b.close,
            volume=b.volume
        ))
    return inverted

def run_on_inverted(seed=42, strength=0.6):
    ltf, htf, desc = fetch_regime("bull_2024_2026")
    
    # Invert both LTF and HTF
    ltf_inv = invert_bars(ltf)
    htf_inv = invert_bars(htf)
    
    cfg = EngineConfig(
        execution_mode="MARKET_ONLY", sl_cap_pips=200.0, fill_window=96,
        session="LONDON", min_rr=2.0, lookback=50, stop_buffer_pips=2.0,
        max_spread_pips=50.0, stricter_slippage=True,
        min_bar_range_pips=0.0, signal_strength_threshold=strength
    )
    random.seed(seed)
    sigs = Engine("XAUUSD").generate(ltf_inv, htf_inv, cfg)
    random.seed(seed)
    res = Simulator("XAUUSD", 10000, 100, 0.01).run(ltf_inv, sigs, cfg)
    
    real_wins = sum(1 for t in res.trade_log if t['pnl_usd'] > 0 and 'TP2' in t['reason'])
    real_losses = sum(1 for t in res.trade_log if t['pnl_usd'] < 0 and 'SL' in t['reason'])
    wr = real_wins/(real_wins+real_losses)*100 if (real_wins+real_losses) else 0
    return {
        'signals': len(sigs), 'wins': real_wins, 'losses': real_losses,
        'wr': wr, 'pnl': res.total_pnl_pct, 'dd': res.max_dd_pct,
        'pf': res.profit_factor, 'kelly': res.kelly_fraction,
        'trades': res.total_trades, 'res': res, 'ltf_inv': ltf_inv
    }

log("="*100)
log("PROVEN ICT ENGINE — INVERTED CHART STRESS TEST")
log("="*100)
log(f"Run at: {datetime.now().isoformat()}")
log("")
log("Inverting the 2yr XAUUSD bull ($2300→$3400) into a bear ($-2300→$-3400)")
log("This tests structural symmetry — bull setups become bear setups.")
log("")

# Run the inverted backtest
inv = run_on_inverted(42, 0.6)

log("RESULTS ON INVERTED CHART:")
log("-"*100)
log(f"  Signals:     {inv['signals']}")
log(f"  Trades:      {inv['trades']}")
log(f"  Wins (TP2):  {inv['wins']}")
log(f"  Losses (SL): {inv['losses']}")
log(f"  Real WR:     {inv['wr']:.1f}%")
log(f"  PnL:         +${inv['pnl']:.2f}%")
log(f"  Max DD:      {inv['dd']:.1f}%")
log(f"  PF:          {inv['pf']:.1f}")
log(f"  Kelly:       {inv['kelly']:.2f}")
log("")

# Compare inverted vs original
log("COMPARISON: NORMAL vs INVERTED")
log("-"*100)
log(f"{'Metric':<20} {'Normal Chart':<18} {'Inverted Chart':<18} {'Diff':<15}")
log("-"*100)

# Fetch original for comparison
random.seed(42)
ltf, htf, _ = fetch_regime("bull_2024_2026")
cfg = EngineConfig("MARKET_ONLY", 200.0, 96, "LONDON", 2.0, 50, 2.0, 50.0, True, 0.0, 0.6)
eng = Engine("XAUUSD")
sigs = eng.generate(ltf, htf, cfg)
random.seed(42)
res_orig = Simulator("XAUUSD", 10000, 100, 0.01).run(ltf, sigs, cfg)
wins_orig = sum(1 for t in res_orig.trade_log if t['pnl_usd'] > 0 and 'TP2' in t['reason'])
losses_orig = sum(1 for t in res_orig.trade_log if t['pnl_usd'] < 0 and 'SL' in t['reason'])
wr_orig = wins_orig/(wins_orig+losses_orig)*100 if (wins_orig+losses_orig) else 0

metrics = [
    ('Signals', len(sigs), inv['signals'], len(sigs) - inv['signals']),
    ('Trades', res_orig.total_trades, inv['trades'], res_orig.total_trades - inv['trades']),
    ('Wins (TP2)', wins_orig, inv['wins'], wins_orig - inv['wins']),
    ('Losses (SL)', losses_orig, inv['losses'], losses_orig - inv['losses']),
    ('WR %', f"{wr_orig:.1f}%", f"{inv['wr']:.1f}%", f"{wr_orig - inv['wr']:.1f}pp"),
    ('PnL %', f"+{res_orig.total_pnl_pct:.1f}%", f"+{inv['pnl']:.1f}%", f"{res_orig.total_pnl_pct - inv['pnl']:.1f}pp"),
    ('Max DD %', f"{res_orig.max_dd_pct:.1f}%", f"{inv['dd']:.1f}%", f"{res_orig.max_dd_pct - inv['dd']:.1f}pp"),
    ('PF', f"{res_orig.profit_factor:.1f}", f"{inv['pf']:.1f}", f"{res_orig.profit_factor - inv['pf']:.1f}"),
]

for m in metrics:
    log(f"{m[0]:<20} {str(m[1]):<18} {str(m[2]):<18} {str(m[3]):<15}")

log("")

# Do inverted monthly breakdown
log("MONTHLY PERFORMANCE ON INVERTED CHART:")
log("-"*100)
monthly = {}
for t in inv['res'].trade_log:
    bar_idx = t.get('open_idx', 0)
    if bar_idx < len(inv['ltf_inv']):
        bar_time = inv['ltf_inv'][bar_idx].time
        ym = str(bar_time)[:7] if not hasattr(bar_time, 'strftime') else bar_time.strftime("%Y-%m")
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

# Test multiple strengths on inverted
log("SIGNAL STRENGTH SWEEP ON INVERTED DATA:")
log("-"*100)
log(f"{'Strength':<12} {'Trades':<10} {'Wins':<8} {'Losses':<10} {'WR':<10} {'PnL':<12} {'DD':<10} {'PF':<10}")
log("-"*100)
for strength in [0.0, 0.3, 0.5, 0.6, 0.7, 0.8]:
    r = run_on_inverted(42, strength)
    log(f"{strength:<12.1f} {r['trades']:<10} {r['wins']:<8} {r['losses']:<10} {r['wr']:<10.1f} +{r['pnl']:.1f}%    {r['dd']:.1f}%     {r['pf']:.1f}")

log("")

# Multi-seed on inverted
log("MULTI-SEED ON INVERTED DATA (10 seeds):")
log("-"*100)
for seed in [0, 7, 13, 21, 42, 55, 77, 88, 99, 123]:
    r = run_on_inverted(seed, 0.6)
    log(f"  Seed {seed:>3}: {r['wins']}W/{r['losses']}L = {r['wr']:.1f}%  |  +{r['pnl']:.1f}%  |  {r['dd']:.1f}% DD  |  PF={r['pf']:.1f}  |  {r['signals']} sigs")

log("")

# Asymmetry analysis
log("ASYMMETRY ANALYSIS:")
log("-"*100)
direction_orig = {'BULL': 0, 'BEAR': 0}
direction_inv = {'BULL': 0, 'BEAR': 0}

random.seed(42)
ltf, htf, _ = fetch_regime("bull_2024_2026")
cfg = EngineConfig("MARKET_ONLY", 200.0, 96, "LONDON", 2.0, 50, 2.0, 50.0, True, 0.0, 0.6)
sigs_orig = Engine("XAUUSD").generate(ltf, htf, cfg)
for s in sigs_orig:
    direction_orig[s.direction] += 1

ltf_inv = invert_bars(ltf)
htf_inv = invert_bars(htf)
sigs_inv = Engine("XAUUSD").generate(ltf_inv, htf_inv, cfg)
for s in sigs_inv:
    direction_inv[s.direction] += 1

log(f"  Normal chart:  BULL={direction_orig['BULL']}  BEAR={direction_orig['BEAR']}  (expected: mostly BULL in bull)")
log(f"  Inverted chart: BULL={direction_inv['BULL']}  BEAR={direction_inv['BEAR']}  (expected: mostly BEAR if symmetric)")
log(f"  Asymmetry score: BULL bias = {(direction_orig['BULL'] - direction_inv['BULL'])/max(direction_orig['BULL'], 1)*100:.0f}%")

log("")
log("="*100)
log("INVERTED CHART TEST COMPLETE")
log("="*100)

report_path = "/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/INVERTED_BACKTEST_REPORT.txt"
with open(report_path, "w") as f:
    f.write("\n".join(OUT))
print(f"\n[Saved to {report_path}]")
