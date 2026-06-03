"""
protocol_evaluator.py — OMNI ICT Protocol v27.0
Definitive source of truth for all entry, risk, partial-close,
helmet-trade and trail decisions.

Mapping to user protocol:
  §1  Session gating           -> _session_gate()
  §2  Structure/micro-sweep    -> _sweep_gate()
  §3  Manipulation/chop        -> _chop_gate()
  §4  Flag patterns            -> _flag_gate()
  §5  Risk sizing / SL / TP    -> _risk_params()
  §6  5-min model / cooldown   -> _aggression_gate()
  §7  Cumulativity/ formation  -> _formation_gate()
  §8  FVG 1-7 / Range / AMD    -> _entry_model_gate()
  §9  Partial close TP1/2/3     -> _partial_plan()
  §10 Confidence score          -> _confidence_score()
  §11 Friday protocol           -> _friday_gate()
  §15 Overtrade / greed guard   -> _behavior_gate()
  §17 Binary state persistence  -> operational_memory.json
"""
from __future__ import annotations
import json, logging, math, time
from datetime import datetime, timezone, time as dt_time, timedelta
from pathlib import Path
from typing import Optional, Tuple, Dict, List

log = logging.getLogger("protocol_evaluator")

OMNI_ROOT = Path.home() / "Omni-full-ALGO-Trading-Bot"
MEM_PATH   = OMNI_ROOT / "python" / "operational_memory.json"
RULES_PATH = OMNI_ROOT / "python" / "rules.json"

# ── §1 Killzone config ────────────────────────────────────────
KILLZONES = {
    "LONDON":   {"start": dt_time(7, 0),  "end": dt_time(12, 0), "min_rr": 2.0, "label": "London"},
    "NY":       {"start": dt_time(12, 0), "end": dt_time(15, 0), "min_rr": 2.0, "label": "NY"},
    "ASIA":     {"start": dt_time(0, 0),  "end": dt_time(3, 0),  "min_rr": 2.0, "label": "Asia"},
    "STANDARD": {"start": dt_time(6, 0),  "end": dt_time(11, 0), "min_rr": 2.0, "label": "Std"},
    "LONDRES":  {"start": dt_time(10, 0), "end": dt_time(15, 0), "min_rr": 2.0, "label": "LateLondon"},
}

TZ = timezone.utc

SESSION_PRIORITY = {"LONDON": 0, "STANDARD": 1, "NY": 2, "LONDRES": 3, "ASIA": 4}

# ── §9 Partial-close fractions (percent of 250-pip TP) ──────
TP_PIPS_TOTAL = 250.0
PARTIALS = [
    {"pct_of_tp": 0.25,  "label": "TP1", "close_frac": 0.50, "be_move": True,  "be_buffer_pips": 1},
    {"pct_of_tp": 0.625, "label": "TP2", "close_frac": 0.30, "be_move": False, "trail_to_tp1": True},
    {"pct_of_tp": 0.875, "label": "TP3", "close_frac": 0.20, "be_move": False, "trail_smart": True},
]

# ── §15 Overtrade guard ───────────────────────────────────────
MAX_TRADES_PER_SYMBOL_PER_4H = 1
SKIP_RATIO_TARGET = 0.70          # skip 70 % of setups
MAX_TRADES_PER_KILLZONE = 1
MAX_TRADES_PER_DAY = 3

# ── §16 Manual override commands ──────────────────────────────
OVERRIDE_PATH = OMNI_ROOT / "shared" / "manual_override.json"

# ═══════════════════════════════════════════════════════════════
#  PERSISTENT MEMORY HELPERS
# ═══════════════════════════════════════════════════════════════
def _load_mem() -> dict:
    if MEM_PATH.exists():
        try:
            with open(MEM_PATH) as f:
                data = json.load(f)
            # Ensure all required keys exist
            defaults = {
                "trades_today": 0,
                "trades_this_killzone": 0,
                "last_trade_ts": None,
                "last_symbol_ts": {},
                "setup_count": 0,
                "passed_count": 0,
                "peak_r_by_ticket": {},
                "partial_close_status": {},
                "halt_flags": {},
                "day_stamp": datetime.now(TZ).strftime("%Y-%m-%d"),
                "kz_stamp": "",
            }
            for k, v in defaults.items():
                data.setdefault(k, v)
            # Day-rollover reset
            today = datetime.now(TZ).strftime("%Y-%m-%d")
            if data.get("day_stamp") != today:
                data["trades_today"] = 0
                data["day_stamp"] = today
            # Killzone-rollover reset
            now = datetime.now(TZ)
            active_kz = _get_active_killzone(now)
            current_kz = data.get("kz_stamp", "")
            if active_kz and active_kz != current_kz:
                data["trades_this_killzone"] = 0
                data["kz_stamp"] = active_kz
            _save_mem(data)
            return data
        except Exception as e:
            log.error(f"MEM_LOAD_ERR: {e}")
    return {
        "trades_today": 0,
        "trades_this_killzone": 0,
        "last_trade_ts": None,
        "last_symbol_ts": {},
        "setup_count": 0,
        "passed_count": 0,
        "peak_r_by_ticket": {},
        "partial_close_status": {},
        "halt_flags": {},
        "day_stamp": datetime.now(TZ).strftime("%Y-%m-%d"),
        "kz_stamp": "",
    }

def _save_mem(mem: dict):
    try:
        MEM_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MEM_PATH, "w") as f:
            json.dump(mem, f, indent=2)
    except Exception as e:
        log.error(f"MEM_SAVE_ERR: {e}")

def _load_rules() -> dict:
    try:
        with open(RULES_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _override_status() -> dict:
    if OVERRIDE_PATH.exists():
        try:
            return json.loads(OVERRIDE_PATH.read_text())
        except Exception:
            return {}
    return {}

# ═══════════════════════════════════════════════════════════════
#  §1  SESSION GATING
# ═══════════════════════════════════════════════════════════════
def _is_within(t: dt_time, start: dt_time, end: dt_time) -> bool:
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end                               # overnight wrap

def _get_active_killzone(now: datetime) -> Optional[str]:
    """Return highest-priority active killzone name or None."""
    active = [n for n, cfg in KILLZONES.items()
              if _is_within(now.time(), cfg["start"], cfg["end"])]
    if not active:
        return None
    return min(active, key=lambda x: SESSION_PRIORITY.get(x, 99))

def _session_gate(now: datetime, signal: dict) -> Tuple[bool, str]:
    kz = _get_active_killzone(now)
    if kz is None:
        return False, "OUTSIDE_ALL_KILLZONES"
    sig_session = signal.get("session", "LONDON").upper()
    if sig_session not in KILLZONES:
        sig_session = "LONDON"
    # Active windows right now
    active = [n for n, cfg in KILLZONES.items() if _is_within(now.time(), cfg["start"], cfg["end"])]
    # Signal must belong to one of the currently active windows
    if sig_session not in active:
        return False, f"SIGNAL_SESSION_{sig_session}_OUTSIDE_ACTIVE"
    # Aggression timing (§6)
    hour = now.hour + now.minute / 60.0
    session_start = KILLZONES[sig_session]["start"]
    session_start_h = session_start.hour + session_start.minute / 60.0
    elapsed = hour - session_start_h
    aggression = signal.get("aggression", "normal").lower()
    if aggression == "aggressive" and elapsed > 3.0:
        return False, "AGGRESSIVE_TOO_LATE"
    if aggression == "conservative" and elapsed < 3.0:
        return False, "CONSERVATIVE_TOO_EARLY"
    return True, kz

# ═══════════════════════════════════════════════════════════════
#  §2  STRUCTURE / MICRO-SWEEP GATE
# ═══════════════════════════════════════════════════════════════
def _sweep_gate(signal: dict, mt5_data: dict) -> Tuple[bool, str, float]:
    chart = mt5_data.get("charts", {}).get(signal.get("symbol"), {})
    bars = chart.get("bars", [])
    if not isinstance(bars, list) or len(bars) < 3:
        return False, "NO_BARS", 0.0

    direction = signal.get("direction")
    last = bars[-1]
    prev = bars[-2]
    lc = float(last.get("c", 0)); pc = float(prev.get("c", 0))

    # Continuation bar
    if direction == "BUY" and lc <= pc:
        return False, "NO_CONTINUATION_BAR", 0.0
    if direction == "SELL" and lc >= pc:
        return False, "NO_CONTINUATION_BAR", 0.0

    # Two equally-sized bars (±10 % variance)
    ll = float(last.get("l", lc)); lh = float(last.get("h", lc))
    pl = float(prev.get("l", pc)); ph = float(prev.get("h", pc))
    last_body = abs(lc - float(last.get("o", lc)))
    prev_body = abs(pc - float(prev.get("o", pc)))
    if prev_body == 0:
        return False, "ZERO_PREV_BODY", 0.0
    body_ratio = last_body / prev_body
    if not (0.9 <= body_ratio <= 1.1):
        return False, f"BODY_RATIO_{body_ratio:.2f}", 0.0

    # R calculation
    entry = float(signal.get("entry_price", signal.get("entry", 0)))
    sl    = float(signal.get("sl", 0))
    tp    = float(signal.get("tp", 0))
    if entry == 0 or sl == 0:
        return False, "MISSING_SL_ENTRY", 0.0
    risk   = abs(entry - sl)
    reward = abs(tp - entry) if tp else risk * 5.0   # default 5R if no TP
    if risk == 0:
        return False, "ZERO_RISK", 0.0
    rr = reward / risk
    return True, "SEQUENCE_OK", min(rr, 10.0)

# ═══════════════════════════════════════════════════════════════
#  §3  MANIPULATION / CHOP GATE
# ═══════════════════════════════════════════════════════════════
def _chop_gate(signal: dict, mt5_data: dict) -> Tuple[bool, str]:
    sym = signal.get("symbol")
    chart = mt5_data.get("charts", {}).get(sym, {})
    bars = chart.get("bars", [])
    if len(bars) < 10:
        return False, "INSUFFICIENT_BARS"

    # ATR spike check
    ranges = [abs(float(b.get("h", 0)) - float(b.get("l", 0))) for b in bars[-10:]]
    avg_range = sum(ranges) / len(ranges)
    if avg_range == 0:
        return False, "ZERO_RANGE"
    recent = ranges[-3:]
    if all(r > avg_range * 1.6 for r in recent):
        return False, "MANIPULATION_BURST"

    # Volleyball (rapid reversal) check on last 2 bars
    o1 = float(bars[-2].get("o", 0)); c1 = float(bars[-2].get("c", 0))
    o2 = float(bars[-1].get("o", 0)); c2 = float(bars[-1].get("c", 0))
    if (c1 - o1) * (c2 - o2) < 0:           # opposite closes
        if abs(c2 - o2) > avg_range * 1.4:
            return False, "VOLLEYBALL_REVERSAL"

    return True, "CLEAR"

# ═══════════════════════════════════════════════════════════════
#  §4  FLAG PATTERNS (D1 only — disabled until D1 data present)
# ═══════════════════════════════════════════════════════════════
def _flag_gate(signal: dict, mt5_data: dict) -> Tuple[bool, str]:
    # D1 bars not guaranteed in omni_data yet; avoid false positives
    chart = mt5_data.get("charts", {}).get(signal.get("symbol"), {})
    d1_bars = chart.get("d1_bars", [])
    if not d1_bars or len(d1_bars) < 5:
        return False, "NO_D1_BARS"
    # TODO: full bull/bear flag sequence matcher when MT5 EA exports D1 bars
    return False, "NOT_IMPLEMENTED_YET"

# ═══════════════════════════════════════════════════════════════
#  §5  RISK PARAMS
# ═══════════════════════════════════════════════════════════════
def _risk_params(equity: float, leverage: int, signal: dict, symbol: str) -> dict:
    """
    §5 exact:
      Lots = equity / 250  (1000x)  or  equity / 100  (200x)
      SL   = 50 pips (XAU)  or  20-30 pips (forex)
      TP   = 250 pips (5R)
    """
    # Leverage read
    if leverage not in (1000, 200):
        leverage = 1000 if symbol == "XAUUSD" else 200

    if leverage == 1000 or symbol == "XAUUSD":
        lots = equity / 250.0
        sl_pips = 50.0
    else:
        lots = equity / 100.0
        sl_pips = 20.0           # tighter for forex

    # Hard cap per account size (§5)
    if lots > 5.0:
        lots = 5.0
    # Small account safety
    if equity < 150 and lots > 0.02:
        lots = 0.02

    entry = float(signal.get("entry_price", signal.get("entry", 0)))
    is_buy = signal.get("direction") == "BUY"
    pip = 0.01 if symbol in ("XAUUSD", "XAGUSD") else 0.0001

    # Standard trade SL
    if is_buy:
        sl_price = entry - sl_pips * pip
    else:
        sl_price = entry + sl_pips * pip

    # Structural TP or default 250 pips
    struct_tp = float(signal.get("tp", 0))
    raw_tp = entry + TP_PIPS_TOTAL * pip if is_buy else entry - TP_PIPS_TOTAL * pip
    if struct_tp == 0:
        tp_price = raw_tp
    else:
        tp_price = struct_tp if (is_buy and struct_tp > entry) or (not is_buy and struct_tp < entry) else raw_tp

    risk = abs(entry - sl_price)
    reward = abs(tp_price - entry)
    rr = reward / risk if risk > 0 else 0.0

    return {
        "lots": round(lots, 2),
        "sl_pips": sl_pips,
        "sl_price": round(sl_price, 5),
        "tp_price": round(tp_price, 5),
        "entry_price": round(entry, 5),
        "rr": round(rr, 2),
        "direction": signal.get("direction"),
        "symbol": symbol,
    }

# ═══════════════════════════════════════════════════════════════
#  §6  AGGRESSION / COOLDOWN GATE
# ═══════════════════════════════════════════════════════════════
def _aggression_gate(signal: dict, now: datetime, mem: dict) -> Tuple[bool, str]:
    kz = _get_active_killzone(now)
    if not kz:
        return False, "NO_KZ"

    # Max 2 trades per killzone
    if mem.get("kz_stamp") != kz:
        mem["trades_this_killzone"] = 0
        mem["kz_stamp"] = kz
    if mem["trades_this_killzone"] >= MAX_TRADES_PER_KILLZONE:
        return False, "KILLZONE_TRADE_LIMIT_REACHED"

    # Max 3 trades per day
    day = now.strftime("%Y-%m-%d")
    if mem.get("day_stamp") != day:
        mem["trades_today"] = 0
        mem["day_stamp"] = day
    if mem["trades_today"] >= MAX_TRADES_PER_DAY:
        return False, "DAILY_TRADE_LIMIT_REACHED"

    # Symbol 4h cooldown
    sym = signal.get("symbol")
    last_sym = mem.get("last_symbol_ts", {}).get(sym)
    if last_sym:
        if (now - datetime.fromisoformat(last_sym)).total_seconds() < 4 * 3600:
            return False, "SYMBOL_4H_COOLDOWN"

    return True, "OK"

# ═══════════════════════════════════════════════════════════════
#  §7  FORMATION / CUMULATIVITY GATE
# ═══════════════════════════════════════════════════════════════
def _formation_gate(signal: dict, mt5_data: dict) -> Tuple[bool, str]:
    """
    Must be FULL formation — Asia + London.
    No early angular entries.
    """
    sym = signal.get("symbol")
    chart = mt5_data.get("charts", {}).get(sym, {})
    # If signal has "formation_status" from MT5 EA, use it directly
    fs = signal.get("formation_status", chart.get("formation_status", "UNKNOWN")).upper()
    if fs in ("EARLY", "ANGULAR", "PARTIAL"):
        return False, f"FORMATION_{fs}"
    if fs == "FULL":
        return True, "FULL_FORMATION"
    # Fallback: if price already took the session liquidity, skip
    if signal.get("liquidity_already_taken") or chart.get("liquidity_already_taken"):
        return False, "LIQUIDITY_ALREADY_TAKEN"
    return True, "FORMATION_FALLBACK_OK"

# ═══════════════════════════════════════════════════════════════
#  §8  ENTRY MODEL GATE (FVG 1-7 surrogate via confluence)
# ═══════════════════════════════════════════════════════════════
def _entry_model_gate(signal: dict, mt5_data: dict) -> Tuple[bool, int, str]:
    """
    Surrogate for the full 7 FVG + Range + AMD models.
    Returns (valid, confidence_1_to_10, model_name)
    """
    sym = signal.get("symbol")
    chart = mt5_data.get("charts", {}).get(sym, {})
    direction = signal.get("direction")
    score = 0
    reasons = []

    # 1. H4 BOS / CHoCH aligned
    h4 = signal.get("h4_bias") or chart.get("h4_bias")
    if h4 and h4.upper() == direction:
        score += 2
        reasons.append("H4_ALIGNED")

    # 2. Unmitigated OB
    if signal.get("ob_unmitigated") or chart.get("ob_unmitigated"):
        score += 1
        reasons.append("OB")

    # 3. Sweep confirmed
    if signal.get("sweep_confirmed") or chart.get("sweep_confirmed"):
        score += 1
        reasons.append("SWEEP")

    # 4. Premium / Discount PDA
    pda = (signal.get("pda_zone") or chart.get("pda_zone", "")).lower()
    if direction == "BUY" and "discount" in pda:
        score += 1; reasons.append("DISCOUNT")
    elif direction == "SELL" and "premium" in pda:
        score += 1; reasons.append("PREMIUM")

    # 5. Multi-TF FVG present
    if signal.get("fvg_present") or chart.get("fvg_present"):
        score += 1
        reasons.append("FVG")

    # 6. Killzone alignment
    now = datetime.now(TZ)
    kz = _get_active_killzone(now)
    if kz:
        score += 1
        reasons.append(f"KZ_{kz}")

    # 7. SMT / BOS / CHoCH confluence (structural confluence)
    if signal.get("smt_divergence") or chart.get("smt_divergence"):
        score += 1
        reasons.append("SMT")
    if signal.get("bos_in_direction") or chart.get("bos_in_direction"):
        score += 1
        reasons.append("BOS")

    # 8. No chop
    ok, _ = _chop_gate(signal, mt5_data)
    if ok:
        score += 1
        reasons.append("CLEAR")

    model_name = "+".join(reasons) if reasons else "BASELINE"
    return score >= 6, score, model_name

# ═══════════════════════════════════════════════════════════════
#  §9  PARTIAL-CLOSE PLAN
# ═══════════════════════════════════════════════════════════════
def _partial_plan(risk: dict, mem: dict, ticket_hint: str = "") -> List[dict]:
    """
    Build TP1 / TP2 / TP3 / Runner specs based on §9.
    Returns list of partial-close instructions.
    """
    entry = risk["entry_price"]
    pip = 0.01 if risk["symbol"] in ("XAUUSD", "XAGUSD") else 0.0001
    is_buy = risk["direction"] == "BUY"
    sign = 1 if is_buy else -1

    plan = []
    for p in PARTIALS:
        target_price = entry + sign * (TP_PIPS_TOTAL * p["pct_of_tp"] * pip)
        plan.append({
            "label": p["label"],
            "target_price": round(target_price, 5),
            "close_frac": p["close_frac"],
            "be_move": p["be_move"],
            "be_buffer_pips": p.get("be_buffer_pips", 0),
            "trail_to_tp1": p.get("trail_to_tp1", False),
            "trail_smart": p.get("trail_smart", False),
        })
    # Runner appended implicitly by TP3 trail_smart=True
    return plan

# ═══════════════════════════════════════════════════════════════
#  §10 CONFIDENCE SCORE
# ═══════════════════════════════════════════════════════════════
def _confidence_score(signal: dict, entry_ok: bool, model_score: int,
                      chop_ok: bool, sweep_ok: bool, formation_ok: bool) -> int:
    """
    Aggressive setup target: 8/10  |  Conservative minimum: 6/10.
    """
    base = model_score  # already 0-8ish
    if entry_ok:
        base += 1
    if formation_ok:
        base += 1
    return min(base, 10)

# ═══════════════════════════════════════════════════════════════
#  §11 FRIDAY GATE
# ═══════════════════════════════════════════════════════════════
def _friday_gate(now: datetime, has_open_positions: bool) -> Tuple[bool, str]:
    if now.weekday() != 4:          # not Friday
        return True, "NOT_FRIDAY"
    if now.time() >= dt_time(14, 0):
        # After 14:00 UTC: same as after NY close
        return False, "FRIDAY_POST_14H"
    if now.time() >= dt_time(12, 0):
        # After 12:00 UTC: no new entries, manage existing only
        if not has_open_positions:
            return False, "FRIDAY_POST_12H_NO_OPEN"
        return True, "FRIDAY_MANAGE_ONLY"
    return True, "FRIDAY_OK"

# ═══════════════════════════════════════════════════════════════
#  §15 BEHAVIOUR / OVERTRADE / GREED / 70 % SKIP
# ═══════════════════════════════════════════════════════════════
def _behavior_gate(signal: dict, mem: dict) -> Tuple[bool, str]:
    mem["setup_count"] += 1
    if mem["setup_count"] < 5:
        _save_mem(mem)  # ensure persist

    # 70 % skip enforcement
    if mem["setup_count"] > 0:
        pass_rate = mem["passed_count"] / mem["setup_count"]
        if pass_rate > (1.0 - SKIP_RATIO_TARGET):
            mem["setup_count"] += 1  # count as evaluated
            _save_mem(mem)
            return False, f"SKIP_70_PCT_ENFORCED (pass_rate {pass_rate:.2%})"

    # Max-1-per-killzone hard cap (skip & overtrade guard)
    if mem["trades_this_killzone"] >= MAX_TRADES_PER_KILLZONE:
        return False, f"MAX_1_PER_KILLZONE ({mem['trades_this_killzone']} taken)"

    # No martingale
    if signal.get("scale_in") and mem.get("current_scale", 0) > 0:
        return False, "MARTINGALE_BLOCKED"

    return True, "OK"

# ═══════════════════════════════════════════════════════════════
#  §16 MANUAL OVERRIDE
# ═══════════════════════════════════════════════════════════════
def _manual_override_check(signal: dict) -> Tuple[bool, str]:
    """
    Returns (blocked, reason).  If blocked, bot IGNORES signal.
    """
    ovr = _override_status()
    if not ovr.get("active"):
        return False, ""

    cmd = ovr.get("command", "").upper()
    scope = ovr.get("scope", "ALL")
    sym = signal.get("symbol")

    if scope not in ("ALL", sym):
        return False, ""

    if cmd == "STOP_NEW_ENTRIES":
        return True, "OVERRIDE_STOP_ENTRIES"
    if cmd == "EMERGENCY_CLOSE":
        return True, "OVERRIDE_EMERGENCY"
    if cmd == "SKIP_ALL":
        return True, "OVERRIDE_SKIP_ALL"
    if cmd == "FORCE_BEARISH" and signal.get("direction") == "BUY":
        return True, "OVERRIDE_FORCE_BEARISH_BLOCK_LONG"
    if cmd == "FORCE_BULLISH" and signal.get("direction") == "SELL":
        return True, "OVERRIDE_FORCE_BULLISH_BLOCK_SHORT"

    return False, ""

# ═══════════════════════════════════════════════════════════════
#  §17 PERSISTENCE HELPERS
# ═══════════════════════════════════════════════════════════════
def mark_trade_executed(signal: dict, ticket: str, mem: dict):
    mem["trades_today"] += 1
    mem["trades_this_killzone"] += 1
    mem["passed_count"] += 1
    mem["last_trade_ts"] = datetime.now(TZ).isoformat()
    mem["last_symbol_ts"][signal.get("symbol")] = datetime.now(TZ).isoformat()
    _save_mem(mem)

def mark_partial_close(ticket: str, label: str, mem: dict):
    s = mem.setdefault("partial_close_status", {}).setdefault(ticket, {})
    s[f"{label.lower()}_done"] = True
    s["last_update"] = datetime.now(TZ).isoformat()
    _save_mem(mem)

# ═══════════════════════════════════════════════════════════════
#  MASTER EVALUATE
# ═══════════════════════════════════════════════════════════════
def evaluate(signal: dict, mt5_data: dict, account: dict,
             now: Optional[datetime] = None) -> dict:
    """
    Master protocol evaluation.
    Returns:
      {
        "trade": bool,
        "reason": str,
        "risk": dict,
        "confidence": int,
        "model": str,
        "partial_plan": [...],
        "session": str,
        "disaster_sl": float,
        "override_active": bool,
        "halt_flags": { ... }
      }
    """
    if now is None:
        now = datetime.now(TZ)

    result = {
        "trade": False,
        "reason": "",
        "risk": {},
        "confidence": 0,
        "model": "",
        "partial_plan": [],
        "session": "",
        "disaster_sl": 0.0,
        "override_active": False,
        "halt_flags": {},
    }

    mem = _load_mem()
    sym = signal.get("symbol", "")
    equity = float(account.get("equity", account.get("balance", 0)))
    leverage = int(account.get("leverage", 1000))
    open_positions = mt5_data.get("positions", [])

    # ── §16 Manual override ──
    blocked, ovr_reason = _manual_override_check(signal)
    if blocked:
        result["trade"] = False
        result["reason"] = ovr_reason
        result["override_active"] = True
        return result

    # ── §11 Friday ──
    ok, friday_reason = _friday_gate(now, len(open_positions) > 0)
    if not ok:
        result["reason"] = friday_reason
        return result

    # ── §1 Session ──
    ok, kz = _session_gate(now, signal)
    if not ok:
        result["reason"] = kz
        return result
    result["session"] = kz

    # ── §2 Sweep ──
    ok, sweep_reason, rr = _sweep_gate(signal, mt5_data)
    if not ok:
        result["reason"] = f"SWEEP_FAIL:{sweep_reason}"
        return result

    # ── §3 Chop ──
    ok, chop_reason = _chop_gate(signal, mt5_data)
    if not ok:
        result["reason"] = f"CHOP:{chop_reason}"
        return result

    # ── §7 Formation ──
    ok, form_reason = _formation_gate(signal, mt5_data)
    if not ok:
        result["reason"] = f"FORMATION:{form_reason}"
        return result

    # ── §6 Aggression & cooldown ──
    ok, agr_reason = _aggression_gate(signal, now, mem)
    if not ok:
        result["reason"] = f"AGGRESSION:{agr_reason}"
        return result

    # ── §8 Entry model / confluence ──
    entry_ok, model_score, model_name = _entry_model_gate(signal, mt5_data)
    if not entry_ok:
        result["reason"] = f"ENTRY_MODEL_FAIL (score {model_score})"
        # still continue to compute confidence for telemetry

    # ── §10 Confidence ──
    confidence = _confidence_score(signal, entry_ok, model_score, True, True, True)
    aggression = signal.get("aggression", "normal").lower()
    min_conf = 8 if aggression == "aggressive" else 6
    if confidence < min_conf:
        result["reason"] = f"CONFIDENCE_{confidence}_BELOW_MIN_{min_conf}"
        result["confidence"] = confidence
        result["model"] = model_name
        return result

    # ── §15 Behaviour / skip 70 % ──
    ok, beh_reason = _behavior_gate(signal, mem)
    if not ok:
        result["reason"] = f"BEHAVIOUR:{beh_reason}"
        return result

    # ── §5 Risk params ──
    risk = _risk_params(equity, leverage, signal, sym or "")
    if risk["rr"] < 3.0:
        result["reason"] = f"RR_{risk['rr']}_BELOW_3"
        return result
    if risk["rr"] > 8.0:
        log.info(f"RARE_OPPORTUNITY: RR {risk['rr']} on {sym}")

    # ── §9 Partial plan ──
    partial_plan = _partial_plan(risk, mem)

    # ── Disaster SL ──
    entry = risk["entry_price"]
    is_buy = risk["direction"] == "BUY"
    pip = 0.01 if sym in ("XAUUSD", "XAGUSD") else 0.0001
    disaster_sl = entry + (105 * pip if is_buy else -105 * pip)

    # ── Persist that we passed evaluation ──
    mem["passed_count"] += 1
    _save_mem(mem)

    result.update({
        "trade": True,
        "reason": "ALL_GATES_PASS",
        "risk": risk,
        "confidence": confidence,
        "model": model_name,
        "partial_plan": partial_plan,
        "disaster_sl": round(disaster_sl, 5),
    })
    return result

# ═══════════════════════════════════════════════════════════════
#  RUNNER / PARTIAL-CLOSE EXECUTION HELPERS
#   (called by position_trailing_manager.py)
# ═══════════════════════════════════════════════════════════════
def check_partial_close(ticket: int, pos: dict, mem: Optional[dict] = None) -> Optional[dict]:
    """
    Check if current profit hits TP1/TP2/TP3 thresholds.
    Returns action dict or None.
    """
    if mem is None:
        mem = _load_mem()
    s = mem.get("partial_close_status", {}).get(str(ticket), {})
    sym = pos.get("symbol", "")

    entry = float(pos.get("open_price", 0))
    cur   = float(pos.get("price", 0))
    direction = pos.get("direction", "")
    if entry == 0:
        return None

    pip = 0.01 if sym in ("XAUUSD", "XAGUSD") else 0.0001
    if direction == "BUY":
        pips = (cur - entry) / pip
    else:
        pips = (entry - cur) / pip

    for p in PARTIALS:
        label = p["label"]
        if s.get(f"{label.lower()}_done"):
            continue
        target_pips = TP_PIPS_TOTAL * p["pct_of_tp"]
        if pips >= target_pips:
            # Also enforce killzone time for partial close?
            # §11: if Friday >12H, runner only until London 12.
            # This is handled by position_trailing_manager's session check.
            return {
                "action": "PARTIAL_CLOSE",
                "ticket": ticket,
                "label": label,
                "close_frac": p["close_frac"],
                "target_price": entry + (target_pips * pip if direction == "BUY" else -target_pips * pip),
                "be_move": p["be_move"],
                "be_buffer_pips": p.get("be_buffer_pips", 0),
                "trail_to_tp1": p.get("trail_to_tp1", False),
                "trail_smart": p.get("trail_smart", False),
            }
    return None

def should_move_to_breakeven(ticket: int, pos: dict, mem: Optional[dict] = None) -> bool:
    """
    §5 / §9: Once into profit, move SL to entry + buffer.
    This is the FIRST thing after entry, before any partial.
    Actually the protocol says: "the stop loss (SL) can be adjusted to the entry price plus a buffer of 2 pips".
    Called immediately when profitable.
    """
    entry = float(pos.get("open_price", 0))
    cur   = float(pos.get("price", 0))
    direction = pos.get("direction", "")
    if entry == 0:
        return False
    pip = 0.01 if pos.get("symbol") in ("XAUUSD", "XAGUSD") else 0.0001
    if direction == "BUY":
        return (cur - entry) > (2 * pip)
    return (entry - cur) > (2 * pip)
