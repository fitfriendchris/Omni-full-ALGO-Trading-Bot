"""
config.py — OMNI-ICT Central Configuration
==========================================
All paths, thresholds, and toggles in one place.
Override any value with an environment variable — prefix OMNI_.

Priority: env var > config.json > auto-detect > hardcoded default

Usage:
    from config import cfg
    print(cfg.JSON_PATH)
    print(cfg.PAPER_MODE)
"""

import os
import json
import logging
from pathlib import Path

log = logging.getLogger("OmniConfig")

# ── Project root ──────────────────────────────────────────────────────────────
_HERE      = Path(__file__).parent           # python/
_ROOT      = _HERE.parent                    # omni-ict/
_CFG_FILE  = _ROOT / "config.json"

# ── MT5 file path auto-detection ──────────────────────────────────────────────
def _find_mt5_common_files() -> Path:
    """
    Find the MT5 Common Files directory on macOS (Wine) or Windows.
    Returns the directory Path, or an empty Path if not found.
    """
    candidates = [
        # macOS — MetaTrader 5 via Wine (CrossOver / Whisky / native Wine)
        Path.home() / "Library/Application Support/net.metaquotes.wine.metatrader5"
                     / "drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files",
        # macOS — alternative Wine prefix names
        Path.home() / "Library/Application Support/MetaTrader 5/drive_c/users/user"
                     / "AppData/Roaming/MetaQuotes/Terminal/Common/Files",
        # Windows / Parallels
        Path.home() / "AppData/Roaming/MetaQuotes/Terminal/Common/Files",
        Path("C:/Users") / os.getenv("USERNAME", "user")
                         / "AppData/Roaming/MetaQuotes/Terminal/Common/Files",
    ]
    for c in candidates:
        if c.exists():
            return c
    # Return first candidate as default even if it doesn't exist yet
    return candidates[0]

_MT5_DIR = _find_mt5_common_files()


# ── Load optional config.json ─────────────────────────────────────────────────
def _load_json_config() -> dict:
    if _CFG_FILE.exists():
        try:
            with open(_CFG_FILE) as f:
                data = json.load(f)
            # Support both flat config and account-array config
            if "accounts" in data and data["accounts"]:
                acc = data["accounts"][0]   # Default to first account
                return {
                    "data_path":    acc.get("data_path", ""),
                    "cmd_path":     acc.get("cmd_path", ""),
                    "state_path":   acc.get("state_path", ""),
                    "log_path":     acc.get("log_path", ""),
                    "memory_path":  acc.get("memory_path", ""),
                    "journal_path": acc.get("journal_path", ""),
                }
            return data
        except Exception as e:
            log.warning(f"config.json load error: {e}")
    return {}

_jcfg = _load_json_config()


def _get(env_key: str, json_key: str, default: str) -> str:
    """Resolve a string config value: env var > config.json > default."""
    return os.getenv(env_key) or _jcfg.get(json_key) or default


def _getbool(env_key: str, json_key: str, default: bool) -> bool:
    v = os.getenv(env_key)
    if v is not None:
        return v.lower() in ("1", "true", "yes")
    return _jcfg.get(json_key, default)


def _getfloat(env_key: str, json_key: str, default: float) -> float:
    v = os.getenv(env_key)
    if v is not None:
        try: return float(v)
        except ValueError: pass
    return float(_jcfg.get(json_key, default))


def _getint(env_key: str, json_key: str, default: int) -> int:
    v = os.getenv(env_key)
    if v is not None:
        try: return int(v)
        except ValueError: pass
    return int(_jcfg.get(json_key, default))


# ═════════════════════════════════════════════════════════════════════════════
# Configuration object
# ═════════════════════════════════════════════════════════════════════════════

class _Config:
    # ── Mode ──────────────────────────────────────────────────────────────────
    PAPER_MODE: bool = _getbool("OMNI_PAPER_MODE", "paper_mode", True)
    # ⚠️  Set OMNI_PAPER_MODE=false (or paper_mode: false in config.json) for live

    # ── Risk ──────────────────────────────────────────────────────────────────
    BASE_RISK_PCT:    float = _getfloat("OMNI_BASE_RISK",     "base_risk_pct",    1.0)
    MAX_RISK_PCT:     float = _getfloat("OMNI_MAX_RISK",      "max_risk_pct",     2.0)
    MIN_RISK_PCT:     float = _getfloat("OMNI_MIN_RISK",      "min_risk_pct",     0.5)
    DAILY_LOSS_LIMIT: float = _getfloat("OMNI_DAILY_LOSS",    "daily_loss_pct",   3.0)
    MAX_DD_FROM_PEAK: float = _getfloat("OMNI_MAX_DD",        "max_drawdown_pct", 10.0)
    MIN_RR:           float = _getfloat("OMNI_MIN_RR",        "min_rr",           2.0)
    MIN_CONFIDENCE:   int   = _getint  ("OMNI_MIN_CONF",      "min_confidence",   55)
    MIN_CONFIDENCE_ASIA: int = _getint ("OMNI_MIN_CONF_ASIA", "min_conf_asia",    65)
    MIN_SL_PIPS:      int   = _getint  ("OMNI_MIN_SL_PIPS",   "min_sl_pips",      10)
    MAX_OPEN_TRADES:  int   = _getint  ("OMNI_MAX_TRADES",    "max_open_trades",  3)
    SCAN_INTERVAL:    int   = _getint  ("OMNI_SCAN_INTERVAL", "scan_interval",    10)

    # ── Spread guard (skip trade if live spread > this multiple of normal) ─────
    MAX_SPREAD_XAUUSD_PIPS: float = _getfloat("OMNI_MAX_SPREAD_XAU",  "max_spread_xau",  35.0)
    MAX_SPREAD_FOREX_PIPS:  float = _getfloat("OMNI_MAX_SPREAD_FX",   "max_spread_fx",   4.0)
    MAX_SPREAD_INDEX_PIPS:  float = _getfloat("OMNI_MAX_SPREAD_IDX",  "max_spread_idx",  15.0)

    # ── Data staleness ────────────────────────────────────────────────────────
    MAX_DATA_AGE_SECS: int = _getint("OMNI_MAX_DATA_AGE", "max_data_age_secs", 60)

    # ── Magic number ──────────────────────────────────────────────────────────
    MAGIC: int = _getint("OMNI_MAGIC", "magic_number", 20250411)

    # ── File paths ────────────────────────────────────────────────────────────
    JSON_PATH: str = _get(
        "OMNI_JSON_PATH", "data_path",
        str(_MT5_DIR / "omni_data.json")
    )
    CMD_PATH: str = _get(
        "OMNI_CMD_PATH", "cmd_path",
        str(_MT5_DIR / "omni_cmd.txt")
    )
    RESULT_PATH: str = _get(
        "OMNI_RESULT_PATH", "result_path",
        str(_MT5_DIR / "omni_result.txt")
    )
    STATE_PATH: str = _get(
        "OMNI_STATE_PATH", "state_path",
        str(_HERE / "trader_state.json")
    )
    LOG_PATH: str = _get(
        "OMNI_LOG_PATH", "log_path",
        str(_HERE / "trader.log")
    )
    MEMORY_PATH: str = _get(
        "OMNI_MEMORY_PATH", "memory_path",
        str(_HERE / "trade_memory.json")
    )
    JOURNAL_PATH: str = _get(
        "OMNI_JOURNAL_PATH", "journal_path",
        str(_HERE / "trade_journal.log")
    )
    KILL_SWITCH_PATH: str = _get(
        "OMNI_KILL_SWITCH", "kill_switch_path",
        str(_HERE / "HALT")
    )

    # ── Symbols ───────────────────────────────────────────────────────────────
    TRADE_SYMBOLS: list = (
        os.getenv("OMNI_SYMBOLS", "").split(",")
        if os.getenv("OMNI_SYMBOLS")
        else _jcfg.get("watchlist", ["XAUUSD","XAGUSD","EURUSD","GBPUSD",
                                      "USDJPY","AUDUSD","USDCAD"])
    )

    def validate(self) -> bool:
        """
        Validate configuration on startup.
        Logs warnings for missing paths; returns False if critical config is bad.
        """
        ok = True

        # Check that MT5 data directory exists
        data_dir = Path(self.JSON_PATH).parent
        if not data_dir.exists():
            log.error(
                f"MT5 data directory not found: {data_dir}\n"
                f"  Is MetaTrader 5 installed and OmniExport.mq5 attached?\n"
                f"  Override with: export OMNI_JSON_PATH=/your/path/omni_data.json"
            )
            ok = False
        elif not Path(self.JSON_PATH).exists():
            log.warning(
                f"omni_data.json not found yet: {self.JSON_PATH}\n"
                f"  The bot will wait for the EA to create it."
            )

        cmd_dir = Path(self.CMD_PATH).parent
        if not cmd_dir.exists():
            log.error(f"Command path directory not found: {cmd_dir}")
            ok = False

        if not Path(self.STATE_PATH).parent.exists():
            log.error(f"State file directory not found: {Path(self.STATE_PATH).parent}")
            ok = False

        if self.PAPER_MODE:
            log.info("PAPER MODE enabled — no real orders will be placed")
        else:
            log.warning("LIVE MODE — real orders will be placed on the MT5 account!")

        log.info(f"MT5 data path:  {self.JSON_PATH}")
        log.info(f"Command path:   {self.CMD_PATH}")
        log.info(f"State path:     {self.STATE_PATH}")
        log.info(f"Memory path:    {self.MEMORY_PATH}")
        return ok


cfg = _Config()
