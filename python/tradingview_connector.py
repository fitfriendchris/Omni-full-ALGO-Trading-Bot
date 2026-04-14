"""
tradingview_connector.py — TradingView → OMNI ICT Bridge
=========================================================
Two modes of operation:

  MODE 1 · Webhook Server (recommended)
    - Run this file directly: python tradingview_connector.py
    - A Flask server listens on port 5555 for Pine Script webhook alerts
    - Normalises incoming JSON into the same schema as omni_data.json
    - Writes tv_data.json every time an alert fires
    - data_router.py picks it up automatically

  MODE 2 · TradingView MCP (Claude Code only)
    - Requires Claude Code CLI + the tradingview-mcp package
    - Allows Claude to read live TradingView DOM data directly
    - See setup instructions at bottom of this file (TRADINGVIEW_MCP_SETUP)

Usage (webhook mode):
    python tradingview_connector.py          # starts webhook server
    python tradingview_connector.py --test   # fires a mock alert for testing
"""

import json
import os
import re
import threading
import time
import argparse
from datetime import datetime, timezone
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
TV_DATA_PATH  = os.path.join(_SCRIPT_DIR, "tv_data.json")
RULES_PATH    = os.path.join(_SCRIPT_DIR, "rules.json")
WEBHOOK_PORT  = 5555
STALE_SECONDS = 60   # TV data older than this = not connected

# ── ICT Symbols we track ──────────────────────────────────────────────────────
PRIMARY_SYMBOLS = [
    "XAUUSD", "XAGUSD",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD",
    "NAS100", "US30", "SPX500",
    "BTCUSD", "ETHUSD",
]

# In-memory store for latest prices received via webhook
_latest: dict = {}
_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# DATA NORMALISER
# Maps TradingView alert payload → omni_data.json schema
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_alert(payload: dict) -> Optional[dict]:
    """
    Accepts a TradingView webhook payload and returns a price record
    matching the omni_data.json 'prices' array entry format.

    Expected payload keys (from Pine Script alert):
      symbol     : string   e.g. "XAUUSD"
      bid        : float    current bid
      ask        : float    current ask
      close      : float    bar close
      open       : float    bar open
      high       : float    bar high
      low        : float    bar low
      volume     : int      bar volume
      timeframe  : string   e.g. "H1"
      structure  : string   BOS_BULLISH | BOS_BEARISH | CHOCH_BULL | CHOCH_BEAR | RANGING
      fvg_type   : string   BULLISH | BEARISH | NONE
      fvg_high   : float    FVG upper bound
      fvg_low    : float    FVG lower bound
      ob_type    : string   BULLISH_OB | BEARISH_OB | NONE
      ob_high    : float
      ob_low     : float
      atr        : float    ATR value
      rsi        : float    RSI value
      bias       : string   BULLISH | BEARISH | NEUTRAL
      source     : string   "TRADINGVIEW" (auto-added)
    """
    sym = payload.get("symbol", "").upper().replace("/", "")
    if not sym:
        return None

    bid   = float(payload.get("bid",   payload.get("close", 0)))
    ask   = float(payload.get("ask",   bid))
    close = float(payload.get("close", bid))

    spread = round((ask - bid) * (10000 if "JPY" not in sym else 100), 1)

    return {
        "symbol":    sym,
        "bid":       bid,
        "ask":       ask,
        "close":     close,
        "open":      float(payload.get("open",  close)),
        "high":      float(payload.get("high",  close)),
        "low":       float(payload.get("low",   close)),
        "spread":    spread,
        "volume":    int(payload.get("volume",  0)),
        "timeframe": payload.get("timeframe", "H1"),
        # ICT structure fields
        "structure": payload.get("structure", "RANGING"),
        "fvg_type":  payload.get("fvg_type",  "NONE"),
        "fvg_high":  float(payload.get("fvg_high", 0)),
        "fvg_low":   float(payload.get("fvg_low",  0)),
        "ob_type":   payload.get("ob_type",  "NONE"),
        "ob_high":   float(payload.get("ob_high", 0)),
        "ob_low":    float(payload.get("ob_low",  0)),
        "atr":       float(payload.get("atr", 0)),
        "rsi":       float(payload.get("rsi", 50)),
        "bias":      payload.get("bias", "NEUTRAL"),
        "source":    "TRADINGVIEW",
        "ts":        datetime.now(timezone.utc).isoformat(),
    }


def _build_tv_data() -> dict:
    """
    Constructs a full omni_data.json-compatible dict from the latest
    TradingView price records stored in _latest.
    """
    now  = datetime.now(timezone.utc)
    hour = now.hour

    # Session / AMD phase (mirrors OmniExport.mq5 logic)
    if hour >= 22 or hour < 7:
        session, amd = "ASIA",     "ACCUMULATION"
    elif 7 <= hour < 12:
        session, amd = "LONDON",   "MANIPULATION"
    elif 12 <= hour < 17:
        session, amd = "NEW_YORK", "DISTRIBUTION"
    else:
        session, amd = "NY_CLOSE", "DISTRIBUTION"

    in_kz = (
        (hour >= 22 or hour < 1) or
        (7 <= hour < 9)          or
        (11 <= hour < 13)        or
        (12 <= hour < 14)        or
        (19 <= hour < 21)
    )

    prices = list(_latest.values())

    # Derive bias counts from incoming bias fields
    bull = sum(1 for p in prices if p.get("bias") == "BULLISH")
    bear = sum(1 for p in prices if p.get("bias") == "BEARISH")
    if bull > bear:
        overall_bias = "BULLISH"
    elif bear > bull:
        overall_bias = "BEARISH"
    else:
        overall_bias = "NEUTRAL"

    return {
        "timestamp":   now.strftime("%Y.%m.%d %H:%M:%S"),
        "source":      "TRADINGVIEW",
        "session":     session,
        "amd_phase":   amd,
        "killzone":    in_kz,
        "gmt_hour":    hour,
        "gmt_time":    now.strftime("%Y.%m.%d %H:%M:%S"),
        "overall_bias": overall_bias,
        "account":     {},           # TV has no account data; MT5 fills this
        "prices":      prices,
        "positions":   [],
        "history":     [],
        "charts":      {p["symbol"]: _price_to_chart(p) for p in prices},
    }


def _price_to_chart(p: dict) -> dict:
    """Convert a price record to a minimal chart entry for ai_engine compatibility."""
    return {
        "symbol":   p["symbol"],
        "close":    p["close"],
        "open":     p["open"],
        "high":     p["high"],
        "low":      p["low"],
        "atr":      p.get("atr", 0),
        "rsi":      p.get("rsi", 50),
        "structure": p.get("structure", "RANGING"),
        "fvg_type": p.get("fvg_type", "NONE"),
        "fvg_high": p.get("fvg_high", 0),
        "fvg_low":  p.get("fvg_low",  0),
        "ob_type":  p.get("ob_type",  "NONE"),
        "ob_high":  p.get("ob_high",  0),
        "ob_low":   p.get("ob_low",   0),
        "bias":     p.get("bias", "NEUTRAL"),
    }


def _save_tv_data(data: dict) -> None:
    try:
        with open(TV_DATA_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[TV] Save error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API  (mirrors mt5_connector.py interface)
# ─────────────────────────────────────────────────────────────────────────────

def get_tv_data() -> dict:
    """Read the latest tv_data.json file."""
    try:
        with open(TV_DATA_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def is_connected() -> bool:
    """True if tv_data.json exists and was written within STALE_SECONDS."""
    try:
        age = datetime.now().timestamp() - os.path.getmtime(TV_DATA_PATH)
        return age < STALE_SECONDS
    except Exception:
        return False


def get_symbol_prices(symbols: list) -> list:
    data = get_tv_data()
    sym_set = set(s.upper() for s in symbols)
    return [p for p in data.get("prices", []) if p.get("symbol") in sym_set]


def get_account_info() -> dict:
    """TV has no account data — returns empty dict (MT5 fills this)."""
    return {}


def get_last_update() -> str:
    return get_tv_data().get("timestamp", "—")


def get_source() -> str:
    return "TRADINGVIEW"


# ─────────────────────────────────────────────────────────────────────────────
# MORNING BRIEF  (equivalent to the video's morning_brief command)
# ─────────────────────────────────────────────────────────────────────────────

def morning_brief(symbols: list = None) -> str:
    """
    Generate a formatted morning brief for all tracked symbols.
    Called by data_router.py or directly from ai_engine.py.

    Usage in terminal:
        from tradingview_connector import morning_brief
        print(morning_brief())
    """
    data    = get_tv_data()
    prices  = data.get("prices", [])
    session = data.get("session", "—")
    amd     = data.get("amd_phase", "—")
    kz      = data.get("killzone", False)
    ts      = data.get("timestamp", "—")

    if not prices:
        return "[OMNI Morning Brief] No TradingView data available. Is the webhook server running?"

    target = set(s.upper() for s in symbols) if symbols else None
    lines  = [
        f"\n{'─'*60}",
        f"  OMNI ICT MORNING BRIEF  |  {ts}",
        f"  Session: {session}  |  AMD: {amd}  |  Kill Zone: {'✓' if kz else '✗'}",
        f"{'─'*60}",
        f"  {'SYMBOL':<10} {'PRICE':>10} {'BIAS':>10} {'STRUCTURE':>14} {'RSI':>6} {'ATR':>8}",
        f"{'─'*60}",
    ]

    for p in prices:
        sym = p.get("symbol", "")
        if target and sym not in target:
            continue
        bias      = p.get("bias", "NEUTRAL")
        structure = p.get("structure", "RANGING")
        price     = p.get("close", p.get("bid", 0))
        rsi       = p.get("rsi", 0)
        atr       = p.get("atr", 0)

        bias_icon = "▲" if bias == "BULLISH" else "▼" if bias == "BEARISH" else "─"
        lines.append(
            f"  {sym:<10} {price:>10.4f} {bias_icon + ' ' + bias:>10} {structure:>14} {rsi:>6.1f} {atr:>8.4f}"
        )

    lines.append(f"{'─'*60}\n")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# FLASK WEBHOOK SERVER
# ─────────────────────────────────────────────────────────────────────────────

def run_webhook_server(port: int = WEBHOOK_PORT) -> None:
    """
    Start the Flask webhook server.
    TradingView Pine Script sends POST requests here when alerts fire.

    Endpoint: POST http://YOUR_IP:5555/alert
    Body: JSON (see Pine Script template in tv_pine_alert.pine)
    """
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        print("[TV] Flask not installed. Run: pip install flask")
        return

    app = Flask("OMNI_TV_Webhook")

    @app.route("/alert", methods=["POST"])
    def receive_alert():
        try:
            payload = request.get_json(force=True)
            if not payload:
                return jsonify({"status": "error", "msg": "empty payload"}), 400

            record = _normalise_alert(payload)
            if not record:
                return jsonify({"status": "error", "msg": "missing symbol"}), 400

            with _lock:
                _latest[record["symbol"]] = record
                tv_data = _build_tv_data()

            _save_tv_data(tv_data)
            print(f"[TV] ✓ Alert received: {record['symbol']} @ {record['close']} | {record.get('bias')} | {record.get('structure')}")
            return jsonify({"status": "ok", "symbol": record["symbol"]}), 200

        except Exception as e:
            print(f"[TV] Alert error: {e}")
            return jsonify({"status": "error", "msg": str(e)}), 500

    @app.route("/status", methods=["GET"])
    def status():
        return jsonify({
            "status":    "running",
            "symbols":   list(_latest.keys()),
            "count":     len(_latest),
            "timestamp": datetime.now().isoformat(),
        })

    @app.route("/brief", methods=["GET"])
    def brief():
        return morning_brief(), 200, {"Content-Type": "text/plain"}

    print(f"\n[OMNI TradingView Connector]")
    print(f"  Webhook server starting on port {port}")
    print(f"  POST alerts to: http://0.0.0.0:{port}/alert")
    print(f"  Status page:    http://localhost:{port}/status")
    print(f"  Morning brief:  http://localhost:{port}/brief\n")
    app.run(host="0.0.0.0", port=port, debug=False)


# ─────────────────────────────────────────────────────────────────────────────
# TEST MODE  (python tradingview_connector.py --test)
# ─────────────────────────────────────────────────────────────────────────────

def _fire_mock_alerts() -> None:
    """Inject mock TradingView alerts to test the pipeline without a live TV connection."""
    import random

    mock_data = [
        {"symbol": "XAUUSD", "close": 2345.50, "bid": 2345.20, "ask": 2345.80,
         "open": 2340.00, "high": 2350.00, "low": 2338.00, "volume": 1200,
         "timeframe": "H1", "structure": "BOS_BULLISH", "bias": "BULLISH",
         "fvg_type": "BULLISH", "fvg_high": 2342.00, "fvg_low": 2340.50,
         "ob_type": "BULLISH_OB", "ob_high": 2339.00, "ob_low": 2337.50,
         "atr": 8.50, "rsi": 58.3},
        {"symbol": "EURUSD", "close": 1.08450, "bid": 1.08448, "ask": 1.08452,
         "open": 1.08200, "high": 1.08600, "low": 1.08100, "volume": 4500,
         "timeframe": "H1", "structure": "RANGING", "bias": "NEUTRAL",
         "fvg_type": "NONE", "fvg_high": 0, "fvg_low": 0,
         "ob_type": "BEARISH_OB", "ob_high": 1.08600, "ob_low": 1.08550,
         "atr": 0.00350, "rsi": 48.7},
        {"symbol": "GBPUSD", "close": 1.27320, "bid": 1.27318, "ask": 1.27322,
         "open": 1.27100, "high": 1.27500, "low": 1.27050, "volume": 3200,
         "timeframe": "H1", "structure": "BOS_BEARISH", "bias": "BEARISH",
         "fvg_type": "BEARISH", "fvg_high": 1.27450, "fvg_low": 1.27380,
         "ob_type": "BEARISH_OB", "ob_high": 1.27500, "ob_low": 1.27420,
         "atr": 0.00420, "rsi": 38.2},
        {"symbol": "USDJPY", "close": 153.45, "bid": 153.44, "ask": 153.46,
         "open": 152.80, "high": 153.60, "low": 152.70, "volume": 2800,
         "timeframe": "H1", "structure": "CHOCH_BULL", "bias": "BULLISH",
         "fvg_type": "BULLISH", "fvg_high": 153.10, "fvg_low": 152.90,
         "ob_type": "BULLISH_OB", "ob_high": 153.00, "ob_low": 152.80,
         "atr": 0.85, "rsi": 62.1},
    ]

    print("\n[OMNI TV Connector] Firing mock alerts...\n")
    for p in mock_data:
        record = _normalise_alert(p)
        if record:
            with _lock:
                _latest[record["symbol"]] = record
            print(f"  ✓ {record['symbol']:>8}  {record['close']:>10.4f}  {record['bias']:>8}  {record['structure']}")

    tv_data = _build_tv_data()
    _save_tv_data(tv_data)
    print(f"\n[TV] tv_data.json written → {TV_DATA_PATH}")
    print(morning_brief())


# ─────────────────────────────────────────────────────────────────────────────
# TRADINGVIEW MCP SETUP INSTRUCTIONS
# ─────────────────────────────────────────────────────────────────────────────
TRADINGVIEW_MCP_SETUP = """
TRADINGVIEW MCP SETUP (Claude Code CLI)
========================================
This gives Claude Code live access to TradingView candle data via CDP.
Based on the tradingview-mcp project (as covered in the YouTube video).

STEP 1: Install Claude Code CLI
  npm install -g @anthropic-ai/claude-code

STEP 2: Install the TradingView MCP package
  npx tradingview-mcp install
  — OR use the one-shot setup prompt from the GitHub repo —
  Paste it directly into your Claude Code terminal.

STEP 3: Create mcp.json in your project root:
  {
    "mcpServers": {
      "tradingview": {
        "command": "npx",
        "args": ["tradingview-mcp"]
      }
    }
  }

STEP 4: Enable CDP in TradingView Desktop
  Launch TradingView Desktop, then run:
  open -a "TradingView" --args --remote-debugging-port=9222

STEP 5: In Claude Code terminal, type:
  morning_brief
  — Claude will scan your full watchlist and return an ICT analysis brief.

STEP 6: Add your watchlist:
  "My watchlist: XAUUSD, EURUSD, GBPUSD, USDJPY, GBPJPY, NAS100"
  Claude will apply your rules.json strategy to each symbol.

STEP 7 (optional): Auto-brief every N minutes
  Add to your OMNI system:
  import schedule
  schedule.every(10).minutes.do(lambda: print(morning_brief()))

NOTES:
  - Claude Code reads LIVE DOM data from TradingView — not screenshots
  - It knows exact OHLCV values, indicator outputs, candle positions
  - You can say "apply my ICT rules to XAUUSD H1" and it builds Pine Script
  - rules.json controls the strategy — edit it by talking to Claude Code
"""


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OMNI TradingView Connector")
    parser.add_argument("--test", action="store_true", help="Fire mock alerts and exit")
    parser.add_argument("--port", type=int, default=WEBHOOK_PORT, help="Webhook port")
    parser.add_argument("--brief", action="store_true", help="Print morning brief and exit")
    args = parser.parse_args()

    if args.test:
        _fire_mock_alerts()
    elif args.brief:
        print(morning_brief())
    else:
        run_webhook_server(port=args.port)
