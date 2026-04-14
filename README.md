# OMNI-ICT Autonomous Trading System

A fully autonomous ICT (Inner Circle Trader) methodology algo-bot with a real-time React dashboard, AI trade memory, advanced risk management, and built-in backtester. Connects to MetaTrader 5 via a custom MQL5 EA on macOS or Windows.

---

## Architecture

```
MetaTrader 5
├── OmniExport.mq5 (EA)          writes omni_data.json every 3 seconds
└── OmniExecutor.mq5 (EA)        reads omni_cmd.txt, executes orders

Python
├── auto_trader.py               main bot loop — scans, scores, places orders
├── ict_precision.py             ICT analysis engine (multi-TF scanner + scoring)
├── ict_engine.py                SMT divergence, kill zones, pandas signal layer
├── trade_memory.py              AI journal — logs every trade, adapts confidence
├── advanced_risk_manager.py     position sizing with fees, swap, slippage, asset class
├── backtester.py                replays historical MT5 bars, simulates trades
├── mt5_connector.py             lightweight MT5 data reader (pandas wrappers)
├── data_router.py               routes data between connectors
├── tradingview_connector.py     TradingView data integration
└── rules.json                   all ICT rule weights, thresholds, symbol overrides

server.py                        FastAPI + WebSocket dashboard backend
webapp/index.html                React real-time dashboard (no build step)
```

---

## What the bot does

### ICT Analysis Engine (`ict_precision.py`)

The scanner runs a full multi-timeframe analysis on every symbol in the watchlist.

**Timeframe cascade:** D1 bias → H4 structure → H1/M15 entry trigger → M5 confirmation

| Concept | Implementation |
|---|---|
| BOS / CHoCH | Break of Structure and Change of Character on H1 |
| Order Blocks | Last bearish candle before bullish impulse (vice versa for shorts); invalidated on full close through |
| Fair Value Gaps | 3-candle imbalance detection, entry at FVG midpoint |
| Liquidity Sweeps | Sweep of equal highs/lows then close back through — primary entry trigger |
| Equal H/L | 0.2% tolerance multi-touch detection — marks liquidity pools |
| AMD Cycle | Accumulation (Asia) → Manipulation (London sweep) → Distribution (NY confirmation) |
| Kill Zones | London Open 07–09 UTC (+10 confidence), NY Open 12–14 UTC (+10 confidence) |
| SMT Divergence | Correlated pairs (EURUSD/GBPUSD, XAUUSD/XAGUSD, etc.) diverging at key levels |

**Quarter Theory:** Daily range split into 4 equal zones. Q1 (deep discount) +20 buy confidence, Q4 (deep premium) +20 sell confidence. OTE Fibonacci entry zone at 62–79% retracement.

**OB Precision Entry:** Never enters at OB extremes. Uses OTE 50% (standard), 62% (deep), or 79% (tightest SL). Entry only valid with displacement candle confirmation.

**Pattern Detection** (linear regression on bar data):
- Double Top / Double Bottom — 0.3% tolerance, min 5-bar separation, +15 confidence
- Head & Shoulders / Inverse H&S — min 20-bar pattern, +18 confidence
- Rising Wedge (distribution) / Falling Wedge (accumulation) — +12 confidence

**Push / Exhaustion:** Body slope and wick slope linear regression over last 6 bars.
- PUSH = continuation signal (+8 confidence with trend)
- EXHAUSTION at OB/FVG = high-probability reversal entry (+12 confidence)

**Support / Resistance:** Classifies price phase as `PUSHING_INTO`, `EXHAUSTING_AT`, or `TESTING` against swing H/L, equal H/L, session H/L, PDH/PDL, PWH/PWL, round numbers, and active OB zones.

---

### Session Rules

All four sessions are tradeable. Session-specific confidence thresholds apply:

| Session | UTC Hours | Min Confidence | Size | Priority |
|---|---|---|---|---|
| Asia | 22:00–07:00 | **65** | 75% | Medium |
| London | 07:00–12:00 | 55 | 100% | **Highest** |
| New York | 12:00–17:00 | 55 | 100% | **Highest** |
| NY Close | 17:00–22:00 | **65** | 50% | Low |

Kill zones (London Open 07–09, NY Open 12–14) add +10 confidence.

---

### Risk Management (`auto_trader.py` + `advanced_risk_manager.py`)

**Compounding position sizing:**
```
lot_size = (equity × risk%) / (sl_distance / tick_size × tick_value)
```

**Adaptive risk scaling:**

| Condition | Action |
|---|---|
| Win streak ≥ 5 | Risk = BASE + 0.50% (max 2.0%) |
| Win streak ≥ 3 | Risk = BASE + 0.25% |
| Normal | Risk = 1.0% (base) |
| Loss streak ≥ 2 | Risk − 0.25% |
| Loss streak ≥ 3 | Risk − 0.25% more (floor 0.5%) |

**Hard limits:**
- Daily loss limit: **3%** → trading halted until next UTC day
- Max drawdown from equity peak: **10%** → trading halted
- Max concurrent open positions: **3**
- Minimum R:R ratio: **2:1** — setups below this are discarded
- Kill switch: create a file at `python/HALT` to stop the bot within one scan cycle

**Advanced risk manager** (`advanced_risk_manager.py`) accounts for asset class (FOREX, METAL, CRYPTO, INDEX, COMMODITY), trade duration (SCALP / INTRADAY / SWING / POSITION), broker spread, commission per lot, and daily swap costs before calculating final lot size.

**Trade management:**
- TP1 — 50% of position closed at 1.5R (nearest liquidity)
- TP2 — 30% closed at 2.5R (session H/L)
- TP3 — 20% runner at 4R (daily H/L or PWH/PWL)
- SL moves to break-even after TP1 fills
- Trailing stop tightens at 2R (trails to 1R), tightens further at 3R
- Runner closed immediately on opposing BOS on H1

**Scale-in logic:** At 1R profit, if push momentum is confirmed on M15 and no opposing BOS exists on H1, the bot adds 50% of the original position size at market. Max one scale-in per trade.

---

### AI Trade Memory (`trade_memory.py`)

Every trade is logged with full context — entry type, session, AMD phase, D1 bias, H4 structure, quarter position, detected patterns, push/exhaustion state, all numbered confluence reasons, raw and adjusted confidence, R:R ratio, and outcome (R multiple, close reason, TP level hit).

The engine tracks win rates across 5 independent dimensions:

| Bucket | Examples |
|---|---|
| Setup type | OB_RETEST, FVG_FILL, SWEEP_REVERSAL, PDH_SWEEP |
| Session | ASIA, LONDON, NEW_YORK, NY_CLOSE |
| AMD phase | ACCUMULATION, MANIPULATION, DISTRIBUTION |
| Pattern | DOUBLE_BOTTOM, HEAD_SHOULDERS, FALLING_WEDGE |
| Quarter | Q1, Q2, Q3, Q4 |

After 8+ samples in any bucket, confidence adjustments apply automatically:

| Win Rate | Adjustment |
|---|---|
| > 70% | +10 confidence for this setup type |
| > 60% | +5 |
| < 45% | −8 |
| < 35% | −15 (disabled below 30%) |
| Avg R > 2.0 | additional +5 |
| Avg R < 0.8 | additional −5 |

Total adjustment is capped at ±30 points. The engine generates plain-English learned rules at startup and logs a full performance report every 30 minutes.

---

### Backtester (`backtester.py`)

Replays actual MT5 historical bar data exported by `OmniExport.mq5`. Simulates the full ICT strategy including entry scoring, compounding position sizing, TP1/2/3 management, and trailing SL. Results saved to `python/backtest_results.json`.

```bash
cd python && python backtester.py
```

---

### Dashboard (`server.py` + `webapp/index.html`)

FastAPI backend + single-file React dashboard. No npm, no build step required — React 18 and Babel load from CDN.

**REST endpoints:**
- `GET /api/accounts` — list configured accounts
- `GET /api/data/{account_id}` — full live account snapshot
- `GET /api/rules` — current `rules.json`
- `GET /api/status` — server health

**WebSocket** `/ws/{account_id}` — pushes live snapshots every 2 seconds. Auto-reconnects on disconnect. Each account has its own independent stream.

**Dashboard layout:**

| Panel | Contents |
|---|---|
| Header | Account tabs, live session badge, AMD phase, bot status |
| Account Summary | Balance, equity, margin, open P&L, daily P&L, overall win rate |
| Open Positions | Each trade with animated SL → Entry → TP progress bar |
| Market Scanner | Full watchlist with confidence bars, setup type, entry/SL/TP levels |
| Analysis Detail | Quarter bar (Q1–Q4), structure info, OB/FVG zones, pattern cards, push/exhaustion alert, numbered entry reasons |
| Bot Feed | Colour-coded live log — entries (green), SL hits (red), scale-ins (yellow), system (grey) |
| Trade History | Closed trades from memory with R multiples |
| Memory Panel | Tabbed view: overall performance / by setup type / by session / AI-learned rules |

---

## Watchlist

Default: `XAUUSD, XAGUSD, EURUSD, GBPUSD, USDJPY, GBPJPY, AUDUSD, NAS100, US30`

Symbol-specific overrides in `rules.json`:
- **XAUUSD** — max spread 30 pips, min ATR 8 pips, tradeable all sessions, Asia min confidence 60
- **NAS100** — NY kill zone only, min confidence 70, kill zone required
- **GBPJPY** — min confidence 70 due to high volatility and wide spreads

---

## Prerequisites

- Python 3.10+
- MetaTrader 5 (Windows, or macOS via Wine / CrossOver)
- `OmniExport.mq5` EA running and attached on each MT5 account

---

## Installation

```bash
# 1. Clone
git clone https://github.com/fitfriendchris/Omni-full-ALGO-Trading-Bot.git
cd Omni-full-ALGO-Trading-Bot

# 2. Virtual environment
python3 -m venv venv
source venv/bin/activate        # macOS / Linux
# venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Configuration

```bash
cp config.example.json config.json
```

Edit `config.json` and set the `data_path` for each account to the full path of the `omni_data.json` file written by `OmniExport.mq5`.

**Finding your MT5 data folder:**  
In MetaTrader 5 → File → Open Data Folder → `MQL5/Files/`

---

## Running

### 1. Attach the MT5 EA

In MetaTrader 5, open the Navigator panel, find `OmniExport` under Expert Advisors, and drag it onto any chart. It starts writing `omni_data.json` every 3 seconds. Repeat for each account (Midas live + Demo).

### 2. Start the bot

```bash
cd python
python auto_trader.py
```

> **Paper mode is ON by default** (`PAPER_MODE = True` at line 26 of `auto_trader.py`).  
> The bot logs every order it *would* place without touching the account.  
> Only set `PAPER_MODE = False` after verifying behaviour on a demo account.

### 3. Start the dashboard

```bash
# From the repo root
python server.py
# Open http://localhost:8000
```

### Emergency stop

```bash
touch python/HALT   # bot halts within one scan cycle (≤10 seconds)
rm python/HALT      # resume trading
```

---

## Project structure

```
Omni-full-ALGO-Trading-Bot/
├── config.example.json
├── requirements.txt
├── server.py
├── webapp/
│   └── index.html
├── OmniExport.mq5
├── OmniExecutor.mq5
└── python/
    ├── auto_trader.py
    ├── ict_precision.py
    ├── ict_engine.py
    ├── trade_memory.py
    ├── advanced_risk_manager.py
    ├── backtester.py
    ├── mt5_connector.py
    ├── data_router.py
    ├── tradingview_connector.py
    ├── dashboard.py               (legacy Dash/Plotly dashboard)
    └── rules.json
```

---

## Disclaimer

This software is for educational and research purposes. Algorithmic trading carries significant financial risk. Past performance — whether backtested or live — does not guarantee future results. Always test thoroughly on a demo account before using real capital.
