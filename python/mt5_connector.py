import json, re, os, glob, time, logging, pandas as pd
from datetime import datetime

log = logging.getLogger("mt5_connector")

try:
    from config import cfg
    JSON_PATH = cfg.JSON_PATH
except ImportError:
    from pathlib import Path as _Path
    JSON_PATH = str(
        _Path.home() / "Library/Application Support"
        / "net.metaquotes.wine.metatrader5/drive_c/users/user"
        / "AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"
    )

# Last-known-good cache used when all retries exhaust
_LAST_GOOD: dict = {}


def _load() -> dict:
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        # Fix trailing commas before ] or }
        raw = re.sub(r',\s*([\]}])', r'\1', raw)
        return json.loads(raw)
    except Exception as e:
        log.warning("mt5_connector _load error: %s", e)
        return {}


def load_with_retry(max_attempts: int = 5) -> dict:
    """Load MT5 data with exponential-backoff retries; falls back to last-known-good cache."""
    global _LAST_GOOD
    for attempt in range(max_attempts):
        try:
            data = _load()
            if data:
                _LAST_GOOD = data.copy()
                return data
        except Exception:
            pass
        delay = 2 ** attempt
        log.warning("MT5 load attempt %d/%d failed; retrying in %ds", attempt + 1, max_attempts, delay)
        time.sleep(delay)
    log.error("All MT5 load retries exhausted; using last-known-good cache")
    return _LAST_GOOD.copy()


def get_json_path(): return JSON_PATH
def get_account_info(): return _load().get("account", {})
def get_symbol_prices(symbols):
    prices = _load().get("prices", [])
    return [p for p in prices if p["symbol"] in set(symbols)]
def get_last_update(): return _load().get("timestamp", "—")


def is_connected(threshold_sec: int = 30) -> bool:
    if not os.path.exists(JSON_PATH):
        log.warning("MT5 data file missing: %s", JSON_PATH)
        return False
    try:
        age = time.time() - os.path.getmtime(JSON_PATH)
        return age < threshold_sec
    except Exception as e:
        log.error("Connection check error: %s", e)
        return False

def get_open_positions():
    data = _load().get("positions", [])
    if not data:
        return pd.DataFrame(columns=["ticket","symbol","type","volume","open_price","current_price","sl","tp","profit","swap","time"])
    return pd.DataFrame(data)

def get_bars(symbol: str, timeframe: str, n: int = 200) -> list:
    """
    Return up to n recent OHLCV bars for symbol/timeframe from omni_data.json.
    Returns list of dicts with keys: time, open, high, low, close, volume.
    Timeframe strings: 'M1','M5','M15','H1','H4','D1','W1'
    """
    data = _load()
    charts = data.get("charts", {})
    sym_data = charts.get(symbol, {})
    bars_raw = sym_data.get(timeframe, [])
    if not bars_raw:
        return []
    result = []
    for b in bars_raw[-n:]:
        o = float(b.get("o", 0))
        h = float(b.get("h", 0))
        l = float(b.get("l", 0))
        c = float(b.get("c", 0))
        # Reject malformed bars (bad OHLC relationships or zero prices)
        if o <= 0 or h <= 0 or l <= 0 or c <= 0 or h < l:
            log.debug("Skipping malformed bar: o=%s h=%s l=%s c=%s", o, h, l, c)
            continue
        result.append({
            "time":   b.get("t", ""),
            "open":   o,
            "high":   h,
            "low":    l,
            "close":  c,
            "volume": int(b.get("v", 0)),
        })
    return result

def get_trade_history(days=30):
    data = _load().get("history", [])
    if not data: return pd.DataFrame()
    df = pd.DataFrame(data)
    df["time"] = pd.to_datetime(df["time"])
    df = df[df["time"] >= pd.Timestamp.now() - pd.Timedelta(days=days)].sort_values("time")
    df["cumulative_profit"] = df["profit"].cumsum()
    return df
