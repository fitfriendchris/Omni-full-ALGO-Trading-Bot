import json, re, os, glob, time, logging, pandas as pd
from pathlib import Path
from datetime import datetime, timezone

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

# ── Symbol alias fallbacks ────────────────────────────────────────────────────
# Different MT5 brokers expose the same instrument under slightly different
# symbol names. When the canonical name (e.g. "XAUUSD") is missing or stale in
# omni_data.json we transparently fall back to common variants. This keeps the
# rest of the system using a stable canonical name regardless of broker.
SYMBOL_ALIASES: dict[str, list[str]] = {
    "XAUUSD": ["XAUUSD", "XAUUSD.spot", "XAUUSDpro", "XAUUSD.r", "XAUUSD#",
               "GOLD", "GOLD.spot", "Gold"],
    "XAGUSD": ["XAGUSD", "XAGUSD.spot", "XAGUSDpro", "XAGUSD.r", "XAGUSD#",
               "SILVER", "SILVER.spot", "Silver"],
    "USDCAD": ["USDCAD", "USDCAD.r", "USDCAD.pro", "USDCAD#", "USDCADm"],
    "USDJPY": ["USDJPY", "USDJPY.r", "USDJPY.pro", "USDJPY#", "USDJPYm"],
    "EURUSD": ["EURUSD", "EURUSD.r", "EURUSD.pro", "EURUSD#", "EURUSDm"],
    "GBPUSD": ["GBPUSD", "GBPUSD.r", "GBPUSD.pro", "GBPUSD#", "GBPUSDm"],
    "AUDUSD": ["AUDUSD", "AUDUSD.r", "AUDUSD.pro", "AUDUSD#", "AUDUSDm"],
    "GBPJPY": ["GBPJPY", "GBPJPY.r", "GBPJPY.pro", "GBPJPY#", "GBPJPYm"],
}

# Resolved alias cache (canonical → broker-actual) so we only scan once per symbol
_RESOLVED_SYMBOL: dict[str, str] = {}

# Broker server-time offset cache (seconds to add to a bar timestamp parsed-as-UTC
# to convert it back to actual UTC). Many MT5 brokers run on UTC+2/+3, which
# means parse-as-UTC produces timestamps 2-3h in the future. We detect this
# once per load by comparing data['timestamp'] (broker server time) to
# data['gmt_time'] (broker's view of UTC) and cache the result.
_BROKER_OFFSET_S: float = 0.0
_BROKER_OFFSET_DETECTED: bool = False


def _detect_broker_offset(data: dict) -> float:
    """
    Compute seconds to subtract from broker-server bar timestamps to get UTC.

    Strategy (in order — first that succeeds wins):
      1. NEWEST BAR vs WALL-CLOCK NOW  — primary, authoritative.
         Take the newest M5 bar across a few major pairs. The current open
         M5 bar is at most 5 minutes old in real time — its timestamp is
         broker-local. (broker_local_timestamp_parsed_as_UTC) − now ≈
         broker_offset. Snap to 15 min (handles odd brokers like UTC+5:30).
      2. data['timestamp'] − data['gmt_time']  — sanity check / fallback.
         Some MT5 setups (especially Wine on macOS) corrupt TimeGMT() so
         this path can disagree with bars by ±1h. Treat as fallback only.
    Returns 0.0 if neither path works (assume timestamps already UTC).
    """
    fmts = ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M")
    def _parse(s):
        if not s or not isinstance(s, str):
            return None
        for f in fmts:
            try:
                return datetime.strptime(s, f).replace(tzinfo=None)
            except ValueError:
                continue
        return None

    bar_offset = None
    charts = data.get("charts", {}) or {}
    # IMPORTANT: use ONLY H1 timeframe for offset detection.
    # In testing, some EA setups write M5 with stale/buggy timestamps
    # (could be hundreds of hours behind H1 even when the file itself is
    # freshly written). H1 has been reliably correct across all observed
    # brokers — the diag uses H1 too and the answers always match reality.
    # Snap to H1 boundary (3600s) since H1 has 1h granularity by definition.
    samples = []
    now_s = time.time()
    for sym in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "AUDUSD"):
        s = charts.get(sym, {})
        bars = s.get("H1") or []
        if not bars:
            continue
        newest_dt = None
        for b in bars:
            if not isinstance(b, dict):
                continue
            t = b.get("t")
            if not isinstance(t, str):
                continue
            dt = _parse(t)
            if dt is None:
                continue
            if newest_dt is None or dt > newest_dt:
                newest_dt = dt
        if newest_dt is None:
            continue
        delta = newest_dt.replace(tzinfo=timezone.utc).timestamp() - now_s
        samples.append(delta)
    if samples:
        samples.sort()
        median = samples[len(samples) // 2]
        # Snap to 1-hour boundary because H1 bars are aligned to the hour.
        # The +/-30min "natural lag" between bar-open and now gets absorbed
        # into the snap. Brokers with non-integer-hour offsets (UTC+5:30,
        # UTC+9:30) are rare on MT5 — almost everyone is on UTC+0/+2/+3.
        bar_offset = round(median / 3600.0) * 3600.0

    # Header sanity check
    srv = _parse(data.get("timestamp"))
    gmt = _parse(data.get("gmt_time"))
    header_offset = None
    if srv and gmt:
        delta = (srv - gmt).total_seconds()
        header_offset = round(delta / 900.0) * 900.0

    if bar_offset is not None:
        if header_offset is not None and abs(bar_offset - header_offset) >= 1800:
            log.warning("broker offset disagreement — bars say %+.2fh, header says %+.2fh; "
                        "trusting bars",
                        bar_offset/3600, header_offset/3600)
        return bar_offset
    if header_offset is not None:
        return header_offset
    return 0.0


def get_broker_offset_s() -> float:
    """Public accessor used by orchestrator.MT5BarFetcher to apply same offset."""
    return _BROKER_OFFSET_S


def _resolve_symbol(charts: dict, canonical: str) -> str:
    """Find which key in `charts` holds bars for `canonical`, with caching."""
    cached = _RESOLVED_SYMBOL.get(canonical)
    if cached and cached in charts:
        return cached
    # Try canonical first, then aliases, then any broker key starting with the canonical
    candidates = SYMBOL_ALIASES.get(canonical, [canonical])
    if canonical not in candidates:
        candidates = [canonical] + list(candidates)
    for cand in candidates:
        if cand in charts:
            _RESOLVED_SYMBOL[canonical] = cand
            if cand != canonical:
                log.info("symbol alias: %s → %s", canonical, cand)
            return cand
    # Last-ditch: prefix match (handles unknown broker suffixes like "XAUUSD-ECN")
    for key in charts.keys():
        if key.startswith(canonical) and key != canonical:
            _RESOLVED_SYMBOL[canonical] = key
            log.info("symbol prefix-match: %s → %s", canonical, key)
            return key
    return canonical  # fall through; caller will see empty bars


def _load() -> dict:
    """Load MT5 JSON with trailing-comma repair, file-stability check, and robust retry."""
    global _BROKER_OFFSET_S, _BROKER_OFFSET_DETECTED, _LAST_GOOD
    JSON_PATH_OBJ = Path(JSON_PATH)
    # Wait for file to stabilize (MT5 writing in progress = size changing)
    for _ in range(5):
        s1 = JSON_PATH_OBJ.stat().st_size
        time.sleep(0.05)
        s2 = JSON_PATH_OBJ.stat().st_size
        if s1 == s2 and s1 > 0:
            break

    raw = ""
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        # Fix trailing commas before ] or }
        raw = re.sub(r',\s*([\]}])', r'\1', raw)
        data = json.loads(raw)
        if data:
            _LAST_GOOD = data  # cache healthy snapshot
            if not _BROKER_OFFSET_DETECTED:
                _BROKER_OFFSET_S = _detect_broker_offset(data)
                _BROKER_OFFSET_DETECTED = True
                if _BROKER_OFFSET_S != 0.0:
                    hrs = _BROKER_OFFSET_S / 3600.0
                    log.info("broker server-time offset detected: %+.1fh (%+ds) — "
                             "bar timestamps will be normalised to UTC",
                             hrs, int(_BROKER_OFFSET_S))
        return data
    except json.JSONDecodeError as e:
        # Try to repair: truncate at last complete top-level object (depth=0)
        log.warning("mt5_connector JSON decode error (mid-write race): %s", e)
        if raw:
            try:
                last_complete = -1
                depth = 0
                in_string = False
                for i, ch in enumerate(raw):
                    if ch == '"' and (i == 0 or raw[i-1] != '\\'):
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            last_complete = i
                if last_complete > 0:
                    repaired = raw[:last_complete+1]
                    # Count unclosed brackets/braces
                    ob = repaired.count('{') - repaired.count('}')
                    bb = repaired.count('[') - repaired.count(']')
                    repaired += '}' * max(0, ob) + ']' * max(0, bb)
                    data = json.loads(repaired)
                    if data:
                        _LAST_GOOD = data
                        log.info("mt5_connector JSON repaired successfully (truncated at char %d)", last_complete)
                        return data
            except Exception as repair_err:
                log.warning("mt5_connector JSON repair failed: %s", repair_err)
        # Fallback: return last-good snapshot
        if _LAST_GOOD:
            log.info("mt5_connector using _LAST_GOOD snapshot (age unknown)")
            return _LAST_GOOD
        log.error("mt5_connector _load failed and no _LAST_GOOD available")
        return {}
    except Exception as e:
        log.warning("mt5_connector _load error: %s", e)
        if _LAST_GOOD:
            return _LAST_GOOD
        return {}


def _bar_time_str(bar: dict) -> str:
    """Parse bar timestamp string to Unix epoch."""
    ts = bar.get("time", bar.get("closeTime", ""))
    if isinstance(ts, (int, float)):
        return float(ts)
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return float(datetime.strptime(ts, fmt).timestamp())
        except Exception:
            continue
    return 0.0


def load_with_retry(max_attempts: int = 5) -> dict:
    """Load MT5 data with exponential-backoff retries; falls back to last-known-good cache."""
    global _LAST_GOOD, _BROKER_OFFSET_S, _BROKER_OFFSET_DETECTED
    for attempt in range(max_attempts):
        try:
            data = _load()
            if data:
                _LAST_GOOD = data.copy()
                if not _BROKER_OFFSET_DETECTED:
                    _BROKER_OFFSET_S = _detect_broker_offset(data)
                    _BROKER_OFFSET_DETECTED = True
                    if _BROKER_OFFSET_S != 0.0:
                        hrs = _BROKER_OFFSET_S / 3600.0
                        log.info("broker server-time offset detected: %+.1fh "
                                 "(%+ds) — bar timestamps will be normalised to UTC",
                                 hrs, int(_BROKER_OFFSET_S))
                return data
            else:
                log.warning("MT5 load returned empty data (attempt %d/%d)", attempt + 1, max_attempts)
        except Exception as exc:
            log.warning("MT5 load exception (attempt %d/%d): %s", attempt + 1, max_attempts, exc)
        delay = 2 ** attempt
        log.warning("MT5 load attempt %d/%d failed; retrying in %ds", attempt + 1, max_attempts, delay)
        time.sleep(delay)
    log.error("All MT5 load retries exhausted; using last-known-good cache")
    return _LAST_GOOD.copy() if _LAST_GOOD else {}


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
    # ── Use retry to survive EA write-in-progress corruption ──
    data = load_with_retry(max_attempts=3)
    charts = data.get("charts", {})
    resolved = _resolve_symbol(charts, symbol)
    sym_data = charts.get(resolved, {})
    bars_raw = sym_data.get(timeframe, [])
    if not bars_raw:
        return []
    offset = _BROKER_OFFSET_S  # broker-server seconds ahead of UTC

    # Detect ordering: compare first vs last timestamp. The OmniExport_v4 EA
    # writes newest-first (despite a comment claiming oldest-first), and we
    # observed historic timestamps go back ~10 months in the tail. Always
    # taking the tail would feed the orchestrator OLD bars and trigger a
    # spurious "stale" verdict. Detect ordering and slice from the correct
    # end so callers always receive the most-recent N bars.
    def _ts(b):
        if not isinstance(b, dict):
            return None
        t = b.get("t")
        if isinstance(t, (int, float)):
            return float(t)
        if isinstance(t, str):
            for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M"):
                try:
                    return datetime.strptime(t, fmt).replace(
                        tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
        return None

    first_ts = _ts(bars_raw[0])
    last_ts  = _ts(bars_raw[-1])
    if first_ts is not None and last_ts is not None and first_ts > last_ts:
        # Newest-first ordering — take the head (most-recent N) and reverse
        # so we hand the caller bars in oldest→newest order (the convention
        # most ICT/SMC code expects).
        slice_view = list(reversed(bars_raw[:n]))
    else:
        # Oldest-first ordering — take the tail
        slice_view = bars_raw[-n:]

    result = []
    for b in slice_view:
        o = float(b.get("o", 0))
        h = float(b.get("h", 0))
        l = float(b.get("l", 0))
        c = float(b.get("c", 0))
        # Reject malformed bars (bad OHLC relationships or zero prices)
        if o <= 0 or h <= 0 or l <= 0 or c <= 0 or h < l:
            log.debug("Skipping malformed bar: o=%s h=%s l=%s c=%s", o, h, l, c)
            continue
        t_raw = b.get("t", "")
        # If consumer wants normalised UTC seconds, apply offset; otherwise
        # leave the original string for backward compat with code paths that
        # parse it themselves.
        result.append({
            "time":      t_raw,        # original string (for legacy callers)
            "time_utc":  _bar_t_to_utc_seconds(t_raw, offset),  # canonical UTC seconds
            "open":      o,
            "high":      h,
            "low":       l,
            "close":     c,
            "volume":    int(b.get("v", 0)),
        })
    return result


def _bar_t_to_utc_seconds(t_raw, offset_s: float) -> float:
    """Parse a bar 't' field (string or number) and normalise to UTC seconds."""
    if isinstance(t_raw, (int, float)):
        # Already an epoch — assume already UTC unless caller knows otherwise
        return float(t_raw)
    if not isinstance(t_raw, str) or not t_raw:
        return 0.0
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M"):
        try:
            dt = datetime.strptime(t_raw, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp() - offset_s
        except ValueError:
            continue
    return 0.0

def get_trade_history(days=30):
    data = _load().get("history", [])
    if not data: return pd.DataFrame()
    df = pd.DataFrame(data)
    df["time"] = pd.to_datetime(df["time"])
    df = df[df["time"] >= pd.Timestamp.now() - pd.Timedelta(days=days)].sort_values("time")
    df["cumulative_profit"] = df["profit"].cumsum()
    return df
