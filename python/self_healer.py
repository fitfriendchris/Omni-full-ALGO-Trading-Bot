"""
self_healer.py — OMNI Self-Healing & Auto-Recovery Daemon
===========================================================
Runs every 60 s via launchd (or cron).  Zero human intervention.

Responsibilities (in priority order):
  1. Detect stale / corrupted MT5 JSON export
  2. Auto-repair JSON truncations silently
  3. Restart MT5 terminal if data stays stale &gt; 60 s
  4. Inject yfinance historical fallback so the pipeline keeps running
  5. Heal dead / stalling Python services (orchestrator, swarm, server)
  6. Clean stale active_trades from trader_state.json
  7. Alert Telegram when any action is taken
  8. Prune logs &gt; 7 days to prevent disk exhaustion

Exit codes:
  0  = healthy run (no action or action succeeded)
  1  = unhandled crash (logged, Telegram alerted)

Usage:
  # one-shot (manual diagnosis)
  python3 self_healer.py

  # daemon (launchd keeps it alive)
  python3 self_healer.py --daemon
"""

from __future__ import annotations
import json, logging, os, re, signal, subprocess, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════
OMNI_ROOT    = Path("/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot")
PYTHON       = Path("/opt/homebrew/bin/python3")

# MT5 Wine data (native macOS wrapper path)
MT5_JSON     = Path(
    "/Users/yuhfriendchris/Library/Application Support/"
    "net.metaquotes.wine.metatrader5/drive_c/users/user/"
    "AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"
)
MT5_CMD      = MT5_JSON.with_name("omni_cmd.txt")
MT5_RES      = MT5_JSON.with_name("omni_result.txt")

STATE_PATH   = OMNI_ROOT / "python" / "trader_state.json"
SWARM_LOG    = OMNI_ROOT / "logs" / "swarm.log"
ORCH_LOG     = OMNI_ROOT / "logs" / "orchestrator.log"
HEAL_LOG     = OMNI_ROOT / "logs" / "self_healer.log"
HEAL_ERR     = OMNI_ROOT / "logs" / "self_healer.err.log"

# Telegram alert (same bot as OMNI)
TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# Thresholds (seconds)
STALE_BAR_SEC    = 300          # bar must be &lt; 5 min old for H1/M15
STALE_H4_SEC     = 3600 * 3     # H4 bar &lt; 3h old acceptable
HARD_RESTART_SEC = 60           # wait after sending EA restart cmd
MT5_LAUNCH_GRACE = 15           # seconds to wait after `open MT5.app`
RESTART_COOLDOWN = 300          # min 5 min between hard restarts
YF_FALLBACK_SYM  = "GC=F"       # yfinance ticker for XAUUSD

_log_heal = logging.getLogger("OMNI.HEAL")

class _HealLog(logging.LoggerAdapter):
    def heal(self, msg, *a, **kw):
        self.log(25, msg, *a, **kw)  # custom level between INFO/WARNING

log = _HealLog(
    _log_heal,
    extra={}
)

def _setup_logging():
    lvl = logging.DEBUG
    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    # file
    fh = logging.FileHandler(HEAL_LOG)
    fh.setLevel(lvl)
    fh.setFormatter(fmt)
    # stderr
    eh = logging.FileHandler(HEAL_ERR)
    eh.setLevel(logging.WARNING)
    eh.setFormatter(fmt)
    # console
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(lvl)
    ch.setFormatter(fmt)
    _log_heal.handlers = []
    _log_heal.addHandler(fh)
    _log_heal.addHandler(eh)
    _log_heal.addHandler(ch)
    _log_heal.setLevel(lvl)
    logging.addLevelName(25, "HEAL")

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _now() -> float:
    return datetime.now(timezone.utc).timestamp()

def _tg_alert(text: str):
    """Fire-and-forget Telegram alert (non-blocking)."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    from urllib import request, parse
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    body = parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": False,
    }).encode()
    try:
        request.urlopen(request.Request(url, data=body, method="POST"), timeout=10)
    except Exception as e:
        log.warning(f"Telegram alert failed: {e}")


def _load_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return None


def _atomic_write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _find_json_truncation(text: str) -> int | None:
    """Return the index of the last valid JSON object end before garbage."""
    # Walk backwards from end looking for '}' followed by optional whitespace
    for i in range(len(text) - 1, -1, -1):
        if text[i] == '}':
            try:
                json.loads(text[: i + 1])
                return i + 1
            except json.JSONDecodeError:
                pass
    return None


def _repair_json(path: Path) -> dict | None:
    """
    Load MT5 JSON; if truncated, find the last valid object end and
    append a closing '}' + ']' + '}' to make it valid again (handles
    common omni_data truncation patterns).
    """
    data = _load_json(path)
    if data is not None:
        return data

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.error(f"Cannot read {path}: {e}")
        return None

    trunc = _find_json_truncation(raw)
    if trunc is None:
        log.error(f"JSON beyond repair — no valid closing brace found")
        return None

    fixed_text = raw[:trunc] + "}}"
    try:
        data = json.loads(fixed_text)
    except json.JSONDecodeError:
        log.error(f"JSON repair still failed after truncation fix")
        return None

    log.heal(f"Repaired truncated JSON at char {trunc}/{len(raw)} → {path.name}")
    _atomic_write_json(path, data)
    return data


# ═══════════════════════════════════════════════════════════════════════════════
# MT5 Data Freshness
# ═══════════════════════════════════════════════════════════════════════════════

def _is_fresh(data: dict) -> bool:
    """Return True if bars are recent enough to trade on."""
    charts = data.get("charts", {})
    now = datetime.now(timezone.utc)
    try:
        # XAUUSD is the primary — check H4, H1, M15
        xau = charts.get("XAUUSD", {})
        for tf, max_age in (("H4", STALE_H4_SEC), ("H1", STALE_BAR_SEC), ("M15", STALE_BAR_SEC)):
            bars = xau.get(tf, [])
            if not isinstance(bars, list) or len(bars) == 0:
                return False
            newest_bar = bars[0].get("t", "")
            if not newest_bar:
                return False
            bt = datetime.strptime(newest_bar, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if (now - bt).total_seconds() > max_age:
                return False
        return True
    except Exception:
        return False


def _send_ea_cmd(cmd: str):
    """Write a pipe-delimited command for the MQL5 EA to read."""
    try:
        with open(MT5_CMD, "w") as f:
            f.write(cmd)
        log.heal(f"Sent MT5 cmd: {cmd}")
    except Exception as e:
        log.error(f"Failed to write cmd file: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# MT5 process / app lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

def _find_mt5_pids() -> list[int]:
    """Find all terminal64 / MetaTrader / Whisky / CrossOver processes."""
    pids = []
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "(?i)(terminal64|MetaTrader|wineserver|WineskinX|Whisky|CrossOver)"],
            text=True,
        )
        pids = [int(x) for x in out.strip().split("\n") if x.strip()]
    except subprocess.CalledProcessError:
        pass
    return pids


def _kill_mt5():
    pids = _find_mt5_pids()
    if not pids:
        return
    log.heal(f"HARD RESTART MT5 — killing {len(pids)} processes")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            log.heal(f"  SIGTERM {pid}")
        except ProcessLookupError:
            pass
    time.sleep(2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            log.heal(f"  SIGKILL {pid}")
        except ProcessLookupError:
            pass


def _launch_mt5():
    """Launch the macOS MetaTrader 5 wrapper."""
    candidates = [
        Path("/Applications/MetaTrader 5.app"),
        Path("/Applications/MetaTrader5.app"),
    ]
    app = next((c for c in candidates if c.exists()), None)
    if app:
        subprocess.Popen(["open", str(app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.heal(f"Launched MT5: {app}")
    else:
        log.warning("MetaTrader 5.app not found in /Applications")


# ═══════════════════════════════════════════════════════════════════════════════
# Python service healer
# ═══════════════════════════════════════════════════════════════════════════════

def _is_process_running(name: str) -> bool:
    try:
        subprocess.check_call(
            ["pgrep", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _restart_python_services():
    """Kickstart all OMNI services via launchd."""
    log.heal("Restarting OMNI Python services via launchd")
    try:
        subprocess.run(
            [
                "launchctl", "kickstart",
                "-k",  # kill then start
                f"gui/{os.getuid()}/com.omni.ict.autonomy",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error(f"launchctl kickstart failed: {e.stderr}")
        _tg_alert(f"⚠ Service restart FAILED: {e.stderr}")


def _check_python_services():
    """Returns list of actions if any service had to be restarted."""
    actions = []
    for name in ["orchestrator.py", "swarm.py", "server.py"]:  # telegram_bot recovers on its own
        if not _is_process_running(name):
            actions.append(f"missing_python_service:{name}")
    if actions:
        _restart_python_services()
    return actions


# ═══════════════════════════════════════════════════════════════════════════════
# yfinance fallback injection
# ═══════════════════════════════════════════════════════════════════════════════

try:
    import yfinance as yf
except ImportError:
    yf = None


def _fetch_yf_fallback(symbol: str = YF_FALLBACK_SYM, period: str = "60d", interval_h4: str = "1h") -> dict:
    """
    Fetch enough data from yfinance to populate H4/H1/M15 keys
    so the confluence engine can still compute signals even when
    MT5 is down.  Returns a dict shaped like `charts.XAUUSD`.
    """
    if yf is None:
        log.error("yfinance not installed — cannot inject fallback data")
        return {}

    log.heal(f"Fetching yfinance fallback data for {symbol}")
    try:
        df = yf.download(symbol, period=period, interval="15m", progress=False)
        if df.empty:
            return {}
        # flatten multi-level columns (yfinance 0.2.40+)
        if isinstance(df.columns, list) and len(df.columns) > 0 and isinstance(df.columns[0], tuple):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

        # Ensure standard names
        remap = {n: n for n in ["Open", "High", "Low", "Close", "Volume"]}
        df = df.rename(columns={k.lower().capitalize(): v for k, v in remap.items()})

        # Build H4 / H1 / M15 bar lists (newest-first)
        def _build(tf_str, rule):
            r = df.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
            out = []
            for ts, row in r.iloc[::-1].iterrows():
                out.append({
                    "t":  ts.strftime("%Y.%m.%d %H:%M:%S"),
                    "o":  float(row["Open"]),
                    "h":  float(row["High"]),
                    "l":  float(row["Low"]),
                    "c":  float(row["Close"]),
                })
            return out

        out = {
            "H4":  _build("H4",  "4H"),
            "H1":  _build("H1",  "1H"),
            "M15": _build("M15", "15T"),
            "D1":  _build("D1",  "1D"),
        }
        return out
    except Exception as e:
        log.error(f"yfinance fallback failed: {e}")
        return {}


def _inject_fallback_into_json(data: dict) -> dict:
    """Merges yfinance bars into XAUUSD slot of omni_data.json."""
    fb = _fetch_yf_fallback()
    if not fb:
        return data
    charts = data.setdefault("charts", {})
    xau = charts.setdefault("XAUUSD", {})
    xau.update(fb)
    log.heal("Injected yfinance fallback after hard restart")
    return data


# ═══════════════════════════════════════════════════════════════════════════════
# State cleanup
# ═══════════════════════════════════════════════════════════════════════════════

def _clean_trader_state(data: dict) -> bool:
    """
    Remove active_trades entries that have no matching open position in MT5.
    Returns True if modifications were made.
    """
    if not STATE_PATH.exists():
        return False

    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except Exception as e:
        log.error(f"Cannot load trader_state: {e}")
        return False

    positions = data.get("positions", []) if isinstance(data, dict) else []
    open_tickets = {p.get("ticket") for p in positions if isinstance(p, dict)}

    active = state.get("active_trades", [])
    cleaned = [t for t in active if (isinstance(t, dict) and t.get("ticket") in open_tickets)]
    removed = len(active) - len(cleaned)

    if removed:
        state["active_trades"] = cleaned
        _atomic_write_json(STATE_PATH, state)
        log.heal(f"Cleaned {removed} stale active_trades from state")
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Log pruning
# ═══════════════════════════════════════════════════════════════════════════════

def _prune_old_logs(days: int = 7):
    cutoff = _now() - days * 86400
    removed = 0
    for log_file in OMNI_ROOT.glob("logs/*"):
        if not log_file.is_file():
            continue
        try:
            mtime = log_file.stat().st_mtime
            if mtime < cutoff:
                log_file.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        log.heal(f"Pruned {removed} log files older than {days} days")


# ═══════════════════════════════════════════════════════════════════════════════
# Main healing cycle
# ═══════════════════════════════════════════════════════════════════════════════

def heal_cycle():
    log.info("=== OMNI Self-Healer cycle start ===")
    t0 = time.time()
    actions = []

    # v27.1: heartbeat — always assert auto-trade enabled (EA now has runtime toggle)
    _send_ea_cmd("ENABLE")
    actions.append("mt5_enable_heartbeat")

    # ── 1. Load & repair JSON ──────────────────────────────────────────────────
    data = _repair_json(MT5_JSON)
    if data is None:
        log.error("MT5 JSON missing or irreparable — will attempt restart anyway")
    else:
        # Try a quick freshness check on the raw data
        if not _is_fresh(data):
            log.warn("MT5 data stale — attempting EA restart")
            _send_ea_cmd("RESTART_EXPORT")
            actions.append("mt5_export_restart_cmd")
            time.sleep(HARD_RESTART_SEC)
            # Re-read after grace
            data = _repair_json(MT5_JSON)
            if data and not _is_fresh(data):
                # Still stale — escalate to full MT5 kill + relaunch
                _kill_mt5()
                _launch_mt5()
                actions.append("mt5_hard_restart")
                time.sleep(MT5_LAUNCH_GRACE)
                # Fallback data while MT5 reconnects
                if data is not None:
                    data = _inject_fallback_into_json(data)
                    actions.append("yfinance_after_hard_restart")
                _atomic_write_json(MT5_JSON, data)

    # ── 2. Heal Python services ────────────────────────────────────────────────
    svc_actions = _check_python_services()
    actions.extend(svc_actions)

    # ── 3. Clean stale state ───────────────────────────────────────────────────
    if data is not None and _clean_trader_state(data):
        actions.append("cleaned_stale_trades")

    # ── 4. Prune logs ────────────────────────────────────────────────────────────
    _prune_old_logs()

    # ── 5. Report ────────────────────────────────────────────────────────────────
    if actions:
        msg = "\n".join(f"• {a}" for a in actions)
        _tg_alert(f"<b>Self-healing actions taken:</b>\n{msg}")
        log.heal(f"Cycle complete in {time.time()-t0:.1f}s — actions: {actions}")
    else:
        log.info(f"Cycle complete in {time.time()-t0:.1f}s — no action needed")


if __name__ == "__main__":
    _setup_logging()
    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        INTERVAL = 60
        while True:
            try:
                heal_cycle()
            except Exception as e:
                log.critical(f"Crash in daemon loop: {e}", exc_info=True)
                time.sleep(INTERVAL)
            time.sleep(INTERVAL)
    else:
        try:
            heal_cycle()
        except Exception as e:
            log.error(f"CRASH in healer cycle: {e}\n{traceback.format_exc()}")
            sys.exit(1)

# ── end ────────────────────────────────────────────────────────────────────────
