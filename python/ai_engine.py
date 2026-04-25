"""
ai_engine.py — OMNI AI Market Regime Analyzer & Parameter Adapter
Runs as a background daemon thread, writes ai_state.json every 5 minutes.

Optional Claude integration: set ANTHROPIC_API_KEY to enable NL briefings.
pip install anthropic  (already in requirements.txt)

Exports:
    OmniAI            — background engine class
    load_ai_state()   — read the last saved state dict
    get_regime_color(phase)  → hex color string
    get_bias_color(bias)     → hex color string
    get_verdict_color(verdict) → hex color string
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
from pathlib import Path as _Path
try:
    from config import cfg
    JSON_PATH = cfg.JSON_PATH
    AI_STATE_PATH = str(_Path(__file__).parent / "ai_state.json")
except ImportError:
    _MT5_COMMON = (
        _Path.home() / "Library/Application Support"
        / "net.metaquotes.wine.metatrader5/drive_c/users/user"
        / "AppData/Roaming/MetaQuotes/Terminal/Common/Files"
    )
    JSON_PATH = str(_MT5_COMMON / "omni_data.json")
    AI_STATE_PATH = str(_Path(__file__).parent / "ai_state.json")

# ── Intervals ─────────────────────────────────────────────────────────────────
REGIME_INTERVAL = 300    # refresh regime every 5 min
PARAMS_INTERVAL = 600    # re-adapt params every 10 min

log = logging.getLogger("OmniAI")

# ── Session / AMD helpers ─────────────────────────────────────────────────────
_SESSIONS = [
    (22, 24, "ASIA"),
    (0,   7, "ASIA"),
    (7,  11, "LONDON"),
    (11, 12, "LONDON_CLOSE"),
    (12, 17, "NEW_YORK"),
    (17, 22, "NY_CLOSE"),
]

_KZ = [       # (start_h, end_h) UTC  — end is exclusive
    (22, 0),  # Asia open  (wraps midnight)
    (7,  9),  # London open
    (11, 13), # London close
    (12, 14), # NY open
    (19, 21), # NY close
]


def _session(hour: int) -> str:
    for s, e, name in _SESSIONS:
        if s < e and s <= hour < e:
            return name
        if s > e and (hour >= s or hour < e):
            return name
    return "CLOSED"


def _amd_phase(hour: int) -> str:
    """ACCUMULATION (Asia) → MANIPULATION (London) → DISTRIBUTION (NY)."""
    if hour >= 22 or hour < 7:
        return "ACCUMULATION"
    if 7 <= hour < 12:
        return "MANIPULATION"
    return "DISTRIBUTION"


def _in_killzone(hour: int) -> bool:
    for s, e in _KZ:
        if s < e and s <= hour < e:
            return True
        if s >= e and (hour >= s or hour < e):  # wraps midnight
            return True
    return False


# ── Color helpers (used by dashboard) ────────────────────────────────────────
def get_regime_color(phase: str) -> str:
    return {
        "TRENDING":  "#2ecc71",
        "RANGING":   "#3b82f6",
        "VOLATILE":  "#e84545",
        "QUIET":     "#4a5a72",
        "REVERSAL":  "#f0c96a",
        "BREAKOUT":  "#00e5a0",
    }.get(phase, "#a8b8c8")


def get_bias_color(bias: str) -> str:
    return {
        "BULLISH": "#2ecc71",
        "BEARISH": "#e84545",
        "NEUTRAL": "#4a5a72",
        "MIXED":   "#f0c96a",
    }.get(bias, "#a8b8c8")


def get_verdict_color(verdict: str) -> str:
    return {
        "STRONG_BUY":  "#2ecc71",
        "BUY":         "#00e5a0",
        "NEUTRAL":     "#4a5a72",
        "AVOID":       "#f0c96a",
        "SELL":        "#ff6b6b",
        "STRONG_SELL": "#e84545",
    }.get(verdict, "#a8b8c8")


# ── State I/O ─────────────────────────────────────────────────────────────────
def load_ai_state() -> dict:
    """Return the last state written by OmniAI, or {} if not yet available."""
    try:
        with open(AI_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ai_state(state: dict) -> None:
    try:
        with open(AI_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error("AI state save error: %s", e)


# ── MT5 data loader ───────────────────────────────────────────────────────────
def _load_mt5() -> dict:
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = re.sub(r',\s*([\]}])', r'\1', f.read())
        return json.loads(raw)
    except Exception:
        return {}


# ── Regime detector ───────────────────────────────────────────────────────────
def _detect_regime(data: dict) -> dict:
    now   = datetime.now(timezone.utc)
    hour  = now.hour
    session  = _session(hour)
    amd      = _amd_phase(hour)
    kz       = _in_killzone(hour)

    prices = data.get("prices", [])
    spreads = [p.get("spread", 0) for p in prices if p.get("spread", 0) > 0]
    avg_spread = sum(spreads) / len(spreads) if spreads else 0

    if avg_spread > 35:
        volatility = "HIGH"
    elif avg_spread < 8:
        volatility = "LOW"
    else:
        volatility = "NORMAL"

    # Bias from D1/H4 price structure — not from our own positions (circular).
    # Count how many symbols have bullish vs bearish recent closes on D1.
    bull_count = 0
    bear_count = 0
    for sym_charts in data.get("charts", {}).values():
        d1_raw = sym_charts.get("D1", [])
        if len(d1_raw) >= 3:
            try:
                closes = [float(b.get("close", b.get("c", 0))) for b in d1_raw[:3]]
                if closes[0] > closes[2]:
                    bull_count += 1
                elif closes[0] < closes[2]:
                    bear_count += 1
            except (TypeError, ValueError):
                pass
    if bull_count > bear_count:
        bias = "BULLISH"
    elif bear_count > bull_count:
        bias = "BEARISH"
    else:
        # Fall back to prices trend field if charts unavailable
        bullish_prices = sum(1 for p in prices if p.get("trend") == "BULLISH")
        bearish_prices = sum(1 for p in prices if p.get("trend") == "BEARISH")
        if bullish_prices > bearish_prices:
            bias = "BULLISH"
        elif bearish_prices > bullish_prices:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

    # Market phase
    if volatility == "HIGH":
        phase = "VOLATILE"
    elif amd == "MANIPULATION":
        phase = "REVERSAL"
    elif session in ("LONDON", "NEW_YORK") and amd == "DISTRIBUTION":
        phase = "TRENDING"
    elif session in ("ASIA", "NY_CLOSE"):
        phase = "RANGING"
    else:
        phase = "RANGING"

    # Confidence
    conf = 45
    if kz:               conf += 15
    if amd == "DISTRIBUTION": conf += 10
    if session in ("LONDON", "NEW_YORK"): conf += 5
    if volatility == "NORMAL": conf += 5
    conf = max(0, min(100, conf))

    # SMT divergence heuristic: XAU / XAG spread divergence
    prices_map = {p["symbol"]: p for p in prices}
    xau_sp = prices_map.get("XAUUSD", {}).get("spread", 0)
    xag_sp = prices_map.get("XAGUSD", {}).get("spread", 0)
    smt = bool(xau_sp and xag_sp and abs(xau_sp - xag_sp) > 15)

    return {
        "phase":           phase,
        "bias":            bias,
        "amd_stage":       amd,
        "volatility":      volatility,
        "session":         session,
        "confidence":      conf,
        "killzone_active": kz,
        "smt_divergence":  smt,
    }


# ── Parameter adapter ─────────────────────────────────────────────────────────
def _adapt_params(regime: dict) -> dict:
    phase   = regime["phase"]
    vol     = regime["volatility"]
    session = regime["session"]
    kz      = regime["killzone_active"]

    base_risk = 2.0
    min_rr    = 1.5
    min_conf  = 55
    max_open  = 3
    daily_lim = 6.0
    tp1_pct   = 0.50
    skip_sess = []
    priority  = []
    notes     = []

    if vol == "HIGH":
        base_risk -= 0.5
        min_rr    += 0.5
        min_conf   = 65
        notes.append("High spread → tighter risk controls")
    elif vol == "LOW":
        min_conf = 50
        notes.append("Low volatility → relaxed confidence threshold")

    if session == "ASIA":
        base_risk -= 0.5
        notes.append("Asia session → reduced size")
    elif session == "CLOSED":
        base_risk = 0.5
        notes.append("Market closed → minimal risk")

    if kz:
        min_rr = max(1.3, min_rr - 0.2)
        notes.append("Kill zone active → tighter RR acceptable")

    if phase == "TRENDING":
        tp1_pct  = 0.40
        priority = ["SWEEP_HIGH_OB", "SWEEP_LOW_OB"]
        notes.append("Trending → let profits run, favour sweeps")
    elif phase == "RANGING":
        min_rr  = max(min_rr, 2.0)
        tp1_pct = 0.60
        priority = ["FVG_FILL_BULLISH", "FVG_FILL_BEARISH"]
        notes.append("Ranging → higher RR, target FVG fills")
    elif phase == "VOLATILE":
        max_open  = 2
        base_risk = max(1.0, base_risk - 0.5)
        notes.append("Volatile → fewer positions, smaller size")
    elif phase == "REVERSAL":
        priority = ["SWEEP_HIGH_OB", "SWEEP_LOW_OB"]
        notes.append("Manipulation window → prioritise sweeps")

    return {
        "base_risk_pct":    round(max(0.5, base_risk), 2),
        "min_rr":           round(min_rr, 2),
        "min_confidence":   min_conf,
        "max_open":         max_open,
        "daily_loss_limit": daily_lim,
        "skip_session":     skip_sess,
        "priority_setups":  priority,
        "tp1_pct":          tp1_pct,
        "ai_rationale":     " | ".join(notes) if notes else "Standard parameters",
    }


# ── Signal evaluator ──────────────────────────────────────────────────────────
def _evaluate_signals(data: dict, regime: dict) -> list:
    bias    = regime["bias"]
    phase   = regime["phase"]
    session = regime["session"]
    kz      = regime["killzone_active"]
    signals = []

    for p in data.get("prices", []):
        sym    = p.get("symbol", "")
        spread = p.get("spread", 0)
        if not sym or spread > 60:
            continue

        if bias == "BULLISH" and phase in ("TRENDING", "REVERSAL"):
            verdict  = "BUY" if kz else "NEUTRAL"
            ai_conf  = 65 if kz else 45
            reason   = (
                f"Bullish bias in {phase} phase"
                + (" — kill zone active" if kz else "")
            )
        elif bias == "BEARISH" and phase in ("TRENDING", "REVERSAL"):
            verdict  = "SELL" if kz else "NEUTRAL"
            ai_conf  = 65 if kz else 45
            reason   = (
                f"Bearish bias in {phase} phase"
                + (" — kill zone active" if kz else "")
            )
        else:
            verdict  = "NEUTRAL"
            ai_conf  = 30
            reason   = f"{session} session {phase.lower()} — await key level"

        signals.append({
            "symbol":        sym,
            "direction":     "BUY" if bias == "BULLISH" else "SELL" if bias == "BEARISH" else "NEUTRAL",
            "entry_type":    "AI_SCAN",
            "ai_verdict":    verdict,
            "ai_confidence": ai_conf,
            "ai_reasoning":  reason,
        })

    return signals[:10]


# ── Claude API insight (optional) ─────────────────────────────────────────────
def _claude_insight(regime: dict, params: dict, data: dict) -> str:
    """Call Claude API for a natural-language briefing. Falls back silently."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return _rule_insight(regime, params)

    try:
        import anthropic

        account = data.get("account", {})
        top_prices = data.get("prices", [])[:6]
        price_txt = "\n".join(
            f"  {p['symbol']}: {p['bid']} (spread {p['spread']})"
            for p in top_prices if p.get("bid")
        )

        # Gather open positions for context (not for bias detection)
        positions  = data.get("positions", [])
        pos_summary = ", ".join(
            f"{p.get('symbol')} {p.get('type')} {p.get('profit',0):+.2f}USD"
            for p in positions[:4]
        ) or "None"

        # Chart structure for key symbols
        chart_context = []
        for sym, sym_charts in list(data.get("charts", {}).items())[:4]:
            d1 = sym_charts.get("D1", [])
            h4 = sym_charts.get("H4", [])
            if d1 and h4:
                d1_close = d1[0].get("close", d1[0].get("c", 0))
                h4_close = h4[0].get("close", h4[0].get("c", 0))
                chart_context.append(f"  {sym}: D1_close={d1_close:.5g}, H4_close={h4_close:.5g}")
        chart_txt = "\n".join(chart_context) or "  (no chart data)"

        prompt = (
            f"=== ICT MARKET ANALYSIS REQUEST ===\n"
            f"Time: {regime['session']} session | AMD Phase: {regime['amd_stage']}\n"
            f"Market Regime: {regime['phase']} | Structural Bias: {regime['bias']}\n"
            f"Kill Zone Active: {regime['killzone_active']} | Regime Confidence: {regime['confidence']}%\n\n"
            f"Account: {account.get('equity', 0):.2f} {account.get('currency', 'USD')} equity\n"
            f"Open Positions: {pos_summary}\n\n"
            f"Live Prices:\n{price_txt}\n\n"
            f"D1/H4 Structure Sample:\n{chart_txt}\n\n"
            f"Bot Parameters: risk={params['base_risk_pct']}% | "
            f"minRR={params['min_rr']} | minConf={params['min_confidence']} | "
            f"priority={params['priority_setups']}\n\n"
            "Provide a focused ICT analysis:\n"
            "1. Current market maker model phase (accumulation/manipulation/distribution)\n"
            "2. Which 1-2 symbols have the best setup potential right now and why\n"
            "3. One specific risk to watch (liquidity level, session overlap, spread concern)\n"
            "Be concise (3-4 sentences total). Use ICT terminology: OB, FVG, EQH/EQL, SMT, AMD."
        )

        import concurrent.futures
        import os as _os
        client = anthropic.Anthropic(api_key=api_key)
        model = _os.getenv("OMNI_CLAUDE_MODEL", "claude-sonnet-4-6")
        def _call():
            return client.messages.create(
                model=model,
                max_tokens=400,
                system=(
                    "You are an expert ICT (Inner Circle Trader) market analyst embedded in a live "
                    "automated trading system. Your analysis directly influences trading decisions. "
                    "Use precise ICT terminology. Be actionable and specific — no generic advice. "
                    "Reference the actual price data and session context provided."
                ),
                messages=[{"role": "user", "content": prompt}],
            )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_call)
            try:
                msg = future.result(timeout=15)
            except concurrent.futures.TimeoutError:
                log.warning("Claude API timeout after 15s; using rule-based insight")
                return _rule_insight(regime, params)
        return msg.content[0].text.strip()

    except Exception as e:
        log.debug("Claude insight failed: %s", e)
        return _rule_insight(regime, params)


def _rule_insight(regime: dict, params: dict) -> str:
    """Rule-based insight when API is unavailable."""
    session  = regime.get("session", "—")
    amd      = regime.get("amd_stage", "—")
    phase    = regime.get("phase", "—")
    bias     = regime.get("bias", "—")
    kz       = regime.get("killzone_active", False)
    conf     = regime.get("confidence", 0)
    priority = params.get("priority_setups", [])

    parts = [
        f"[{session}] {amd} phase | {phase} market | {bias} bias ({conf}% confidence)."
    ]
    if kz:
        parts.append("Kill zone active — elevated opportunity window.")
    if priority:
        parts.append(f"Prioritising: {', '.join(priority)} setups.")

    detail = {
        "TRENDING":  "Trend in play — seek high-confluence OB retests aligned with D1 bias.",
        "RANGING":   "Range-bound — FVG fills and equal H/L sweeps are preferred entries.",
        "REVERSAL":  "London manipulation window — watch for liquidity sweeps reversing into session bias.",
        "VOLATILE":  "Elevated spreads — wait for normalisation; reduce size until conditions stabilise.",
        "QUIET":     "Low activity — monitor key levels for breakout trigger.",
    }.get(phase, "")
    if detail:
        parts.append(detail)

    return " ".join(parts)


# ── OmniAI engine ─────────────────────────────────────────────────────────────
class OmniAI:
    """
    Background thread that refreshes regime every 5 min and writes ai_state.json.
    Params are re-adapted every 60 min to avoid churn.

    Usage:
        ai = OmniAI()
        ai.start()   # non-blocking; daemon thread
    """

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop   = threading.Event()
        self._params_ts   = 0.0
        self._cached_params: dict = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="OmniAI"
        )
        self._thread.start()
        log.info("OmniAI started")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                log.error("OmniAI tick error: %s", e)
            self._stop.wait(REGIME_INTERVAL)

    def _tick(self) -> None:
        data = _load_mt5()
        if not data:
            log.debug("OmniAI: no MT5 data available")
            return

        regime = _detect_regime(data)

        now = time.monotonic()
        if not self._cached_params or (now - self._params_ts) >= PARAMS_INTERVAL:
            self._cached_params = _adapt_params(regime)
            self._params_ts = now
            log.info(
                "OmniAI params updated: risk=%.2f%% minRR=%.1f priority=%s",
                self._cached_params["base_risk_pct"],
                self._cached_params["min_rr"],
                self._cached_params["priority_setups"],
            )

        signals = _evaluate_signals(data, regime)
        insight = _claude_insight(regime, self._cached_params, data)

        _save_ai_state({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "regime":    regime,
            "params":    self._cached_params,
            "signals":   signals,
            "insight":   insight,
        })

        log.debug(
            "OmniAI: %s | %s | conf=%d%%",
            regime["phase"], regime["bias"], regime["confidence"]
        )
