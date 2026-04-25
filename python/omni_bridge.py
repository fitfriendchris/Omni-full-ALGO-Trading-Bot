"""
omni_bridge.py — OMNI-ICT License Server Bridge
================================================
Replaces telegram_bot.py for distributed clients.

Instead of each client running their own Telegram bot, this process:
  - Polls the central license server for commands sent via @OmniAutoTraderICTbot
  - Executes commands by reading/writing local MT5 state files
  - Posts trade alerts and responses back to the server
  - The community bot then relays everything to the client's Telegram chat

No Telegram bot token needed. Just OMNI_LICENSE_KEY in .env.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

log = logging.getLogger("omni_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HERE         = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
LOG_DIR      = PROJECT_ROOT / "logs"
CONFIG_PATH  = PROJECT_ROOT / "config.json"

LICENSE_KEY    = os.getenv("OMNI_LICENSE_KEY", "")
LICENSE_SERVER = os.getenv("OMNI_LICENSE_SERVER",
                           "https://omni-full-algo-trading-bot-production.up.railway.app")
POLL_INTERVAL  = 5   # seconds between command polls

ACTIVE_ACC_FILE = HERE / "active_account.txt"
HALT_PATH       = HERE / "HALT"
WATCHDOG_STATE  = LOG_DIR / "watchdog_state.json"


# ── HTTP helpers ──────────────────────────────────────────────

def _http(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(
        url, data=data,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": "omni-bridge/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"error": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def notify(message: str) -> None:
    """Post an alert or response to the license server for relay to Telegram."""
    if not LICENSE_KEY or LICENSE_KEY == "OWNER_BYPASS":
        return
    _http("POST", f"{LICENSE_SERVER}/client/notify",
          {"key": LICENSE_KEY, "message": message})


def poll_commands() -> list[dict]:
    """Fetch pending commands from the license server."""
    if not LICENSE_KEY or LICENSE_KEY == "OWNER_BYPASS":
        return []
    result = _http("GET", f"{LICENSE_SERVER}/client/poll/{LICENSE_KEY}")
    return result.get("commands", [])


def respond(result: str) -> None:
    """Post a command result back to be relayed to the user."""
    if not LICENSE_KEY or LICENSE_KEY == "OWNER_BYPASS":
        return
    _http("POST", f"{LICENSE_SERVER}/client/respond",
          {"key": LICENSE_KEY, "result": result})


# ── State readers (same as telegram_bot.py) ───────────────────

def _load_active_account() -> str:
    try:
        return ACTIVE_ACC_FILE.read_text().strip()
    except Exception:
        return "demo"


def _mt5_data_path(acc_id: str) -> Path | None:
    for p in [
        PROJECT_ROOT / "mt5" / f"omni_data_{acc_id}.json",
        PROJECT_ROOT / "mt5" / "omni_data.json",
        Path(os.getenv("OMNI_MT5_DATA_PATH", "")) if os.getenv("OMNI_MT5_DATA_PATH") else None,
    ]:
        if p and p.exists():
            return p
    return None


def _state_path(acc_id: str) -> Path | None:
    for p in [
        LOG_DIR / f"trader_state_{acc_id}.json",
        LOG_DIR / "trader_state_demo.json",
        LOG_DIR / "trader_state.json",
    ]:
        if p and p.exists():
            return p
    return None


def _read_json(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _live_equity(acc_id: str) -> tuple[float, float, float, float, str]:
    mt5 = _read_json(_mt5_data_path(acc_id))
    balance = float(mt5.get("balance") or 0)
    equity  = float(mt5.get("equity") or balance)
    margin  = float(mt5.get("free_margin") or 0)
    profit  = float(mt5.get("profit") or 0)
    cur     = mt5.get("currency", "USD")
    return balance, equity, margin, profit, cur


# ── Command handlers ──────────────────────────────────────────

def cmd_equity() -> str:
    acc = _load_active_account()
    balance, equity, margin, profit, cur = _live_equity(acc)
    if not balance:
        return "⚠️ MT5 data not available. Is the EA running on your chart?"
    dd = ((balance - equity) / balance * 100) if balance else 0
    return (
        f"💰 <b>Equity — {acc}</b>\n\n"
        f"Balance:  {balance:,.2f} {cur}\n"
        f"Equity:   {equity:,.2f} {cur}\n"
        f"Free Margin: {margin:,.2f} {cur}\n"
        f"Open P&L: {profit:+,.2f} {cur}\n"
        f"Drawdown: {dd:.1f}%"
    )


def cmd_pnl() -> str:
    acc   = _load_active_account()
    balance, equity, margin, profit, cur = _live_equity(acc)
    state = _read_json(_state_path(acc))
    day_start = float(state.get("day_start_equity") or balance or 0)
    day_pnl   = equity - day_start if day_start else profit
    wins      = state.get("daily_wins", 0)
    losses    = state.get("daily_losses", 0)
    ws        = state.get("win_streak", 0)
    ls        = state.get("loss_streak", 0)
    return (
        f"📊 <b>P&L — {acc}</b>\n\n"
        f"Day start: {day_start:,.2f} {cur}\n"
        f"Current:   {equity:,.2f} {cur}\n"
        f"Day P&L:   {day_pnl:+,.2f} {cur}\n"
        f"Open P&L:  {profit:+,.2f} {cur}\n\n"
        f"Trades: {wins}W / {losses}L\n"
        f"Streak: {'🔥 ' + str(ws) + ' wins' if ws > 1 else ('❄️ ' + str(ls) + ' losses' if ls > 1 else 'neutral')}"
    )


def cmd_trades() -> str:
    acc   = _load_active_account()
    state = _read_json(_state_path(acc))
    trades = state.get("active_trades", {})
    if not trades:
        return "📭 No open positions."
    lines = [f"📈 <b>Open positions — {acc}</b>\n"]
    for ticket, t in trades.items():
        sym  = t.get("symbol", "?")
        dire = t.get("direction", "?")
        lots = t.get("lot_size", 0)
        entr = t.get("entry_price", 0)
        sl   = t.get("stop_loss", 0)
        tp   = t.get("take_profit", 0)
        pnl  = t.get("unrealized_pnl", 0)
        lines.append(
            f"#{ticket} {sym} {dire} {lots}L\n"
            f"  Entry: {entr}  SL: {sl}  TP: {tp}\n"
            f"  P&L: {pnl:+.2f}"
        )
    return "\n".join(lines)


def cmd_status() -> str:
    ws = _read_json(WATCHDOG_STATE)
    svcs = ws.get("services", {})
    if not svcs:
        return "⚠️ Watchdog state not available."
    lines = ["🔧 <b>Service Status</b>\n"]
    for name, info in svcs.items():
        alive = info.get("alive", False)
        icon  = "🟢" if alive else "🔴"
        rs    = info.get("restarts", 0)
        lines.append(f"{icon} {name}  restarts={rs}")
    halted = HALT_PATH.exists()
    lines.append(f"\n{'🛑 HALT active' if halted else '✅ Trading active'}")
    return "\n".join(lines)


def cmd_halt() -> str:
    HALT_PATH.touch()
    return "🛑 <b>HALT activated.</b> No new entries will be opened."


def cmd_resume() -> str:
    HALT_PATH.unlink(missing_ok=True)
    return "✅ <b>HALT lifted.</b> Bot will resume trading on next signal."


def cmd_signals() -> str:
    sig_file = LOG_DIR / "signals.json"
    sigs = _read_json(sig_file) if sig_file.exists() else {}
    if not sigs:
        return "📭 No recent signals."
    lines = ["📡 <b>Latest ICT Signals</b>\n"]
    items = list(sigs.items())[-5:]
    for sid, s in items:
        sym  = s.get("symbol", "?")
        dire = s.get("direction", "?")
        conf = float(s.get("confidence", 0)) * 100
        ts   = datetime.fromtimestamp(s.get("ts", 0), tz=timezone.utc).strftime("%H:%M")
        lines.append(f"• {sym} {dire}  conf={conf:.0f}%  @{ts}")
    return "\n".join(lines)


def cmd_performance() -> str:
    acc   = _load_active_account()
    state = _read_json(_state_path(acc))
    wins  = state.get("total_wins", 0)
    total = wins + state.get("total_losses", 0)
    wr    = (wins / total * 100) if total else 0
    avg_r = state.get("avg_r_multiple", 0)
    exp   = state.get("expectancy", 0)
    return (
        f"🏆 <b>Performance — {acc}</b>\n\n"
        f"Total trades: {total}\n"
        f"Win rate: {wr:.1f}%\n"
        f"Avg R: {avg_r:.2f}\n"
        f"Expectancy: {exp:.3f}R\n"
        f"Win streak: {state.get('win_streak', 0)}\n"
        f"Loss streak: {state.get('loss_streak', 0)}"
    )


def _dispatch(text: str) -> str:
    cmd = text.lower().lstrip("/").split("@")[0].split()[0]
    parts = text.split()

    if cmd in ("pnl",):         return cmd_pnl()
    if cmd in ("equity",):      return cmd_equity()
    if cmd in ("trades",):      return cmd_trades()
    if cmd in ("status",):      return cmd_status()
    if cmd in ("signals",):     return cmd_signals()
    if cmd in ("performance",): return cmd_performance()
    if cmd in ("halt",):        return cmd_halt()
    if cmd in ("resume",):      return cmd_resume()
    if cmd in ("help", "menu"): return (
        "📋 <b>Commands</b>\n\n"
        "/pnl /equity /trades /signals /performance /status\n"
        "/halt /resume\n"
        "/setrisk &lt;pct&gt; — e.g. /setrisk 1.5"
    )

    if cmd == "setrisk" and len(parts) >= 2:
        try:
            val = float(parts[1])
            env_path = PROJECT_ROOT / ".env"
            lines = env_path.read_text().splitlines() if env_path.exists() else []
            lines = [l for l in lines if not l.startswith("OMNI_BASE_RISK=")]
            lines.append(f"OMNI_BASE_RISK={val}")
            env_path.write_text("\n".join(lines) + "\n")
            return f"✅ Base risk set to {val}%. Restart auto_trader to apply."
        except ValueError:
            return "Usage: /setrisk 1.5"

    return f"Unknown command: {cmd}\nSend /help for the list."


# ── Main loop ─────────────────────────────────────────────────

def run() -> None:
    if not LICENSE_KEY:
        log.error("OMNI_LICENSE_KEY not set — bridge cannot start")
        raise SystemExit(1)
    if LICENSE_KEY == "OWNER_BYPASS":
        log.info("OWNER_BYPASS — bridge is a no-op (using local telegram_bot instead)")
        return

    log.info("OMNI bridge starting (key=%s...)", LICENSE_KEY[:8])
    notify("🟢 <b>OMNI-ICT bot online.</b> Use /status, /pnl, /equity to check in.")

    while True:
        try:
            cmds = poll_commands()
            for cmd in cmds:
                text = cmd.get("text", "").strip()
                log.info("Command received: %r", text)
                try:
                    result = _dispatch(text)
                except Exception as e:
                    result = f"❌ Error: {e}"
                respond(result)
        except Exception as e:
            log.warning("Bridge poll error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
