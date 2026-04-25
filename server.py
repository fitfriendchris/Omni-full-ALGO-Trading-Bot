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
        "server": {"host": "0.0.0.0", "port": 8787},
    }

CONFIG = load_config()
ACCOUNTS = {a["id"]: a for a in CONFIG.get("accounts", [])}

# ── Per-account log ring buffers ──────────────────────────────────────────────

_log_buffers: dict[str, deque] = {
    aid: deque(maxlen=LOG_LINES) for aid in ACCOUNTS
}

# ── Data readers ───────────────────────────────────────────────────────────────

_MT5_CACHE: dict = {"data": {}, "mtime": 0.0}

# Use orjson for fast parsing (10× faster than stdlib for large files)
try:
    import orjson as _orjson
    def _json_loads(raw_bytes: bytes) -> dict:
        try:
            return _orjson.loads(raw_bytes)
        except Exception:
            # Fall back to stdlib with trailing-comma strip
            text = raw_bytes.decode("utf-8", errors="replace")
            return json.loads(re.sub(r',\s*([\]}])', r'\1', text))
except ImportError:
    _orjson = None  # type: ignore
    def _json_loads(raw_bytes: bytes) -> dict:
        text = raw_bytes.decode("utf-8", errors="replace")
        return json.loads(re.sub(r',\s*([\]}])', r'\1', text))


def _load_json(path: str) -> dict:
    """Load MT5 JSON with mtime-based cache — avoids re-parsing 55MB file on every tick."""
    # Pick freshest between .json and .tmp
    tmp = path + ".tmp"
    best_path, best_mtime = path, 0.0
    for p in (path, tmp):
        try:
            m = os.path.getmtime(p)
            if m > best_mtime:
                best_mtime, best_path = m, p
        except OSError:
            pass
    if not best_mtime:
        return _MT5_CACHE["data"] or {}
    if best_mtime == _MT5_CACHE["mtime"]:
        return _MT5_CACHE["data"]
    # New data — try freshest file, fall back to the other on parse error
    for p in (best_path, path if best_path == tmp else tmp):
        try:
            with open(p, "rb") as f:
                raw = f.read()
            data = _json_loads(raw)
            _MT5_CACHE["data"]  = data
            _MT5_CACHE["mtime"] = best_mtime
            return data
        except Exception:
            continue
    return _MT5_CACHE["data"] or {}


def _load_json_clean(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _data_age_secs(path: str) -> float:
    """Return age in seconds of path, also checking path+'.tmp' (EA writes .tmp then renames)."""
    try:
        best = max(
            os.path.getmtime(path) if os.path.exists(path) else 0,
            os.path.getmtime(path + ".tmp") if os.path.exists(path + ".tmp") else 0,
        )
        return time.time() - best if best else 9999.0
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

    # ── ICT Scanner — use cached results, skip on cold start ─────────
    setups = []
    scan_error = None
    if ICT_AVAILABLE and connected and mt5:
        try:
            # Only scan if cache is warm (mtime matches) — avoids blocking
            # the initial snapshot on a cold 55MB parse
            from ict_precision import _SCAN_CACHE, _DATA_CACHE
            if _SCAN_CACHE.get("mtime") and _SCAN_CACHE["mtime"] == _DATA_CACHE.get("mtime"):
                raw_setups = _SCAN_CACHE.get("result", [])
            else:
                raw_setups = ict.scan_all_primary_symbols()
            for s in raw_setups[:50]:
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
                    "kill_zone":   getattr(s, "kill_zone", None),
                    "invalidation": getattr(s, "invalidation", 0),
                })
        except Exception as e:
            scan_error = str(e)

    # Per-symbol analysis — only for configured watchlist, not all 28 charts
    symbol_analysis = {}
    if ICT_AVAILABLE and connected and mt5:
        charts = mt5.get("charts", {})
        try:
            from config import cfg as _cfg
            _watchlist = set(_cfg.TRADE_SYMBOLS)
        except Exception:
            _watchlist = set(CONFIG.get("watchlist", ["XAUUSD","XAGUSD","EURUSD","GBPUSD","USDJPY","GBPJPY","AUDUSD","USDCAD"]))
        for sym, sym_data in {k: v for k, v in charts.items() if k in _watchlist}.items():
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
    # Kill zone (local UTC fallback)
    try:
        from ict_precision import get_killzone
        _kz = get_killzone()
    except Exception:
        _kz = None

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
        "risk_mode":     state.get("risk_mode", ""),
        "freq_mode":     state.get("freq_mode", ""),
        "base_risk":     state.get("base_risk", 0),
        "max_dd":        state.get("max_dd", 0),
        "daily_limit":   state.get("daily_limit", 0),
        "kill_zone":     _kz,
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


@app.get("/smart-trail", include_in_schema=False)
async def serve_smart_trail_dashboard():
    """Smart Trail dev dashboard — paper-mode visibility into trail proposals."""
    return FileResponse(str(WEBAPP_DIR / "smart_trail.html"))


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


@app.get("/api/smart_trail/{account_id}")
async def get_smart_trail(account_id: str):
    """
    Telemetry for the smart trailing stop engine.
    Returns:
      - config:     rules.json → smart_trail block
      - enabled:    top-level flag
      - proposals:  state.last_trail_proposals (per-ticket proposal snapshot)
      - context:    state.last_scan_context (per-symbol SMC scan context)
      - positions:  open positions with entry/sl/tp/current for side-by-side viewing
    """
    if account_id not in ACCOUNTS:
        raise HTTPException(404, f"Account '{account_id}' not found")
    acc = ACCOUNTS[account_id]

    rules_path = SCRIPT_DIR / "python" / "rules.json"
    rules = _load_json_clean(str(rules_path)) if rules_path.exists() else {}
    smart_trail_cfg = rules.get("smart_trail", {}) or {}

    state = _load_json_clean(acc.get("state_path", ""))
    data  = _load_json(acc.get("data_path", ""))

    proposals = state.get("last_trail_proposals", {}) or {}
    scan_ctx  = state.get("last_scan_context", {}) or {}
    positions = [
        {
            "ticket":        str(p.get("ticket", "")),
            "symbol":        p.get("symbol", ""),
            "type":          p.get("type", ""),
            "volume":        p.get("volume", 0),
            "open_price":    p.get("open_price", 0),
            "sl":            p.get("sl", 0),
            "tp":            p.get("tp", 0),
            "current_price": p.get("current_price", 0),
            "profit":        p.get("profit", 0),
        }
        for p in (data.get("positions", []) or [])
    ]

    return JSONResponse({
        "enabled":   bool(smart_trail_cfg.get("enabled", False)),
        "config":    smart_trail_cfg,
        "proposals": proposals,
        "context":   scan_ctx,
        "positions": positions,
        "state_age_secs": _data_age_secs(acc.get("state_path", "")),
        "data_age_secs":  _data_age_secs(acc.get("data_path", "")),
    })


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
        loop = asyncio.get_event_loop()

        # ── Fast minimal snapshot — sent immediately so the dashboard goes "online" ──
        # Skips all per-symbol ICT analysis and trade-memory (those take 5-30 s on cold
        # start).  The update loop populates setups / symbol_analysis in the next tick.
        def _fast_snapshot():
            a    = ACCOUNTS[account_id]
            mt5  = _load_json(a.get("data_path", ""))
            st   = _load_json_clean(a.get("state_path", ""))
            age  = _data_age_secs(a.get("data_path", ""))
            return {
                "account_id":      account_id,
                "account_name":    a.get("name", account_id),
                "account_type":    a.get("type", "unknown"),
                "connected":       age < 30,
                "data_age_secs":   round(age, 1),
                "timestamp":       mt5.get("timestamp", "—"),
                "gmt_time":        mt5.get("gmt_time", "—"),
                "session":         mt5.get("session", "—"),
                "amd_phase":       mt5.get("amd_phase", "—"),
                "account_info":    mt5.get("account", {}),
                "prices":          mt5.get("prices", []),
                "positions":       mt5.get("positions", []),
                "history":         mt5.get("history", [])[-50:],
                "setups": [
                    {
                        "symbol":      s.get("symbol", ""),
                        "direction":   s.get("direction", ""),
                        "entry_type":  s.get("entry_type", ""),
                        "confidence":  s.get("confidence", 0),
                        "rr_ratio":    s.get("rr_ratio", 0),
                        "entry_price": s.get("entry_price", 0),
                        "sl_price":    s.get("sl_price", 0),
                        "tp1_price":   s.get("tp1_price", 0),
                        "tp2_price":   s.get("tp2_price", 0),
                        "tp3_price":   s.get("tp3_price", 0),
                        "reasons":     s.get("reasons", []),
                        "session":     s.get("session", ""),
                        "tf_bias":     s.get("tf_bias", ""),
                        "amd_phase":   s.get("amd_phase", ""),
                        "grade":       s.get("grade", ""),
                        "invalidation": s.get("invalidation", 0),
                    }
                    for s in st.get("top_setups", [])[:15]
                ],
                "scan_error":      None,
                "symbol_analysis": {},
                "memory":          {},
                "bot_status": {
                    "running":        bool(st),
                    "trading_halted": st.get("trading_halted", False),
                    "halt_reason":    st.get("halt_reason", ""),
                    "win_streak":     st.get("win_streak", 0),
                    "loss_streak":    st.get("loss_streak", 0),
                    "current_risk":   st.get("current_risk_pct", 0),
                    "total_trades":   st.get("total_trades", 0),
                    "winning_trades": st.get("winning_trades", 0),
                    "losing_trades":  st.get("losing_trades", 0),
                    "total_profit":   st.get("total_profit", 0),
                    "peak_equity":    st.get("peak_equity", 0),
                    "peak_drawdown":  st.get("peak_drawdown", 0),
                    "last_scan":      st.get("last_scan", ""),
                    "paper_mode":     st.get("current_risk_pct") is not None,
                },
                "log_lines": _tail_log(a.get("log_path", ""), 100),
            }

        fast = await loop.run_in_executor(None, _fast_snapshot)
        await ws.send_json({"type": "snapshot", **fast})

        while True:
            await asyncio.sleep(STREAM_HZ)

            # Run all I/O in a thread pool so the event loop stays free.
            # Setups are read from trader_state.json (written by auto_trader every scan)
            # so the server never needs to run its own ICT scan.
            def _build_update():
                acc_cfg  = ACCOUNTS[account_id]
                mt5      = _load_json(acc_cfg.get("data_path", ""))
                state    = _load_json_clean(acc_cfg.get("state_path", ""))
                new_logs = _get_new_log_lines(account_id)
                # Read setups computed by auto_trader (stored in state["top_setups"])
                setups_fast = [
                    {
                        "symbol":      s.get("symbol", ""),
                        "direction":   s.get("direction", ""),
                        "entry_type":  s.get("entry_type", ""),
                        "confidence":  s.get("confidence", 0),
                        "rr_ratio":    s.get("rr_ratio", 0),
                        "entry_price": s.get("entry_price", 0),
                        "sl_price":    s.get("sl_price", 0),
                        "tp1_price":   s.get("tp1_price", 0),
                        "tp2_price":   s.get("tp2_price", 0),
                        "tp3_price":   s.get("tp3_price", 0),
                        "reasons":     s.get("reasons", []),
                        "session":     s.get("session", ""),
                        "tf_bias":     s.get("tf_bias", ""),
                        "amd_phase":   s.get("amd_phase", ""),
                        "grade":       s.get("grade", ""),
                        "invalidation": s.get("invalidation", 0),
                    }
                    for s in state.get("top_setups", [])[:15]
                ]
                return mt5, state, new_logs, setups_fast, acc_cfg

            mt5, state, new_logs, setups_fast, acc_cfg = await loop.run_in_executor(None, _build_update)

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
    host = os.getenv("OMNI_SERVER_HOST", server_cfg.get("host", "0.0.0.0"))
    port = int(os.getenv("OMNI_SERVER_PORT", server_cfg.get("port", 8787)))

    log.info(f"OMNI ICT Dashboard starting on http://{host}:{port}")
    log.info(f"Accounts: {list(ACCOUNTS.keys())}")
    log.info(f"ICT Scanner: {'available' if ICT_AVAILABLE else 'not available'}")
    log.info(f"Trade Memory: {'available' if MEMORY_AVAILABLE else 'not available'}")

    uvicorn.run(app, host=host, port=port, log_level="warning")
