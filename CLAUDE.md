# OMNI-ICT Auto Trading Bot — Claude Code Guide

This is a fully autonomous algorithmic trading bot running ICT/SMC strategy on MetaTrader 5 via a macOS/Wine bridge. Read this before making any changes.

## Architecture Overview

```
MT5 (Wine) ──── OmniExport_v4.mq5 ──── omni_data.json ──── Python stack
                     EA writes every 5s         JSON bridge

Python stack (all supervised by watchdog.py via launchd):
  watchdog.py        → supervisor (launchd agent: com.omni.ict.autonomy)
  orchestrator.py    → signal generator, runs every 60s
  swarm.py           → 8-agent autonomous swarm (execution, risk, ML, journal)
  telegram_bot.py    → Telegram control interface (@OmniAutoTraderICTbot)
  server.py          → FastAPI API at http://localhost:8787
  dashboard.py       → Dash web UI at http://localhost:8050
```

## Live Trading Status (as of 2026-05-07)
- **OMNI_PAPER_MODE = false** (live trading enabled)
- Broker: MidasFX-Live | Account: Christopher Ryan Benavides
- Leverage: 1:1000 | Magic: 20250411
- Telegram chat ID: 5786598754

## Key Files

| File | Purpose |
|------|---------|
| `python/swarm.py` | Agent swarm coordinator — 8 agents handle execution, risk, ML |
| `python/agents/execution_agent.py` | Live trade execution via MT5 command bridge |
| `python/orchestrator.py` | Signal generation pipeline (HTF+LTF ICT analysis) |
| `python/watchdog.py` | Process supervisor. Restarts crashed services. |
| `python/telegram_bot.py` | Telegram control. Send `/start` to `@OmniAutoTraderICTbot`. |
| `python/config.py` | All config. Priority: env var > config.json > auto-detect > default. |
| `python/mt5_connector.py` | Reads `omni_data.json`. Handles Wine 4MB truncation via brace-recovery. |
| `python/smc_engine.py` | Pure ICT/SMC analysis functions (order blocks, FVGs, sweeps). |
| `python/ict_precision.py` | High-confidence entry scanner (4500 lines). |
| `python/scaling_engine.py` | Pyramid scaling decisions. |
| `python/rules.json` | Trade rule config (sessions, symbols, risk, scaling). |
| `python/xauusd_scale_backtest.py` | 2000%+ XAUUSD scale strategy backtester (5% risk compound). |
| `python/backtester.py` | ICT backtest engine using MT5 exported data. |
| `python/backtest.py` | Walk-forward backtest using yfinance (10 symbols, 2yr H1). |
| `python/feature_store.py` | SQLite ML feature store. |
| `python/online_learner.py` | Online ML win-rate optimizer. |
| `python/llm_client.py` | Async LLM (Kimi K2.6 via Ollama → OpenRouter fallback). |
| `OmniExport_v4.mq5` | MT5 EA v4.2 — exports market data (7 TFs). **Compile with F7.** |
| `OmniExecutor.mq5` | MT5 EA — reads `omni_cmd.txt`, places live orders. |

## MT5 / Wine Bridge

The EA writes JSON to:
```
~/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/
AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json
```

**Critical Wine limitation:** FILE_COMMON writes cap at 4MB (4,194,304 bytes). Files truncated silently. Python handles this via brace-recovery in `mt5_connector._recover_truncated_json()`.

**EA symbol list** (7 symbols to stay under 4MB):
- Primary: XAUUSD, XAGUSD (always first — gold/silver priority)
- Forex: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD

**EA exports 7 timeframes per symbol:** D1/H4/H1/M30/M15/M5/M1

## Trading Logic

### Entry Flow
1. `orchestrator.py` → `smc_engine.analyze()` on H1 + M5 bars per symbol
2. Detects: Order Blocks, FVGs, Liquidity Sweeps, BOS/CHoCH
3. `dual_tf_selector.select_trade()` grades the setup (A+/A/B+/B/C)
4. `swarm` execution_agent picks up signals, checks confidence/RR, applies risk filters
5. Live: writes `OPEN|SYMBOL|BUY|0|SL|TP|VOLUME|comment` to `omni_cmd.txt`

### 2000%+ Scale Strategy (xauusd_scale_backtest.py)
- 5% base risk, 8% max on win streak, 1.5% floor after losses
- TP1 at 2R (50%), TP2 at 4R (30%), runner to 6R (20%)
- London open (07-10 UTC) + NY killzone (13-16 UTC) only
- Pyramid: add 50% at +1.5R float, 33% at +2.5R float
- Result: +2,045% in 2yr backtest | 37% WR | 48% max DD

### Risk Modes (auto_trader / rules.json)
| Mode | Base Risk | Max Risk | Daily Limit | Max DD |
|------|-----------|----------|-------------|--------|
| LOW | 0.5% | 1.0% | 2.0% | 5.0% |
| MODERATE | 1.0% | 2.0% | 3.0% | 10.0% |
| HIGH | 2.0% | 4.0% | 5.0% | 15.0% |

## Configuration

### Environment (plist sets these)
```bash
OMNI_PAPER_MODE=false         # Live trading ENABLED
OMNI_RISK_MODE=MODERATE       # LOW / MODERATE / HIGH
OMNI_FREQ_MODE=AGGRESSIVE     # CONSERVATIVE / NORMAL / AGGRESSIVE
OMNI_TELEGRAM_TOKEN=...       # Bot token
```

### LaunchAgent
```bash
# Reload after plist changes:
launchctl unload ~/Library/LaunchAgents/com.omni.ict.autonomy.plist
launchctl load ~/Library/LaunchAgents/com.omni.ict.autonomy.plist
# OR:
launchctl kickstart -k gui/$(id -u)/com.omni.ict.autonomy
```

## Common Tasks

```bash
# Check service health
cat logs/watchdog_state.json | python3 -m json.tool

# Watch live trades
tail -f logs/swarm.log

# Check signals
cat shared/signals.json | python3 -m json.tool

# Run 2000% backtest
cd python && python3 xauusd_scale_backtest.py

# Dashboard
open http://localhost:8050
open http://localhost:8787/docs
```

## Debugging

### "stale HTF bars" in orchestrator.log
EA not running or not recompiled. Open MT5 → MetaEditor → F7 to compile EA.

### Telegram bot crashing with 409 conflict
Orphan telegram_bot process. Kill it: `pkill -f telegram_bot.py` then watchdog restarts.

### JSON parse fails after EA changes
Data exceeded 4MB. Reduce symbols or bar counts in EA and recompile.

### execution_agent errors
Check `logs/swarm.log`. Common: OmniExecutor EA not running on any MT5 chart.

## Code Conventions

- ICT analysis: pure functions in `smc_engine.py` + `ict_precision.py` — no I/O
- I/O: `orchestrator.py`, `swarm` agents, `server.py`
- XAUUSD + XAGUSD always first in symbol lists
- Live trading is now the default (PAPER_MODE=false) — confirm before disabling

## AI / LLM Integration

- Primary model: `kimi-k2.6:cloud` via Ollama (http://localhost:11434)
- Fallback: `anthropic/claude-sonnet-4-6` via OpenRouter
- Local fallback: `qwen3.5:9b` via Ollama
- Config: `python/llm_client.py` (KIMI_VIA_OLLAMA, KIMI_VIA_OR, OLLAMA_LOCAL)
- Hermes agent: `~/.hermes/` — reads this file via MCP filesystem server

## File Locations

```
Omni-full-ALGO-Trading-Bot/
├── CLAUDE.md                     ← this file (also imported to Hermes)
├── python/
│   ├── swarm.py                  ← 8-agent coordinator
│   ├── agents/                   ← individual agents
│   ├── orchestrator.py           ← signal generator
│   ├── watchdog.py               ← supervisor
│   ├── ict_precision.py          ← ICT core (4500 lines)
│   ├── xauusd_scale_backtest.py  ← 2000%+ backtester
│   └── rules.json                ← live-editable rules
├── shared/
│   ├── signals.json              ← live signals
│   └── swarm_state.json          ← agent health
├── logs/
│   ├── swarm.log                 ← trade execution
│   ├── orchestrator.log          ← signal scans
│   ├── telegram_bot.log          ← Telegram
│   └── watchdog_state.json       ← service health
└── Desktop EAs:
    ├── OmniExport_v4.mq5         ← compile in MetaEditor (F7)
    ├── OmniExecutor.mq5          ← executes trade commands
    └── OmniSignalOverlay.mq5     ← chart indicator
```
