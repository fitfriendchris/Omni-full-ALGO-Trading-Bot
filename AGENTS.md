# Omni-ICT Algo Trading Bot — Agent Context

## Project Overview
Autonomous ICT/SMC algo trading bot for XAUUSD and XAGUSD (paper mode by default).
Runs on macOS via launchd (`com.omni.ict.autonomy`).

## Key Paths
- Python source: `python/`
- Live signals: `shared/signals.json`
- Logs: `logs/` (orchestrator.log, auto_trader.log, launchagent.err.log)
- MT5 data bridge: `~/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json`

## Architecture
```
watchdog.py → orchestrator.py → dual_tf_selector.py → signals.json
                              ↗ smc_engine.py (OBs, FVGs, sweeps, structure)
auto_trader.py reads signals.json → executes trades via MT5
```

## ICT Strategy
- **Timeframes**: H4 macro bias → H1 HTF structure → M5 LTF trigger
- **Entry types**: OB mitigation (body close-through only), FVG fill (close-through), CHoCH, BOS
- **Entry precision**: OTE = midpoint of OB body
- **Filters**: Kill zones (London 07-09, NY 12-14 UTC), liquidity sweep gate, CISD bonus
- **R:R target**: 2.5, SL at OB extreme
- **Symbols**: XAUUSD (gold), XAGUSD (silver) — broker-offered only

## Service Management
```bash
# Status
launchctl list | grep omni
# Restart
launchctl kickstart -k gui/$(id -u)/com.omni.ict.autonomy
# Logs
tail -f logs/orchestrator.log
tail -f logs/auto_trader.log
```

## Current Mode
`OMNI_PAPER_MODE=true` — paper trading only. Set to `false` in plist for live.

## Rules Config
`python/rules.json` — controls dual_tf thresholds, tp_rr, macro_timeframe, etc.
