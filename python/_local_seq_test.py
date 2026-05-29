"""Real backtest (fills+costs+cooldown) of the calibrated sequential engine on
local real XAUUSD (MidasFX) data. Short window — validation, not edge proof."""
import json
from datetime import datetime, timezone
from smc_engine import Bar
from ict_sequential import SequentialConfig
from risk_sizing import RiskConfig
from ict_sequential_backtest import run

def load(tf):
    d = json.load(open(f"../shared/xauusd_{tf}.json"))
    out = [Bar(time=datetime.strptime(r["t"], "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp(),
               open=r["o"], high=r["h"], low=r["l"], close=r["c"]) for r in d]
    out.sort(key=lambda b: b.time)
    return out

htf = load("h1"); ltf = load("m15")
print(f"HTF(h1)={len(htf)} LTF(m15)={len(ltf)} | "
      f"{datetime.fromtimestamp(ltf[0].time,timezone.utc):%Y-%m-%d} -> "
      f"{datetime.fromtimestamp(ltf[-1].time,timezone.utc):%Y-%m-%d}")

cfg = SequentialConfig(symbol="XAUUSD")          # calibrated defaults (pd_block=0.25)
rep = run(htf, ltf, "XAUUSD", cfg, RiskConfig(), start_equity=133.42,
          spread=0.30, slippage=0.10, commission_per_lot=7.0,
          fill_window=6, cooldown_bars=4)

print("\n=== CALIBRATED SEQUENTIAL — local XAUUSD backtest ===")
for k in ("trades","wins","losses","win_rate","total_R","avg_R","profit_factor",
          "max_consec_losses","start_equity","end_equity","return_pct"):
    print(f"  {k:<18} {rep[k]}")
print("\nTrades:")
for t in rep["_trades"]:
    dt = datetime.fromtimestamp(t.fill_time, timezone.utc)
    print(f"  {dt:%m-%d %H:%M} {t.direction:4} e={t.entry} sl={t.sl} tp={t.tp} "
          f"-> {t.outcome:9} {t.r_realized:+}R  ${t.pnl_usd:+} (lot {t.lots})")
