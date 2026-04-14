# OMNI-ICT Autonomous Trading System

A fully autonomous ICT (Inner Circle Trader) methodology trading bot with a real-time analysis dashboard. Supports multiple MT5 accounts simultaneously (e.g. Midas live + Demo).

---

## What it does

| Feature | Detail |
|---|---|
| **ICT Analysis Engine** | BOS/CHoCH, Order Blocks, FVGs, liquidity sweeps, AMD cycle |
| **Quarter Theory** | Q1–Q4 discount/premium zone scoring on every setup |
| **OB Precision Entry** | Enters at OTE 50%/62%/79% Fibonacci — never at OB extremes |
| **Pattern Detection** | Double Top/Bottom, Head & Shoulders, Inverse H&S, Rising/Falling Wedges |
| **Push/Exhaustion** | Body+wick slope regression to confirm or fade momentum |
| **All-Session Trading** | Asia (min confidence 65), London/NY (min confidence 55), NY Close |
| **AI Trade Memory** | Tracks every trade outcome, adapts confidence ±30 pts by setup/session/pattern/quarter |
| **Scale-In Logic** | Adds 50% position at 1R profit when push momentum confirmed |
| **Detailed Trade Logs** | Full numbered reasoning log for every trade entry and exit |
| **Real-Time Dashboard** | React WebSocket dashboard — market scanner, bot feed, memory panel |
| **Multi-Account** | Separate Midas live + Demo feeds, each with independent memory |

---

## Prerequisites

- **Python 3.10+**
- **MetaTrader 5** (Windows) with the `OmniExport.mq5` EA running on each account
- macOS/Linux: can run the dashboard server without MT5 (reads JSON data files)

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/omni-ict.git
cd omni-ict

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate          # macOS/Linux
# venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Configuration

```bash
# Copy the example config and edit it with your real paths
cp config.example.json config.json
```

Open `config.json` and update:

- `data_path` → path to the MT5 `omni_data.json` file written by `OmniExport.mq5`
- `cmd_path`  → path to `omni_cmd.txt` (the bot writes orders here; EA reads them)
- `state_path`, `log_path`, `memory_path`, `journal_path` → where runtime files live

**Find your MT5 terminal ID:**  
In MT5 → File → Open Data Folder. The folder name in `MetaQuotes/Terminal/` is your terminal ID.

---

## Running the system

### 1. Start the MT5 EA (Windows, in MetaTrader 5)

Attach `OmniExport.mq5` to any chart on **each** account. It will continuously write market data to `omni_data.json`.

### 2. Start the autonomous bot

```bash
cd python
python auto_trader.py
```

The bot reads `tv_data.json` (written by the EA via `omni_data.json`), runs ICT analysis, and writes orders to `omni_cmd.txt` which the EA executes.

### 3. Start the dashboard server

```bash
# From the repo root
python server.py
```

Open your browser at **http://localhost:8000**

---

## Dashboard overview

```
┌─────────────────────────────────────────────────────────────┐
│  OMNI-ICT  [Midas Live] [Demo]   SESSION: LONDON  AMD: DIST │
├──────────────┬───────────────────────────┬───────────────────┤
│  Account     │  Market Scanner           │  Bot Feed         │
│  Summary     │  (all symbols, live conf) │  (live log lines) │
│              ├───────────────────────────┤                   │
│  Open        │  Analysis Detail          │  Trade Memory     │
│  Positions   │  (selected symbol)        │  (perf / rules)   │
└──────────────┴───────────────────────────┴───────────────────┘
```

- **Market Scanner** — scans the full watchlist every refresh, shows confidence bars and setup type
- **Analysis Detail** — click any symbol to see: quarter bar, structure, OB/FVG levels, pattern cards, push/exhaustion alert, trade levels, full numbered reasons
- **Bot Feed** — colour-coded live log (entries green, SL red, scale-in yellow, system grey)
- **Trade Memory** — tabbed panel: overall performance / by setup type / by session / AI-learned rules

---

## Project structure

```
omni-ict/
├── config.example.json      ← copy to config.json and edit
├── requirements.txt
├── server.py                ← FastAPI + WebSocket dashboard server
├── webapp/
│   └── index.html           ← React single-file dashboard (no build needed)
├── python/
│   ├── auto_trader.py       ← Autonomous bot main loop
│   ├── ict_precision.py     ← ICT analysis engine (scanner, patterns, scoring)
│   ├── trade_memory.py      ← AI learning + trade journal
│   └── rules.json           ← ICT rule weights and thresholds
└── mql5/
    ├── OmniExport.mq5       ← MT5 EA: exports market data, executes commands
    └── OmniExecutor.mq5     ← MT5 EA: order execution helper
```

---

## Environment variables (optional)

Create a `.env` file in the repo root:

```env
OMNI_CONFIG_PATH=/path/to/config.json   # override default config location
OMNI_HOST=0.0.0.0
OMNI_PORT=8000
```

---

## Adding a new symbol to the watchlist

Edit the `"watchlist"` array in `config.json`. The scanner picks it up on next restart.

---

## License

Private / proprietary. Do not redistribute.
