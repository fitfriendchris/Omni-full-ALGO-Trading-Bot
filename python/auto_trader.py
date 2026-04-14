"""
auto_trader.py — OMNI ICT Auto-Trading Engine
Compounding 2% risk | Multi-TF ICT entries | Full trade management

⚠️  PAPER MODE IS ON BY DEFAULT
    To enable live trading set OMNI_PAPER_MODE=false in your environment,
    or set "paper_mode": false in config.json.
    Always test on a demo account first!

Kill switch: touch the file at KILL_SWITCH_PATH (default: python/HALT) to
halt trading instantly.  Remove the file to resume.

Run: python auto_trader.py
"""

import os
import sys
import json
import time
import re
import math
import logging
import logging.handlers
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── Centralised config (paths, thresholds, paper/live toggle) ─────────────────
from config import cfg

PAPER_MODE       = cfg.PAPER_MODE
BASE_RISK_PCT    = cfg.BASE_RISK_PCT
MAX_RISK_PCT     = cfg.MAX_RISK_PCT
MIN_RISK_PCT     = cfg.MIN_RISK_PCT
MAX_OPEN_TRADES  = cfg.MAX_OPEN_TRADES
DAILY_LOSS_LIMIT = cfg.DAILY_LOSS_LIMIT
MAX_DD_FROM_PEAK = cfg.MAX_DD_FROM_PEAK
SCAN_INTERVAL    = cfg.SCAN_INTERVAL
MIN_RR           = cfg.MIN_RR
MIN_CONFIDENCE   = cfg.MIN_CONFIDENCE
MIN_CONFIDENCE_ASIA = cfg.MIN_CONFIDENCE_ASIA
MIN_SL_PIPS      = cfg.MIN_SL_PIPS
OUR_MAGIC        = cfg.MAGIC

SESSION_FILTER   = None   # None = all sessions; session-specific thresholds in execute_setup

TRADE_SYMBOLS    = cfg.TRADE_SYMBOLS

JSON_PATH        = cfg.JSON_PATH
CMD_PATH         = cfg.CMD_PATH
RESULT_PATH      = cfg.RESULT_PATH
STATE_PATH       = cfg.STATE_PATH
LOG_PATH         = cfg.LOG_PATH
KILL_SWITCH_PATH = cfg.KILL_SWITCH_PATH

# ── Logging (with rotation — max 10 MB, keep 5 files) ────────────────────────
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
log = logging.getLogger("OMNI")

# ── State ─────────────────────────────────────────────────────────────────────
@dataclass
class TraderState:
    # Compounding
    current_risk_pct:    float = BASE_RISK_PCT
    win_streak:          int   = 0
    loss_streak:         int   = 0
    total_trades:        int   = 0
    winning_trades:      int   = 0
    losing_trades:       int   = 0
    total_profit:        float = 0.0
    peak_equity:         float = 0.0
    peak_drawdown:       float = 0.0     # Max % drawdown seen from peak

    # Daily tracking
    day_start_equity:    float = 0.0
    day_initialized:     bool  = False   # Prevents treating equity==0 as "unset"
    last_reset_day:      str   = ""      # "YYYY-MM-DD" UTC — fires reset exactly once per day
    day_profit:          float = 0.0
    day_trades:          int   = 0
    trading_halted:      bool  = False
    halt_reason:         str   = ""

    # Active setups (pending orders we placed)
    pending_orders:      dict  = field(default_factory=dict)
    active_trades:       dict  = field(default_factory=dict)  # ticket -> trade info + TP levels
    recently_traded:     list  = field(default_factory=list)  # symbols to avoid re-entry

    # Statistics
    start_time:          str   = ""
    last_scan:           str   = ""


def load_state() -> TraderState:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                d = json.load(f)
            s = TraderState()
            for k, v in d.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            return s
        except Exception:
            pass
    return TraderState()


def save_state(state: TraderState):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(asdict(state), f, indent=2)
    except Exception as e:
        log.error(f"State save error: {e}")


# ── Data Loading ──────────────────────────────────────────────────────────────
def load_mt5_data() -> dict:
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = re.sub(r',\s*([\]}])', r'\1', f.read())
        return json.loads(raw)
    except Exception as e:
        log.warning(f"Data load error: {e}")
        return {}


def get_account(data: dict) -> dict:
    return data.get("account", {})


def get_open_positions(data: dict) -> list:
    return data.get("positions", [])


# ── Position Sizing (Compounding) ─────────────────────────────────────────────
def calculate_lot_size(equity: float, risk_pct: float, entry: float,
                       sl: float, symbol: str, sym_info: dict) -> float:
    """
    Compound position sizing:
    lot_size = (equity × risk%) / (sl_distance × tick_value / tick_size)
    """
    if equity <= 0 or entry <= 0 or sl <= 0:
        return 0.0

    risk_amount  = equity * (risk_pct / 100)
    sl_distance  = abs(entry - sl)
    tick_size    = sym_info.get("tick_size", 0.01)
    tick_value   = sym_info.get("tick_value", 1.0)
    min_lot      = sym_info.get("min_lot", 0.01)
    max_lot      = sym_info.get("max_lot", 100.0)
    lot_step     = sym_info.get("lot_step", 0.01)

    if tick_size == 0 or tick_value == 0:
        return min_lot

    # Risk per lot = (sl_distance / tick_size) × tick_value
    risk_per_lot = (sl_distance / tick_size) * tick_value
    if risk_per_lot <= 0:
        return min_lot

    lot_size = risk_amount / risk_per_lot

    # Round down to lot step, then clamp
    lot_size = math.floor(lot_size / lot_step) * lot_step
    lot_size = max(min_lot, min(lot_size, max_lot))

    return round(lot_size, 2)


# ── Risk Adjuster (Compounding logic) ─────────────────────────────────────────
def adjust_risk(state: TraderState) -> float:
    """
    Adaptive risk scaling:
      Win streak 3   → +0.25% (compound up)
      Win streak 5   → +0.25% more (capped at MAX_RISK_PCT)
      Loss streak 2  → -0.25% (protect capital)
      Loss streak 3  → -0.25% more (capped at MIN_RISK_PCT)
    Risk resets toward BASE on new day (done in reset_daily_if_new_day).
    """
    risk = state.current_risk_pct

    if state.win_streak >= 5:
        new_risk = min(BASE_RISK_PCT + 0.50, MAX_RISK_PCT)
        if new_risk > risk:
            risk = new_risk
            log.info(f"Win streak {state.win_streak} — scaling risk UP to {risk:.2f}%")
    elif state.win_streak >= 3:
        new_risk = min(BASE_RISK_PCT + 0.25, MAX_RISK_PCT)
        if new_risk > risk:
            risk = new_risk
            log.info(f"Win streak {state.win_streak} — scaling risk UP to {risk:.2f}%")
    elif state.loss_streak >= 3:
        risk = max(risk - 0.25, MIN_RISK_PCT)
        log.info(f"Loss streak {state.loss_streak} — risk REDUCED to {risk:.2f}%")
    elif state.loss_streak >= 2:
        risk = max(risk - 0.25, MIN_RISK_PCT)
        log.info(f"Loss streak {state.loss_streak} — risk reduced to {risk:.2f}%")

    state.current_risk_pct = risk
    return risk


# ── Command Sender ────────────────────────────────────────────────────────────
def send_command(command: str) -> str:
    """Write command to file for MT5 EA to execute."""
    if PAPER_MODE:
        log.info(f"[PAPER] Would execute: {command}")
        return "PAPER|simulated"

    try:
        with open(CMD_PATH, "w") as f:
            f.write(command)
        # Poll for result — use try/except to avoid TOCTOU race
        for _ in range(30):  # 3 second timeout
            time.sleep(0.1)
            try:
                with open(RESULT_PATH) as f:
                    result = f.read().strip()
                os.remove(RESULT_PATH)
                log.info(f"EA Result: {result}")
                return result
            except FileNotFoundError:
                pass  # File not ready yet, keep polling
        return "TIMEOUT|no response from EA"
    except Exception as e:
        return f"ERROR|{e}"


def place_order(symbol: str, direction: str, order_type: str,
                price: float, sl: float, tp: float,
                volume: float, comment: str) -> str:
    """Place a pending or market order."""
    cmd = f"OPEN|{symbol}|{order_type}|{price:.5f}|{sl:.5f}|{tp:.5f}|{volume:.2f}|{comment}"
    return send_command(cmd)


def close_position(ticket: int, volume: float = 0) -> str:
    vol_str = f"{volume:.2f}" if volume > 0 else ""
    cmd = f"CLOSE|{ticket}|||||{vol_str}|"
    return send_command(cmd)


def modify_position(ticket: int, sl: float, tp: float) -> str:
    cmd = f"MODIFY|{ticket}|||{sl:.5f}|{tp:.5f}||"
    return send_command(cmd)


# ── Daily Loss / Drawdown Check ───────────────────────────────────────────────
def check_daily_limits(state: TraderState, equity: float) -> bool:
    """
    Returns False if trading should be halted.
    Checks both the daily loss limit and max drawdown from peak equity.
    """
    if state.trading_halted:
        return False

    # Initialize baseline on first run (use bool flag, not equity==0 sentinel)
    if not state.day_initialized:
        state.day_initialized = True
        state.day_start_equity = equity
        state.last_reset_day = datetime.utcnow().strftime("%Y-%m-%d")
        return True

    # Daily loss check
    if state.day_start_equity > 0:
        day_loss_pct = ((equity - state.day_start_equity) / state.day_start_equity) * 100
        if day_loss_pct <= -DAILY_LOSS_LIMIT:
            state.trading_halted = True
            state.halt_reason = f"Daily loss limit hit: {day_loss_pct:.2f}%"
            log.warning(f"TRADING HALTED: {state.halt_reason}")
            return False

    # Peak drawdown check
    if state.peak_equity > 0:
        drawdown_pct = ((state.peak_equity - equity) / state.peak_equity) * 100
        if drawdown_pct > state.peak_drawdown:
            state.peak_drawdown = drawdown_pct
        if drawdown_pct >= MAX_DD_FROM_PEAK:
            state.trading_halted = True
            state.halt_reason = f"Max drawdown from peak hit: {drawdown_pct:.2f}%"
            log.warning(f"TRADING HALTED: {state.halt_reason}")
            return False

    return True


def reset_daily_if_new_day(state: TraderState, equity: float):
    """
    Reset daily stats exactly once per UTC calendar day.
    Uses last_reset_day (YYYY-MM-DD) so it fires once regardless of
    how many scans run in the first minute, and survives restarts.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if state.last_reset_day == today:
        return  # Already reset today

    log.info(f"New trading day ({today}) — resetting daily stats")
    state.last_reset_day    = today
    state.day_initialized   = True
    state.day_start_equity  = equity
    state.day_profit        = 0.0
    state.day_trades        = 0
    state.trading_halted    = False
    state.halt_reason       = ""
    state.recently_traded   = []
    save_state(state)  # Persist immediately so a crash can't re-trigger the reset


# ── Trade Management ──────────────────────────────────────────────────────────
def manage_open_trades(state: TraderState, data: dict, memory=None):
    """
    Manage existing trades:
    - Detect closes using MT5 history for accurate settled P&L
    - Adjust risk streak once after all closes in a cycle
    - Take partial profits at TP1 (50%) and TP2 (30%), let 20% run to TP3
    - Trail SL to break-even (with 1-pip buffer) at 1R, then to 1R at 2R, tight at 3R
    - Use per-symbol pip size for the modify threshold
    """
    positions       = get_open_positions(data)
    position_tickets = {str(p.get("ticket")): p for p in positions}
    charts           = data.get("charts", {})

    # History lookup for accurate settled P&L (commission + swap included)
    history_by_ticket = {
        str(h.get("ticket", "")): h
        for h in data.get("history", [])
        if isinstance(h, dict)
    }

    # Detect closed trades (were in active_trades, no longer in open positions)
    closed_tickets = [t for t in list(state.active_trades.keys())
                      if t not in position_tickets]

    any_closed = False
    for ticket in closed_tickets:
        trade = state.active_trades.pop(ticket, {})

        # Use history for settled P&L; fall back to last floating value if not yet exported
        if ticket in history_by_ticket:
            h = history_by_ticket[ticket]
            profit = h.get("profit", 0) + h.get("swap", 0) + h.get("commission", 0)
        else:
            profit = trade.get("last_profit", 0)

        entry_price   = trade.get("entry", 0)
        sl_price      = trade.get("sl", 0)
        close_price   = history_by_ticket.get(ticket, {}).get("price", 0) if ticket in history_by_ticket else 0
        # R-multiple = profit / (risk per unit × lot size).  Use SL distance for risk.
        risk_distance = abs(entry_price - sl_price) if (entry_price > 0 and sl_price > 0) else 1
        risk_usd_per_r = risk_distance   # approximate; memory records full USD via lot size
        r_multiple    = round(profit / (risk_distance * 10000), 2) if risk_distance > 0 else 0
        # Simpler meaningful R: sign follows profit, magnitude is pips gained / pips risked
        if entry_price > 0 and sl_price > 0 and risk_distance > 0 and close_price > 0:
            pips_gained = abs(close_price - entry_price)
            r_multiple  = round((pips_gained / risk_distance) * (1 if profit >= 0 else -1), 2)

        # Determine exit level
        tp1 = trade.get("tp1", 0)
        tp2 = trade.get("tp2", 0)
        tp3 = trade.get("tp3", 0)
        if profit >= 0 and tp3 > 0 and close_price:
            direction = trade.get("direction", "")
            if direction == "BUY":
                if close_price >= tp3: exit_level = "TP3"
                elif close_price >= tp2: exit_level = "TP2"
                elif close_price >= tp1: exit_level = "TP1"
                else: exit_level = "PARTIAL"
            elif direction == "SELL":
                if close_price <= tp3: exit_level = "TP3"
                elif close_price <= tp2: exit_level = "TP2"
                elif close_price <= tp1: exit_level = "TP1"
                else: exit_level = "PARTIAL"
            else: exit_level = "WIN"
        elif profit < 0:
            exit_level = "SL"
        else:
            exit_level = "MANUAL"

        if profit >= 0:
            state.win_streak   += 1
            state.loss_streak   = 0
            state.winning_trades += 1
            log.info(f"Trade {ticket} CLOSED WIN: +{profit:.2f} | R={r_multiple:.2f} | Streak: {state.win_streak}W | Exit: {exit_level}")
        else:
            state.loss_streak  += 1
            state.win_streak    = 0
            state.losing_trades += 1
            log.info(f"Trade {ticket} CLOSED LOSS: {profit:.2f} | R={r_multiple:.2f} | Streak: {state.loss_streak}L | Exit: {exit_level}")

        state.total_trades  += 1
        state.total_profit  += profit
        any_closed = True

        # Record outcome in trade memory
        if memory and trade.get("memory_trade_id"):
            try:
                memory.record_close(
                    trade_id=trade["memory_trade_id"],
                    close_price=close_price or entry_price,
                    profit_usd=profit,
                    r_multiple=r_multiple,
                    tp_level_hit=exit_level,
                    close_reason=f"Auto-closed | profit={profit:.2f}",
                )
            except Exception as e:
                log.warning(f"Memory record_close error: {e}")

    # Adjust risk once after all closures (prevents double-adjustment within one scan)
    if any_closed:
        adjust_risk(state)

    # Manage active positions
    for ticket_str, pos in position_tickets.items():
        magic = pos.get("magic", 0)

        # Live mode: only manage OMNI's trades by magic number
        # Paper mode: only manage positions we explicitly opened (tracked in active_trades)
        if not PAPER_MODE and magic != OUR_MAGIC:
            continue
        if PAPER_MODE and ticket_str not in state.active_trades:
            continue

        symbol        = pos.get("symbol", "")
        current_price = pos.get("current_price", 0)
        open_price    = pos.get("open_price", 0)
        current_sl    = pos.get("sl", 0)
        current_tp    = pos.get("tp", 0)
        profit        = pos.get("profit", 0)
        pos_type      = pos.get("type", "")
        volume        = pos.get("volume", 0)

        # Track last floating P&L as fallback for win/loss detection
        if ticket_str not in state.active_trades:
            state.active_trades[ticket_str] = {}
        state.active_trades[ticket_str]["last_profit"] = profit

        if current_price == 0 or open_price == 0 or current_sl == 0:
            continue

        # Per-symbol pip size for thresholds
        sym_info = charts.get(symbol, {})
        pip_size = sym_info.get("point", 0.0001) * 10
        min_lot  = sym_info.get("min_lot", 0.01)

        setup     = state.active_trades[ticket_str]
        tp1       = setup.get("tp1", current_tp)
        tp2       = setup.get("tp2", current_tp)
        tp3       = setup.get("tp3", current_tp)
        tp1_taken = setup.get("tp1_taken", False)
        tp2_taken = setup.get("tp2_taken", False)
        risk      = abs(open_price - current_sl)

        if risk == 0:
            continue

        new_sl = current_sl
        new_tp = current_tp

        if pos_type == "BUY":
            profit_in_r = (current_price - open_price) / risk

            # Partial TP1: close 50% when price hits first target
            if not tp1_taken and current_price >= tp1:
                close_vol = round(volume * 0.5, 2)
                if close_vol >= min_lot:
                    if ticket_str.isdigit():
                        result = close_position(int(ticket_str), close_vol)
                    else:
                        result = "PAPER|partial_tp1"
                    state.active_trades[ticket_str]["tp1_taken"] = True
                    log.info(f"{symbol} TP1 hit — closed 50% ({close_vol} lots) | {result}")

            # Partial TP2: close 30% of original when price hits second target
            if tp1_taken and not tp2_taken and current_price >= tp2:
                close_vol = round(volume * 0.30, 2)
                if close_vol >= min_lot:
                    if ticket_str.isdigit():
                        result = close_position(int(ticket_str), close_vol)
                    else:
                        result = "PAPER|partial_tp2"
                    state.active_trades[ticket_str]["tp2_taken"] = True
                    log.info(f"{symbol} TP2 hit — closed 30% ({close_vol} lots) | {result}")

            # Trail SL to break-even + 1 pip (covers spread on reversal)
            if profit_in_r >= 1.0 and current_sl < open_price:
                new_sl = open_price + pip_size
                log.info(f"{symbol} BUY: Moving SL to break-even ({new_sl:.5f})")

            # Trail SL to 1R profit at 2R move
            if profit_in_r >= 2.0 and current_sl < open_price + risk:
                new_sl = max(new_sl, open_price + risk)
                log.info(f"{symbol} BUY: Trailing SL to 1R ({new_sl:.5f})")

            # Tight trail at 3R (runner management)
            if profit_in_r >= 3.0:
                new_sl = max(new_sl, current_price - risk * 0.5)
                log.info(f"{symbol} BUY: Tight trail at 3R ({new_sl:.5f})")

        elif pos_type == "SELL":
            profit_in_r = (open_price - current_price) / risk

            # Partial TP1
            if not tp1_taken and current_price <= tp1:
                close_vol = round(volume * 0.5, 2)
                if close_vol >= min_lot:
                    if ticket_str.isdigit():
                        result = close_position(int(ticket_str), close_vol)
                    else:
                        result = "PAPER|partial_tp1"
                    state.active_trades[ticket_str]["tp1_taken"] = True
                    log.info(f"{symbol} TP1 hit — closed 50% ({close_vol} lots) | {result}")

            # Partial TP2
            if tp1_taken and not tp2_taken and current_price <= tp2:
                close_vol = round(volume * 0.30, 2)
                if close_vol >= min_lot:
                    if ticket_str.isdigit():
                        result = close_position(int(ticket_str), close_vol)
                    else:
                        result = "PAPER|partial_tp2"
                    state.active_trades[ticket_str]["tp2_taken"] = True
                    log.info(f"{symbol} TP2 hit — closed 30% ({close_vol} lots) | {result}")

            # Trail to break-even - 1 pip
            if profit_in_r >= 1.0 and current_sl > open_price:
                new_sl = open_price - pip_size
                log.info(f"{symbol} SELL: Moving SL to break-even ({new_sl:.5f})")

            if profit_in_r >= 2.0 and current_sl > open_price - risk:
                new_sl = min(new_sl, open_price - risk)
                log.info(f"{symbol} SELL: Trailing SL to 1R ({new_sl:.5f})")

            if profit_in_r >= 3.0:
                new_sl = min(new_sl, current_price + risk * 0.5)
                log.info(f"{symbol} SELL: Tight trail at 3R ({new_sl:.5f})")

        # Modify only when SL moves by at least 3 pips (prevents micro-update spam)
        min_move = max(risk * 0.05, pip_size * 3)
        if abs(new_sl - current_sl) > min_move:
            result = modify_position(int(ticket_str) if ticket_str.isdigit() else 0, new_sl, new_tp)
            log.info(f"Modified {ticket_str}: SL {current_sl:.5f}→{new_sl:.5f} | {result}")


# ── Scale-In Logic ────────────────────────────────────────────────────────────
def check_scale_in(state: TraderState, data: dict, equity: float, memory=None):
    """
    After TP1 is hit and push momentum is confirmed, add to the winning position.

    Rules (from rules.json scale_in_rules):
    1. Trade must be in profit >= 1R (SL already at BE or better)
    2. TP1 must already be taken (50% of position closed)
    3. Push phase confirmed on M15 or H1 (bodies growing in direction)
    4. No opposing BOS on H1
    5. Max 1 scale-in per trade

    Scale-in position: 50% of original lot size
    Scale-in SL: same as current trailing SL (at BE or better)
    Scale-in TP: 1.5× TP3 distance from scale-in entry
    """
    positions      = get_open_positions(data)
    pos_dict       = {str(p.get("ticket")): p for p in positions}
    charts         = data.get("charts", {})

    for ticket_str, trade in state.active_trades.items():
        # Only scale into trades where TP1 taken + no previous scale-in
        if not trade.get("tp1_taken", False):
            continue
        if trade.get("scaled_in", False):
            continue

        symbol    = trade.get("symbol", "")
        direction = trade.get("direction", "")
        pos       = pos_dict.get(ticket_str)
        if not pos:
            continue

        current_price = pos.get("current_price", 0)
        open_price    = pos.get("open_price", 0)
        current_sl    = pos.get("sl", 0)
        original_vol  = trade.get("volume_original", 0)

        if current_price == 0 or open_price == 0 or current_sl == 0:
            continue

        # Check we're at least 1R in profit
        risk = abs(open_price - current_sl) if current_sl else 0
        if risk == 0:
            continue

        if direction == "BUY":
            profit_r = (current_price - open_price) / risk
        else:
            profit_r = (open_price - current_price) / risk

        if profit_r < 1.0:
            continue

        # Check push momentum on M15 or H1
        sym_data = charts.get(symbol, {})
        import ict_precision as ict

        # Parse bars from current chart data
        h1_bars  = ict._parse_bars(sym_data.get("H1",  []))
        m15_bars = ict._parse_bars(sym_data.get("M15", []))

        push_h1  = ict.detect_push_exhaustion(h1_bars)  if h1_bars  else {"phase": "NEUTRAL"}
        push_m15 = ict.detect_push_exhaustion(m15_bars) if m15_bars else {"phase": "NEUTRAL"}

        # Prefer M15 for scale-in decision
        push = push_m15 if push_m15.get("phase") != "NEUTRAL" else push_h1

        push_confirmed = (
            push.get("phase") == "PUSH"
            and (
                (direction == "BUY"  and push.get("direction") == "UP")
                or
                (direction == "SELL" and push.get("direction") == "DOWN")
            )
        )

        if not push_confirmed:
            log.debug(f"{symbol} scale-in skipped — no push momentum (phase={push.get('phase')})")
            continue

        # Check no opposing BOS on H1
        h4_struct = ict.get_h4_structure(h1_bars) if h1_bars else "RANGING"
        opposing_bos = (
            (direction == "BUY"  and h4_struct == "BOS_BEARISH")
            or
            (direction == "SELL" and h4_struct == "BOS_BULLISH")
        )
        if opposing_bos:
            log.info(f"{symbol} scale-in skipped — opposing BOS detected on H1 ({h4_struct})")
            continue

        # Calculate scale-in parameters
        sym_info_data = sym_data
        scale_vol = round(original_vol * 0.50, 2)
        min_lot   = sym_info_data.get("min_lot", 0.01)
        if scale_vol < min_lot:
            continue

        scale_entry  = current_price
        scale_sl     = current_sl  # Already at BE or better
        tp3_original = trade.get("tp3", 0)

        if tp3_original > 0 and open_price > 0:
            # Scale TP = 1.5× the remaining distance to TP3
            remaining_to_tp3 = abs(tp3_original - current_price)
            if direction == "BUY":
                scale_tp = round(current_price + remaining_to_tp3 * 1.5, 5)
            else:
                scale_tp = round(current_price - remaining_to_tp3 * 1.5, 5)
        else:
            orig_risk = abs(open_price - current_sl) if current_sl else 0
            if direction == "BUY":
                scale_tp = round(current_price + orig_risk * 3, 5)
            else:
                scale_tp = round(current_price - orig_risk * 3, 5)

        log.info(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCALE-IN TRIGGERED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Symbol:     {symbol}  {direction}
Parent:     Ticket {ticket_str} | In profit {profit_r:.1f}R
Push phase: {push.get('signal', 'confirmed')}
Scale lots: {scale_vol} (50% of original {original_vol})
Entry:      {scale_entry:.5g}  (market)
SL:         {scale_sl:.5g}    (already at BE/better)
TP:         {scale_tp:.5g}    (1.5× remaining to TP3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")

        order_type = "BUY" if direction == "BUY" else "SELL"
        comment    = f"OMNI_SCALE_{ticket_str}"
        result     = place_order(symbol, direction, order_type,
                                 scale_entry, scale_sl, scale_tp, scale_vol, comment)
        log.info(f"Scale-in order result: {result}")

        if result.startswith("OK") or result.startswith("PAPER"):
            state.active_trades[ticket_str]["scaled_in"] = True
            parts = result.split("|")
            if result.startswith("OK") and len(parts) > 1 and parts[1].isdigit():
                scale_ticket = parts[1]
            else:
                scale_ticket = f"SCALE_{symbol}_{int(time.time())}"

            state.active_trades[scale_ticket] = {
                "symbol":          symbol,
                "direction":       direction,
                "entry":           scale_entry,
                "tp1":             scale_tp,
                "tp2":             scale_tp,
                "tp3":             scale_tp,
                "volume_original": scale_vol,
                "tp1_taken":       False,
                "tp2_taken":       False,
                "last_profit":     0.0,
                "is_scale_in":     True,
                "parent_ticket":   ticket_str,
            }

            # Log to trade memory
            if memory:
                try:
                    memory.log_entry(
                        symbol=symbol, direction=direction,
                        entry_type="SCALE_IN",
                        entry_price=scale_entry,
                        sl_price=scale_sl,
                        tp1_price=scale_tp, tp2_price=scale_tp, tp3_price=scale_tp,
                        confidence=75,
                        reasons=[
                            f"Scale-in: {profit_r:.1f}R profit on parent trade",
                            f"Push momentum confirmed: {push.get('signal','')}",
                            f"H1 structure: {h4_struct}",
                        ],
                        session=data.get("session", ""),
                        amd_phase=data.get("amd_phase", ""),
                        lot_size=scale_vol,
                        rr_ratio=0,
                        is_scale_in=True,
                        parent_trade_id=ticket_str,
                    )
                except Exception as e:
                    log.warning(f"Memory scale-in log error: {e}")


# ── Setup Executor ────────────────────────────────────────────────────────────
def execute_setup(setup, equity: float, state: TraderState, sym_info: dict,
                  memory=None) -> bool:
    """Execute an ICT setup with proper position sizing and TP level storage."""
    symbol    = setup.symbol
    direction = setup.direction
    entry     = setup.entry_price
    sl        = setup.sl_price
    tp        = setup.tp1_price  # TP1 as the primary order TP (50% target)
    session   = getattr(setup, "session", "")

    # Session-aware confidence threshold — Asia requires higher bar
    effective_min_conf = MIN_CONFIDENCE_ASIA if session == "ASIA" else MIN_CONFIDENCE
    if setup.confidence < effective_min_conf:
        log.debug(f"{symbol}: confidence {setup.confidence} below {session} threshold {effective_min_conf}")
        return False

    # Apply adaptive confidence from trade memory
    if memory:
        try:
            patterns = [r for r in setup.reasons if "Pattern:" in r]
            pattern_names = [p.replace("Pattern:", "").strip().split()[0] for p in patterns]
            adj = memory.get_confidence_adjustment(
                entry_type=setup.entry_type,
                session=session,
                patterns=pattern_names,
                amd_phase=getattr(setup, "amd_phase", ""),
            )
            adj_confidence = setup.confidence + adj
            if adj != 0:
                log.info(f"{symbol}: Memory adj {adj:+d} → effective confidence {adj_confidence}/100")
            # Apply adjusted threshold check
            if adj_confidence < effective_min_conf - 5:
                log.info(f"{symbol}: Memory-adjusted confidence {adj_confidence} too low, skipping")
                return False
        except Exception as e:
            log.warning(f"Memory confidence adj error: {e}")
            adj_confidence = setup.confidence
    else:
        adj_confidence = setup.confidence

    # Skip if already traded this symbol recently
    if symbol in state.recently_traded:
        log.debug(f"{symbol} recently traded, skipping")
        return False

    # Minimum SL distance guard: rejects setups where SL would be eaten by spread
    pip_size    = sym_info.get("point", 0.0001) * 10
    sl_distance = abs(entry - sl)
    if sl_distance < MIN_SL_PIPS * pip_size:
        log.debug(f"{symbol}: SL too tight ({sl_distance:.5g} < {MIN_SL_PIPS} pips), skipping")
        return False

    # ── Spread guard — skip trade if spread is abnormally wide ────────────
    # Spread in the prices array is in points; convert to pips for comparison.
    live_spread_pts = sym_info.get("spread", 0)
    live_spread_pips = live_spread_pts * sym_info.get("point", 0.0001) / pip_size * live_spread_pts
    # Simpler: spread field from OmniExport is already in points (integer ticks)
    # 1 pip = 10 points for most symbols; for XAUUSD 1 pip = 1 point
    point = sym_info.get("point", 0.0001)
    if live_spread_pts > 0:
        if "XAU" in symbol or "GOLD" in symbol:
            max_spread = cfg.MAX_SPREAD_XAUUSD_PIPS
        elif any(idx in symbol for idx in ("US30", "NAS", "DAX", "SPX")):
            max_spread = cfg.MAX_SPREAD_INDEX_PIPS
        else:
            max_spread = cfg.MAX_SPREAD_FOREX_PIPS
        spread_in_pips = live_spread_pts * point / pip_size if pip_size > 0 else live_spread_pts
        if spread_in_pips > max_spread:
            log.info(f"{symbol}: spread {spread_in_pips:.1f} pips > max {max_spread} — skipping (wide spread)")
            return False

    # Calculate lot size with compounding
    lot_size = calculate_lot_size(
        equity=equity,
        risk_pct=state.current_risk_pct,
        entry=entry, sl=sl,
        symbol=symbol, sym_info=sym_info
    )

    if lot_size <= 0:
        log.warning(f"{symbol}: lot size calculation failed")
        return False

    # Determine order type (limit vs market)
    current_price = sym_info.get("bid", entry)
    if direction == "BUY":
        order_type = "BUY_LIMIT" if entry < current_price * 0.999 else "BUY"
    else:
        order_type = "SELL_LIMIT" if entry > current_price * 1.001 else "SELL"

    risk_usd = equity * state.current_risk_pct / 100
    comment  = f"OMNI_{setup.entry_type}_{adj_confidence}"

    # ── Full detailed pre-trade log ────────────────────────────────
    log.info(f"""
╔══════════════════════════════════════════════════════════════════╗
║  ICT SETUP EXECUTING
╠══════════════════════════════════════════════════════════════════╣
║  Symbol:     {symbol:<10}  Direction: {direction}
║  Type:       {setup.entry_type}
║  Confidence: {setup.confidence}/100 raw  |  {adj_confidence}/100 memory-adjusted
║  RR Ratio:   {setup.rr_ratio}:1  (min required: {MIN_RR}:1)
╠══════════════════════════════════════════════════════════════════╣
║  ENTRY LEVELS:
║    Entry:  {entry:.5g}  ({order_type})
║    SL:     {sl:.5g}  ({sl_distance / pip_size:.1f} pips from entry)
║    TP1:    {setup.tp1_price:.5g}  (50% — first exit)
║    TP2:    {setup.tp2_price:.5g}  (30% — trail to 1R)
║    TP3:    {setup.tp3_price:.5g}  (20% — runner, let it run)
║    Inval:  {getattr(setup, 'invalidation', 0):.5g}  (setup is void if price hits this)
╠══════════════════════════════════════════════════════════════════╣
║  POSITION:
║    Lots:   {lot_size}  |  Risk: {state.current_risk_pct:.2f}%  =  ${risk_usd:.2f}
║    Equity: ${equity:,.2f}  |  Win streak: {state.win_streak}  Loss streak: {state.loss_streak}
╠══════════════════════════════════════════════════════════════════╣
║  CONTEXT:
║    Session:   {setup.session}  |  AMD Phase: {setup.amd_phase}
║    D1 Bias:   {setup.tf_bias}
╠══════════════════════════════════════════════════════════════════╣
║  CONFLUENCE REASONS ({len(setup.reasons)} factors):
{chr(10).join('║    ' + str(i+1).rjust(2) + '. ' + r for i, r in enumerate(setup.reasons))}
╚══════════════════════════════════════════════════════════════════╝
""")

    result = place_order(symbol, direction, order_type, entry, sl, tp, lot_size, comment)
    log.info(f"Order result: {result}")

    if result.startswith("OK") or result.startswith("PAPER"):
        parts = result.split("|")
        if result.startswith("OK") and len(parts) > 1 and parts[1].isdigit():
            ticket_key = parts[1]
        else:
            ticket_key = f"PAPER_{symbol}_{int(time.time())}"

        # Store TP levels, metadata, and memory trade_id
        trade_id_mem = None
        if memory:
            try:
                trade_id_mem = memory.log_entry(
                    symbol=symbol,
                    direction=direction,
                    entry_type=setup.entry_type,
                    entry_price=entry,
                    sl_price=sl,
                    tp1_price=setup.tp1_price,
                    tp2_price=setup.tp2_price,
                    tp3_price=setup.tp3_price,
                    confidence=setup.confidence,
                    reasons=setup.reasons,
                    session=setup.session,
                    amd_phase=setup.amd_phase,
                    d1_bias=setup.tf_bias,
                    rr_ratio=setup.rr_ratio,
                    lot_size=lot_size,
                    risk_pct=state.current_risk_pct,
                    risk_usd=risk_usd,
                    invalidation=getattr(setup, "invalidation", 0),
                    adj_confidence=adj_confidence,
                )
            except Exception as e:
                log.warning(f"Memory log_entry error: {e}")

        state.active_trades[ticket_key] = {
            "symbol":          symbol,
            "direction":       direction,
            "entry":           entry,
            "sl":              sl,           # ← stored for accurate R-multiple calculation
            "tp1":             setup.tp1_price,
            "tp2":             setup.tp2_price,
            "tp3":             setup.tp3_price,
            "volume_original": lot_size,
            "tp1_taken":       False,
            "tp2_taken":       False,
            "scaled_in":       False,
            "last_profit":     0.0,
            "memory_trade_id": trade_id_mem,
            "entry_type":      setup.entry_type,
        }

        if symbol not in state.recently_traded:
            state.recently_traded.append(symbol)
            if len(state.recently_traded) > 10:
                state.recently_traded.pop(0)
        return True

    return False


# ── Print Status ──────────────────────────────────────────────────────────────
def print_status(state: TraderState, data: dict, setups: list):
    account   = get_account(data)
    equity    = account.get("equity", 0)
    balance   = account.get("balance", 0)
    profit    = account.get("profit", 0)
    session   = data.get("session", "—")
    amd       = data.get("amd_phase", "—")
    positions = get_open_positions(data)
    win_rate  = (state.winning_trades / state.total_trades * 100) if state.total_trades > 0 else 0
    dd_pct    = state.peak_drawdown
    in_filter = SESSION_FILTER is None or session in SESSION_FILTER

    print(f"""
╔══════════════════════════════════════════════╗
║  OMNI ICT AUTO-TRADER  {'[PAPER MODE]' if PAPER_MODE else '[LIVE]':>16}  ║
╠══════════════════════════════════════════════╣
║  Balance:  ${balance:>10,.2f}  Equity: ${equity:>9,.2f}  ║
║  P&L:      ${profit:>+10,.2f}  Risk:   {state.current_risk_pct:>8.2f}%  ║
║  Session:  {session:<10}  Phase:  {amd:<14}  ║
╠══════════════════════════════════════════════╣
║  Trades:   {state.total_trades:<5} W:{state.winning_trades} L:{state.losing_trades} WR:{win_rate:.0f}%         ║
║  Profit:   ${state.total_profit:>+10,.2f}  Streak: {'W'+str(state.win_streak) if state.win_streak>0 else 'L'+str(state.loss_streak):<7}      ║
║  Open:     {len(positions):<3} positions  MaxDD: {dd_pct:.1f}%            ║
║  Setups:   {len(setups):<3} detected  Filter: {'ACTIVE' if in_filter else 'SESSION WAIT'}     ║
╚══════════════════════════════════════════════╝
""")

    if state.trading_halted:
        print(f"  TRADING HALTED: {state.halt_reason}")

    if setups:
        print("  Top setups:")
        for s in setups[:3]:
            marker = "BUY" if s.direction == "BUY" else "SELL"
            print(f"  [{marker}] {s.symbol:<10} @ {s.entry_price:.5g}  "
                  f"SL:{s.sl_price:.5g}  Conf:{s.confidence}/100  RR:{s.rr_ratio}")
        print()


# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    print(f"""
╔══════════════════════════════════════════════════════════╗
║          OMNI ICT AUTO-TRADER STARTING                   ║
║                                                          ║
║  Mode:     {'PAPER (simulated)' if PAPER_MODE else 'LIVE TRADING'}                              ║
║  Risk:     {BASE_RISK_PCT}% base | {MIN_RISK_PCT}% min | {MAX_RISK_PCT}% max (compounding)     ║
║  Symbols:  {', '.join(TRADE_SYMBOLS[:4])}...              ║
║  Min RR:   {MIN_RR}:1    Min Confidence: {MIN_CONFIDENCE}/100           ║
║  Max Open: {MAX_OPEN_TRADES} trades    Daily Loss Limit: {DAILY_LOSS_LIMIT}%       ║
║  Max DD:   {MAX_DD_FROM_PEAK}% from peak equity                     ║
║  Sessions: {', '.join(SESSION_FILTER) if SESSION_FILTER else 'All'}                              ║
╚══════════════════════════════════════════════════════════╝
""")

    if not PAPER_MODE:
        print("LIVE TRADING ENABLED. You have 10 seconds to cancel (Ctrl+C)...")
        for i in range(10, 0, -1):
            print(f"   Starting in {i}...", end="\r")
            time.sleep(1)
        print()

    # Import ICT precision scanner
    try:
        import ict_precision as ict
        log.info("ICT Precision module loaded")
    except ImportError as e:
        log.error(f"Cannot import ict_precision: {e}")
        log.error("Make sure ict_precision.py is in the same folder")
        sys.exit(1)

    # Import trade memory / AI learning engine
    try:
        from trade_memory import get_memory
        memory = get_memory()
        log.info(f"Trade Memory loaded — {len(memory.trades)} historical trades")
        if memory.trades:
            log.info("\n" + memory.get_performance_report())
    except ImportError as e:
        log.warning(f"trade_memory not available: {e} — continuing without AI memory")
        memory = None

    state = load_state()
    if not state.start_time:
        state.start_time = datetime.now().isoformat()

    log.info(f"Trader started | Paper={PAPER_MODE} | Risk={state.current_risk_pct}%")
    log.info(f"State loaded: {state.total_trades} trades, P&L: ${state.total_profit:.2f}")

    # ── Startup reconciliation: sync active_trades with real MT5 positions ─
    # If the bot was restarted, positions may have closed while it was down.
    # Purge any tracked tickets that are no longer open in MT5.
    _startup_data = load_mt5_data()
    if _startup_data:
        _live_tickets = {
            str(p.get("ticket"))
            for p in _startup_data.get("positions", [])
        }
        _stale = [t for t in list(state.active_trades.keys())
                  if not t.startswith("PAPER_") and t not in _live_tickets]
        if _stale:
            log.warning(
                f"Reconciliation: removing {len(_stale)} stale trades "
                f"no longer open in MT5: {_stale}"
            )
            for t in _stale:
                state.active_trades.pop(t, None)
        _tracked = len(state.active_trades)
        _live    = len(_live_tickets)
        log.info(f"Reconciliation complete: {_tracked} tracked / {_live} live MT5 positions")
        save_state(state)
    else:
        log.warning("Could not load MT5 data at startup — skipping reconciliation")

    scan_count     = 0
    data_fail_count = 0

    while True:
        try:
            scan_count += 1
            state.last_scan = datetime.now().isoformat()

            # ── Kill switch check ──────────────────────────────────────
            if os.path.exists(KILL_SWITCH_PATH):
                if not state.trading_halted:
                    log.warning("KILL SWITCH ACTIVATED — HALT file detected, trading stopped")
                    state.trading_halted = True
                    state.halt_reason = "Manual kill switch (HALT file)"
                    save_state(state)
                time.sleep(30)
                continue

            # ── Load MT5 data ──────────────────────────────────────────
            data = load_mt5_data()
            if not data:
                data_fail_count += 1
                if data_fail_count % 6 == 0:
                    log.error(f"MT5 data missing for {data_fail_count * SCAN_INTERVAL}s — is OmniExport EA running?")
                else:
                    log.warning("No MT5 data — waiting for EA...")
                time.sleep(SCAN_INTERVAL)
                continue

            # ── Staleness guard — abort if data file is too old ────────
            try:
                data_age = time.time() - os.path.getmtime(JSON_PATH)
                if data_age > cfg.MAX_DATA_AGE_SECS:
                    log.error(
                        f"MT5 data stale ({data_age:.0f}s old, max {cfg.MAX_DATA_AGE_SECS}s) — "
                        f"is OmniExport EA running? Trading paused."
                    )
                    time.sleep(SCAN_INTERVAL)
                    continue
            except OSError:
                pass  # File may not exist yet; already handled above

            data_fail_count = 0

            account   = get_account(data)
            equity    = account.get("equity", 0)
            balance   = account.get("balance", 0)
            positions = get_open_positions(data)

            if equity == 0:
                log.warning("Equity is 0 — check MT5 connection")
                time.sleep(SCAN_INTERVAL)
                continue

            # ── Update peak equity ─────────────────────────────────────
            if equity > state.peak_equity:
                state.peak_equity = equity

            # ── Daily reset (exactly once per UTC date) ────────────────
            reset_daily_if_new_day(state, equity)

            # ── Daily loss + drawdown check ────────────────────────────
            if not check_daily_limits(state, equity):
                log.warning(f"Trading halted: {state.halt_reason}")
                print_status(state, data, [])
                save_state(state)
                time.sleep(60)
                continue

            # ── Manage existing trades ─────────────────────────────────
            manage_open_trades(state, data, memory=memory)

            # ── Session filter ─────────────────────────────────────────
            current_session = data.get("session", "")
            session_ok = SESSION_FILTER is None or current_session in SESSION_FILTER

            if not session_ok and scan_count % 6 == 0:
                log.debug(f"Outside trading session ({current_session}) — monitoring only")

            # ── Open trade gate ────────────────────────────────────────
            open_count = len(positions)
            # SESSION_FILTER=None = all sessions allowed; session-specific
            # confidence thresholds are applied inside execute_setup
            can_trade  = (open_count < MAX_OPEN_TRADES
                          and not state.trading_halted
                          and session_ok)

            # ── Scale-in check on winning open trades ──────────────────
            if len(positions) > 0 and not state.trading_halted:
                try:
                    check_scale_in(state, data, equity, memory=memory)
                except Exception as e:
                    log.error(f"Scale-in check error: {e}", exc_info=True)

            # ── Scan for ICT setups ────────────────────────────────────
            all_setups = []
            if can_trade or scan_count % 6 == 0:  # Always scan for display purposes
                try:
                    all_setups = ict.scan_all_primary_symbols()
                    # Filter by minimum RR; confidence check is session-aware inside execute_setup
                    high_conf  = [s for s in all_setups
                                  if s.confidence >= MIN_CONFIDENCE and s.rr_ratio >= MIN_RR]
                except Exception as e:
                    log.error(f"ICT scan error: {e}")
                    high_conf = []

                if scan_count % 6 == 0:
                    print_status(state, data, all_setups)
                    # Print memory report every 30 minutes
                    if memory and scan_count % 180 == 0:
                        log.info("\n" + memory.get_performance_report())

                # ── Execute high-confidence setups ─────────────────────
                if can_trade:
                    for setup in high_conf:
                        if open_count >= MAX_OPEN_TRADES:
                            break
                        if setup.symbol in state.recently_traded:
                            continue

                        charts   = data.get("charts", {})
                        sym_info = charts.get(setup.symbol, {})
                        if not sym_info:
                            continue

                        # Inject current bid price for order type detection
                        prices = {p["symbol"]: p for p in data.get("prices", [])}
                        if setup.symbol in prices:
                            sym_info["bid"] = prices[setup.symbol].get("bid", 0)

                        success = execute_setup(setup, equity, state, sym_info, memory=memory)
                        if success:
                            open_count += 1
                            log.info(f"Order placed: {setup.symbol} {setup.direction} @ {setup.entry_price:.5g}")

            # ── Persist state ──────────────────────────────────────────
            save_state(state)
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("Shutting down...")
            save_state(state)
            win_rate = state.winning_trades / state.total_trades * 100 if state.total_trades > 0 else 0
            print(f"\n  Final P&L:    ${state.total_profit:.2f}")
            print(f"  Trades:       {state.total_trades} | WR: {win_rate:.0f}%  "
                  f"(W:{state.winning_trades} / L:{state.losing_trades})")
            print(f"  Peak equity:  ${state.peak_equity:,.2f}")
            print(f"  Max drawdown: {state.peak_drawdown:.2f}%")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
