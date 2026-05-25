"""proven_meta_runner.py
Run deterministic ICT engine across regimes and configs.
Outputs a ranked comparison with honest caveats.
"""

import json, math, sys
from dataclasses import asdict
from deterministic_ict_proven_backtest import EngineConfig, run_config_on_regime

REGIMES = ["bull_2024_2026", "bear_2022", "mixed_2020_2024"]
CONFIGS = [
    ("MKT+200+LONDON",  EngineConfig("MARKET_ONLY", 200.0, 96,  "LONDON", 2.0, 50, 2.0, 50.0, True)),
    ("MKT+200+ALL",     EngineConfig("MARKET_ONLY", 200.0, 96,  "ALL",    1.5, 50, 2.0, 50.0, True)),
    ("MKT+200+LONDON-L",EngineConfig("MARKET_ONLY", 200.0, 96,  "LONDON", 2.0, 50, 2.0, 50.0, False)),
    ("LIM+200+LONDON",  EngineConfig("LIMIT_ONLY",  200.0, 25,  "LONDON", 2.0, 50, 2.0, 50.0, True)),
    ("LIM+LONDON",      EngineConfig("LIMIT_ONLY",  None,  25,  "LONDON", 2.0, 50, 2.0, 50.0, True)),
]

def score(entry):
    r = entry["result"]
    # composite score: return / (max_dd + 1) * profit_factor * sqrt(trades)
    dd_penalty = max(r["max_dd_pct"], 1.0)
    trades_sqrt = math.sqrt(max(r["total_trades"], 1))
    s = (r["total_pnl_pct"] / dd_penalty) * r["profit_factor"] * trades_sqrt
    return s

def main():
    results = []
    print(f"[META] Running {len(REGIMES)} regimes x {len(CONFIGS)} configs")
    for regime in REGIMES:
        for name, cfg in CONFIGS:
            print(f"  -> {regime} | {name} ...", end="", flush=True)
            try:
                out = run_config_on_regime(cfg, regime, start_equity=10000.0, lev=100.0, risk=0.01)
                out["config_name"] = name
                results.append(out)
                r = out["result"]
                print(f"  trades={r['total_trades']}  WR={r['win_rate_pct']}%  PnL={r['total_pnl_pct']}%  DD={r['max_dd_pct']}%")
            except Exception as e:
                print(f"  FAILED: {e}")
                results.append({"regime": regime, "config_name": name, "error": str(e)})

    results.sort(key=score, reverse=True)
    payload = {
        "meta": {"regimes": REGIMES, "configs": [c[0] for c in CONFIGS]},
        "ranked": results,
        "notes": [
            "Balance: $10,000 start | Leverage 1:100 | Risk per trade 1%",
            "Stricter slippage: entry 2-5 pips, exit 3-8 pips (toggle: False = 0.5-2/1-3)",
            "Execution modes: MARKET_ONLY instant fill, LIMIT_ONLY with 25-bar window",
            "SL cap: fixed pips max SL distance; original SL from sweep candle extreme",
            "Kelly fraction is raw full-Kelly; practical use = Kelly/4 or Kelly/6",
        ],
    }
    out_path = "proven_meta_results.json"
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"[SAVED] {out_path}")

    # Print top 10 ranking table
    print("\n=== TOP 10 RANKED ===")
    print(f"{'Rank':<4} {'Regime':<20} {'Config':<18} {'Trades':<7} {'WR%':<6} {'PnL%':<8} {'DD%':<6} {'ProfitF':<8} {'Score':<8}")
    for i, e in enumerate(results[:10]):
        if "error" in e:
            continue
        r = e["result"]
        print(f"{i+1:<4} {e['regime']:<20} {e.get('config_name',''):<18} {r['total_trades']:<7} {r['win_rate_pct']:<6} {r['total_pnl_pct']:<8} {r['max_dd_pct']:<6} {r['profit_factor']:<8} {score(e):<8.1f}")

if __name__ == "__main__":
    main()
