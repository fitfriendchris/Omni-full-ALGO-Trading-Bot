"""
server.py — OMNI ICT Dashboard Server
======================================
FastAPI backend: REST API + WebSocket real-time streaming
Serves the React dashboard at http://localhost:8000

Supports multiple MT5 accounts simultaneously (Midas live + Demo).
Each account has its own WebSocket stream.

Usage:
    python server.py
    # then open http://localhost:8000

Config: edit config.json (copy from config.example.json)
"""

import asyncio
import json
import os
import re
import sys
import time
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Add python/ to import path ─────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR / "python"))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# Optional: ICT precision scanner (only if MT5 data available)
try:
    import ict_precision as ict
    ICT_AVAILABLE = True
except ImportError:
    ICT_AVAILABLE = False

try:
    from trade_memory import TradeMemory
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = SCRIPT_DIR / "config.json"
WEBAPP_DIR  = SCRIPT_DIR / "webapp"
LOG_LINES   = 200   # Max log lines kept in memory per account
STREAM_HZ   = 2.0   # WebSocket push interval (seconds)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("OmniServer")


# ── Load / default config ──────────────────────────────────────────────────────

def _default_mt5_path(filename: str) -> str:
    mac = os.path.expanduser(
        f"~/Library/Application Support/net.metaquotes.wine.metatrader5"
        f"/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/{filename}"
    )
    win = os.path.expanduser(
        f"~/AppData/Roaming/MetaQuotes/Terminal/Common/Files/{filename}"
    )
    return mac if os.path.exists(mac) else win


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Config read error: {e} — using defaults")

    # Auto-detect defaults if no config file
    default_data = _default_mt5_path("omni_data.json")
    default_cmd  = _default_mt5_path("omni_cmd.txt")
    python_dir   = str(SCRIPT_DIR / "python")

    return {
        "accounts": [
            {
                "id":         "primary",
                "name":       "Primary Account",
                "type":       "live",
                "data_path":  default_data,
                "cmd_path":   default_cmd,
                "state_path": str(SCRIPT_DIR / "python" / "trader_state.json"),
                "log_path":   str(SCRIPT_DIR / "python" / "trader.log"),
                "memory_path": str(SCRIPT_DIR / "python" / "trade_memory.json"),
                "journal_path": str(SCRIPT_DIR / "python" / "trade_journal.log"),
            }
        ],
        "server": {"host": "0.0.0.0", "port": 8000},
    }

CONFIG = load_config()
ACCOUNTS = {a["id"]: a for a in CONFIG.get("accounts", [])}

# ── Per-account log ring buffers ──────────────────────────────────────────────

_log_buffers: dict[str, deque] = {
    aid: deque(maxlen=LOG_LINES) for aid in ACCOUNTS
}

# ── Data readers ───────────────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            raw = re.sub(r',\s*([\]}])', r'\1', f.read())
        return json.loads(raw)
    except Exception:
        return {}


def _load_json_clean(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _data_age_secs(path: str) -> float:
    try:
        return time.time() - os.path.getmtime(path)
    except Exception:
        return 9999.0


def _tail_log(path: str, n: int = 80) -> list[str]:
    """Read last n lines from a log file."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []


def build_account_snapshot(account_id: str) -> dict:
    """
    Build a full data snapshot for one account.
    Returns a JSON-serialisable dict consumed by the frontend.
    """
    acc = ACCOUNTS.get(account_id)
    if not acc:
        return {"error": f"Unknown account: {account_id}"}

    data_path  = acc.get("data_path", "")
    state_path = acc.get("state_path", "")
    log_path   = acc.get("log_path", "")
    mem_path   = acc.get("memory_path", "")

    # ── MT5 market data ──────────────────────────────────────────────
    mt5 = _load_json(data_path)
    connected = _data_age_secs(data_path) < 30

    prices    = mt5.get("prices", [])
    positions = mt5.get("positions", [])
    history   = mt5.get("history", [])[-50:]  # last 50 closed trades
    account   = mt5.get("account", {})
    session   = mt5.get("session", "—")
    amd_phase = mt5.get("amd_phase", "—")
    gmt_time  = mt5.get("gmt_time", "—")
    timestamp = mt5.get("timestamp", "—")

    # ── Trader state ─────────────────────────────────────────────────
    state = _load_json_clean(state_path)

    # ── ICT Scanner ──────────────────────────────────────────────────
    setups = []
    scan_error = None
    if ICT_AVAILABLE and connected and mt5:
        try:
            raw_setups = ict.scan_all_primary_symbols()
            for s in raw_setups[:20]:   # cap at 20
                setups.append({
                    "symbol":      s.symbol,
                    "direction":   s.direction,
                    "entry_type":  s.entry_type,
                    "confidence":  s.confidence,
                    "rr_ratio":    s.rr_ratio,
                    "entry_price": s.entry_price,
                    "sl_price":    s.sl_price,
                    "tp1_price":   s.tp1_price,
                    "tp2_price":   s.tp2_price,
                    "tp3_price":   s.tp3_price,
                    "reasons":     s.reasons,
                    "session":     s.session,
                    "amd_phase":   s.amd_phase,
                    "tf_bias":     s.tf_bias,
                    "rr_ratio":    s.rr_ratio,
                    "invalidation": getattr(s, "invalidation", 0),
                })
        except Exception as e:
            scan_error = str(e)

    # Per-symbol analysis (quarter theory, patterns, push/exhaustion)
    symbol_analysis = {}
    if ICT_AVAILABLE and connected and mt5:
        charts = mt5.get("charts", {})
        for sym, sym_data in charts.items():
            try:
                h1_bars  = ict._parse_bars(sym_data.get("H1",  []))
                h4_bars  = ict._parse_bars(sym_data.get("H4",  []))
                d1_bars  = ict._parse_bars(sym_data.get("D1",  []))
                m15_bars = ict._parse_bars(sym_data.get("M15", []))

                if not h1_bars:
                    continue

                d1_high = max((b.h for b in d1_bars[:5]), default=0)
                d1_low  = min((b.l for b in d1_bars[:5] if b.l > 0), default=0)
                cur_price = h1_bars[0].c

                qt = ict.get_quarter_position(cur_price, d1_high, d1_low) if d1_high > 0 else {}
                push = ict.detect_push_exhaustion(h1_bars)
                sr   = ict.find_key_levels(h1_bars)
                d1b  = ict.get_d1_bias(d1_bars) if d1_bars else "NEUTRAL"
                h4s  = ict.get_h4_structure(h4_bars) if h4_bars else "RANGING"

                # Pattern detection
                patterns_found = []
                for fn in [ict.detect_double_top, ict.detect_double_bottom,
                            ict.detect_head_shoulders, ict.detect_inverse_head_shoulders,
                            ict.detect_wedge]:
                    try:
                        res = fn(h1_bars[:30])
                        if res:
                            patterns_found.append(res)
                    except Exception:
                        pass

                symbol_analysis[sym] = {
                    "current_price":   cur_price,
                    "d1_bias":         d1b,
                    "h4_structure":    h4s,
                    "quarter":         qt,
                    "push_exhaustion": push,
                    "support_resistance": {
                        "nearest_support":    sr.get("nearest_support", 0),
                        "nearest_resistance": sr.get("nearest_resistance", 0),
                        "phase_at_support":   sr.get("phase_at_support", ""),
                        "phase_at_resistance": sr.get("phase_at_resistance", ""),
                    },
                    "patterns": patterns_found,
                    "ob_high": sym_data.get("ob_high", 0),
                    "ob_low":  sym_data.get("ob_low", 0),
                    "ob_type": sym_data.get("ob_type", ""),
                    "fvg_high": sym_data.get("fvg_high", 0),
                    "fvg_low":  sym_data.get("fvg_low", 0),
                    "fvg_type": sym_data.get("fvg_type", ""),
                }
            except Exception:
                continue

    # ── Trade memory summary ─────────────────────────────────────────
    memory_data = {}
    if MEMORY_AVAILABLE and os.path.exists(mem_path):
        try:
            mem = TradeMemory(path=mem_path)
            closed = [t for t in mem.trades.values() if t.status != "OPEN"]
            wins   = sum(1 for t in closed if t.status == "WIN")
            total  = len(closed)
            memory_data = {
                "total_trades":  total,
                "wins":          wins,
                "losses":        sum(1 for t in closed if t.status == "LOSS"),
                "win_rate":      round(wins / total * 100, 1) if total > 0 else 0,
                "total_r":       round(sum(t.r_multiple for t in closed), 2),
                "total_pnl":     round(sum(t.profit_usd for t in closed), 2),
                "by_type":       {k: {"wins": v.wins, "losses": v.losses,
                                       "win_rate": round(v.win_rate, 1),
                                       "avg_r": round(v.avg_r, 2)}
                                  for k, v in mem.by_entry_type.items()},
                "by_session":    {k: {"wins": v.wins, "losses": v.losses,
                                       "win_rate": round(v.win_rate, 1)}
                                  for k, v in mem.by_session.items()},
                "by_quarter":    {k: {"wins": v.wins, "losses": v.losses,
                                       "win_rate": round(v.win_rate, 1)}
                                  for k, v in mem.by_quarter.items()},
                "learned_rules": mem.generate_learned_rules()[:10],
                "recent_trades": [
                    {
                        "trade_id":    t.trade_id,
                        "symbol":      t.symbol,
                        "direction":   t.direction,
                        "entry_type":  t.entry_type,
                        "status":      t.status,
                        "r_multiple":  t.r_multiple,
                        "profit_usd":  t.profit_usd,
                        "confidence":  t.confidence,
                        "session":     t.session,
                        "quarter":     t.quarter,
                        "patterns":    t.patterns,
                        "reasons":     t.reasons[:5],
                        "open_time":   t.open_time,
                        "close_time":  t.close_time,
                    }
                    for t in mem.get_recent_trades(15)
                ],
            }
        except Exception as e:
            memory_data = {"error": str(e)}

    # ── Recent log lines ─────────────────────────────────────────────
    log_lines = _tail_log(log_path, 100)

    # ── Bot status ───────────────────────────────────────────────────
    bot_status = {
        "running":       bool(state),
        "paper_mode":    state.get("current_risk_pct") is not None,
        "trading_halted": state.get("trading_halted", False),
        "halt_reason":   state.get("halt_reason", ""),
        "win_streak":    state.get("win_streak", 0),
        "loss_streak":   state.get("loss_streak", 0),
        "current_risk":  state.get("current_risk_pct", 0),
        "total_trades":  state.get("total_trades", 0),
        "winning_trades": state.get("winning_trades", 0),
        "losing_trades": state.get("losing_trades", 0),
        "total_profit":  state.get("total_profit", 0),
        "peak_equity":   state.get("peak_equity", 0),
        "peak_drawdown": state.get("peak_drawdown", 0),
        "last_scan":     state.get("last_scan", ""),
    }

    return {
        "account_id":      account_id,
        "account_name":    acc.get("name", account_id),
        "account_type":    acc.get("type", "unknown"),
        "connected":       connected,
        "data_age_secs":   round(_data_age_secs(data_path), 1),
        "timestamp":       timestamp,
        "gmt_time":        gmt_time,
        "session":         session,
        "amd_phase":       amd_phase,
        "account_info":    account,
        "prices":          prices,
        "positions":       positions,
        "history":         history,
        "setups":          setups,
        "scan_error":      scan_error,
        "symbol_analysis": symbol_analysis,
        "memory":          memory_data,
        "bot_status":      bot_status,
        "log_lines":       log_lines,
    }


# ── FastAPI App ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="OMNI ICT Dashboard",
    description="Real-time autonomous trading bot dashboard",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve React dashboard
app.mount("/static", StaticFiles(directory=str(WEBAPP_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    return FileResponse(str(WEBAPP_DIR / "index.html"))


# ── REST API ───────────────────────────────────────────────────────────────────

@app.get("/api/accounts")
async def get_accounts():
    return [
        {"id": a["id"], "name": a["name"], "type": a["type"]}
        for a in CONFIG.get("accounts", [])
    ]


@app.get("/api/data/{account_id}")
async def get_account_data(account_id: str):
    if account_id not in ACCOUNTS:
        raise HTTPException(404, f"Account '{account_id}' not found")
    return JSONResponse(build_account_snapshot(account_id))


@app.get("/api/rules")
async def get_rules():
    rules_path = SCRIPT_DIR / "python" / "rules.json"
    if rules_path.exists():
        with open(rules_path) as f:
            return json.load(f)
    return {}


@app.get("/api/status")
async def get_status():
    return {
        "server":   "OMNI ICT Dashboard v2.0",
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "accounts": [
            {
                "id":        aid,
                "name":      acc["name"],
                "connected": _data_age_secs(acc.get("data_path", "")) < 30,
            }
            for aid, acc in ACCOUNTS.items()
        ],
        "ict_scanner":   ICT_AVAILABLE,
        "trade_memory":  MEMORY_AVAILABLE,
    }


# ── WebSocket (per account) ────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: dict[str, list[WebSocket]] = {}

    async def connect(self, ws: WebSocket, account_id: str):
        await ws.accept()
        if account_id not in self.active:
            self.active[account_id] = []
        self.active[account_id].append(ws)
        log.info(f"WS connected: account={account_id} total={len(self.active[account_id])}")

    def disconnect(self, ws: WebSocket, account_id: str):
        if account_id in self.active:
            try:
                self.active[account_id].remove(ws)
            except ValueError:
                pass

    async def broadcast(self, account_id: str, data: dict):
        ws_list = self.active.get(account_id, [])
        dead = []
        for ws in ws_list:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, account_id)


manager = ConnectionManager()

# Track last log position per account (tail new lines only)
_log_positions: dict[str, int] = {}


def _get_new_log_lines(account_id: str) -> list[str]:
    """Return only newly-appended lines since last call."""
    acc = ACCOUNTS.get(account_id, {})
    log_path = acc.get("log_path", "")
    if not log_path or not os.path.exists(log_path):
        return []

    prev_pos = _log_positions.get(account_id, 0)
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            cur_size = f.tell()

            if cur_size < prev_pos:
                # File was rotated
                _log_positions[account_id] = 0
                prev_pos = 0

            if cur_size == prev_pos:
                return []

            f.seek(prev_pos)
            new_lines = f.read().splitlines()
            _log_positions[account_id] = cur_size

        return [l for l in new_lines if l.strip()]
    except Exception:
        return []


@app.websocket("/ws/{account_id}")
async def websocket_endpoint(ws: WebSocket, account_id: str):
    if account_id not in ACCOUNTS:
        await ws.close(code=4004)
        return

    await manager.connect(ws, account_id)

    # Seed log position at current end-of-file (don't replay whole history)
    acc = ACCOUNTS.get(account_id, {})
    lp = acc.get("log_path", "")
    if lp and os.path.exists(lp):
        try:
            with open(lp) as f:
                f.seek(0, 2)
                _log_positions[account_id] = f.tell()
        except Exception:
            pass

    try:
        # Send initial full snapshot
        snapshot = build_account_snapshot(account_id)
        await ws.send_json({"type": "snapshot", **snapshot})

        while True:
            await asyncio.sleep(STREAM_HZ)

            # Lightweight update: only what changes frequently
            acc_cfg  = ACCOUNTS[account_id]
            mt5      = _load_json(acc_cfg.get("data_path", ""))
            state    = _load_json_clean(acc_cfg.get("state_path", ""))
            new_logs = _get_new_log_lines(account_id)

            # Run ICT scanner (only if data fresh)
            setups_fast = []
            if ICT_AVAILABLE and _data_age_secs(acc_cfg.get("data_path", "")) < 30:
                try:
                    raw = ict.scan_all_primary_symbols()
                    for s in raw[:15]:
                        setups_fast.append({
                            "symbol":     s.symbol,
                            "direction":  s.direction,
                            "entry_type": s.entry_type,
                            "confidence": s.confidence,
                            "rr_ratio":   s.rr_ratio,
                            "entry_price": s.entry_price,
                            "sl_price":   s.sl_price,
                            "tp1_price":  s.tp1_price,
                            "reasons":    s.reasons,
                            "session":    s.session,
                            "tf_bias":    s.tf_bias,
                        })
                except Exception:
                    pass

            update = {
                "type":      "update",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "connected": _data_age_secs(acc_cfg.get("data_path", "")) < 30,
                "session":   mt5.get("session", "—"),
                "amd_phase": mt5.get("amd_phase", "—"),
                "gmt_time":  mt5.get("gmt_time", "—"),
                "account_info": mt5.get("account", {}),
                "prices":    mt5.get("prices", []),
                "positions": mt5.get("positions", []),
                "setups":    setups_fast,
                "new_log_lines": new_logs,
                "bot_status": {
                    "win_streak":    state.get("win_streak", 0),
                    "loss_streak":   state.get("loss_streak", 0),
                    "current_risk":  state.get("current_risk_pct", 0),
                    "total_trades":  state.get("total_trades", 0),
                    "winning_trades": state.get("winning_trades", 0),
                    "losing_trades": state.get("losing_trades", 0),
                    "total_profit":  state.get("total_profit", 0),
                    "trading_halted": state.get("trading_halted", False),
                    "halt_reason":   state.get("halt_reason", ""),
                    "last_scan":     state.get("last_scan", ""),
                },
            }

            await ws.send_json(update)

    except WebSocketDisconnect:
        manager.disconnect(ws, account_id)
        log.info(f"WS disconnected: account={account_id}")
    except Exception as e:
        log.error(f"WS error ({account_id}): {e}")
        manager.disconnect(ws, account_id)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    server_cfg = CONFIG.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8000)

    log.info(f"OMNI ICT Dashboard starting on http://{host}:{port}")
    log.info(f"Accounts: {list(ACCOUNTS.keys())}")
    log.info(f"ICT Scanner: {'available' if ICT_AVAILABLE else 'not available'}")
    log.info(f"Trade Memory: {'available' if MEMORY_AVAILABLE else 'not available'}")

    uvicorn.run(app, host=host, port=port, log_level="warning")
