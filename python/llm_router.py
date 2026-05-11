"""
llm_router.py — Hybrid LLM routing for OMNI AI market analysis.

Routes between Ollama (local) and Claude API (cloud) based on OMNI_AI_PROVIDER:
  ollama  → always local LLM via Ollama REST API
  claude  → always Anthropic Claude API
  hybrid  → simple regime summaries to Ollama, complex ICT analysis to Claude

Env vars:
  OMNI_AI_PROVIDER    (default: hybrid)
  OMNI_OLLAMA_HOST     (default: http://localhost:11434)
  OMNI_OLLAMA_MODEL    (default: llama3.1)
  ANTHROPIC_API_KEY    (required for claude mode)
  OMNI_CLAUDE_MODEL    (default: claude-opus-4-7)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger("llm_router")

# ── Config ────────────────────────────────────────────────────────────────────
PROVIDER = os.getenv("OMNI_AI_PROVIDER", "hybrid").lower().strip()
OLLAMA_HOST = os.getenv("OMNI_OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OMNI_OLLAMA_MODEL", "llama3.1")
CLAUDE_MODEL = os.getenv("OMNI_CLAUDE_MODEL", "claude-opus-4-7")
CLAUDE_TIMEOUT = 15.0
OLLAMA_TIMEOUT = 10.0

# ── Prompt builder (shared) ───────────────────────────────────────────────────

def _build_ict_prompt(regime: dict, params: dict, data: dict) -> str:
    """Build the full ICT analysis prompt from market data."""
    account = data.get("account", {})
    top_prices = data.get("prices", [])[:6]
    price_txt = "\n".join(
        f"  {p['symbol']}: {p['bid']} (spread {p['spread']})"
        for p in top_prices if p.get("bid")
    )

    positions = data.get("positions", [])
    pos_summary = ", ".join(
        f"{p.get('symbol')} {p.get('type')} {p.get('profit', 0):+.2f}USD"
        for p in positions[:4]
    ) or "None"

    chart_context = []
    for sym, sym_charts in list(data.get("charts", {}).items())[:4]:
        d1 = sym_charts.get("D1", [])
        h4 = sym_charts.get("H4", [])
        if d1 and h4:
            d1_close = d1[0].get("close", d1[0].get("c", 0))
            h4_close = h4[0].get("close", h4[0].get("c", 0))
            chart_context.append(f"  {sym}: D1_close={d1_close:.5g}, H4_close={h4_close:.5g}")
    chart_txt = "\n".join(chart_context) or "  (no chart data)"

    return (
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


def _build_simple_prompt(regime: dict, params: dict) -> str:
    """Shortened prompt for lightweight local LLM summarisation."""
    return (
        f"Market regime: {regime['phase']} | {regime['bias']} bias | "
        f"{regime['session']} session | AMD: {regime['amd_stage']}. "
        f"Risk={params['base_risk_pct']}% minRR={params['min_rr']}. "
        "Give a one-sentence ICT trading focus tip."
    )


# ── Ollama client ─────────────────────────────────────────────────────────────

def _ollama_generate(prompt: str, model: str = OLLAMA_MODEL) -> Optional[str]:
    """Call local Ollama /api/generate. Returns text or None on failure."""
    try:
        import requests
        r = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=OLLAMA_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        return body.get("response", "").strip() or None
    except Exception as e:
        log.debug("Ollama call failed: %s", e)
        return None


# ── Claude client ─────────────────────────────────────────────────────────────

def _claude_generate(prompt: str, model: str = CLAUDE_MODEL) -> Optional[str]:
    """Call Anthropic Claude API with prompt caching on system prompt."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.debug("Claude skipped: ANTHROPIC_API_KEY not set")
        return None

    try:
        import anthropic
        import concurrent.futures

        client = anthropic.Anthropic(api_key=api_key)

        system_payload = [
            {
                "type": "text",
                "text": (
                    "You are an expert ICT (Inner Circle Trader) market analyst embedded in a live "
                    "automated trading system. Your analysis directly influences trading decisions. "
                    "Use precise ICT terminology. Be actionable and specific — no generic advice. "
                    "Reference the actual price data and session context provided."
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ]

        def _call():
            return client.messages.create(
                model=model,
                max_tokens=400,
                thinking={"type": "adaptive"},
                system=system_payload,
                messages=[{"role": "user", "content": prompt}],
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_call)
            msg = future.result(timeout=CLAUDE_TIMEOUT)

        # content is a list of TextBlock / ThinkingBlock etc
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                return block.text.strip()
        return None

    except concurrent.futures.TimeoutError:
        log.warning("Claude API timeout after %.0fs", CLAUDE_TIMEOUT)
        return None
    except Exception as e:
        log.debug("Claude call failed: %s", e)
        return None


# ── Hybrid heuristic ──────────────────────────────────────────────────────────

def _is_complex_regime(regime: dict) -> bool:
    """Return True when the regime warrants a deep Claude analysis."""
    return (
        regime.get("phase") in ("VOLATILE", "REVERSAL", "BREAKOUT")
        or regime.get("killzone_active") is True
        or regime.get("confidence", 0) < 50
    )


# ── Public API ────────────────────────────────────────────────────────────────

def _rule_insight(regime: dict, params: dict) -> str:
    """Rule-based insight when all LLM backends are unavailable."""
    session = regime.get("session", "—")
    amd = regime.get("amd_stage", "—")
    phase = regime.get("phase", "—")
    bias = regime.get("bias", "—")
    kz = regime.get("killzone_active", False)
    conf = regime.get("confidence", 0)
    priority = params.get("priority_setups", [])

    parts = [
        f"[{session}] {amd} phase | {phase} market | {bias} bias ({conf}% confidence)."
    ]
    if kz:
        parts.append("Kill zone active — elevated opportunity window.")
    if priority:
        parts.append(f"Prioritising: {', '.join(priority)} setups.")

    detail = {
        "TRENDING": "Trend in play — seek high-confluence OB retests aligned with D1 bias.",
        "RANGING": "Range-bound — FVG fills and equal H/L sweeps are preferred entries.",
        "REVERSAL": "London manipulation window — watch for liquidity sweeps reversing into session bias.",
        "VOLATILE": "Elevated spreads — wait for normalisation; reduce size until conditions stabilise.",
        "QUIET": "Low activity — monitor key levels for breakout trigger.",
    }.get(phase, "")
    if detail:
        parts.append(detail)

    return " ".join(parts)


def llm_insight(regime: dict, params: dict, data: dict) -> str:
    """
    Unified LLM insight entrypoint.

    Respects OMNI_AI_PROVIDER:
      ollama  → always Ollama (light prompt)
      claude  → always Claude (full prompt + cached system)
      hybrid  → simple summaries via Ollama, complex regimes via Claude

    Falls back to _rule_insight() on any failure.
    """
    provider = PROVIDER
    complex_regime = _is_complex_regime(regime)

    # ── Ollama path ──────────────────────────────────────────────────────────
    if provider == "ollama":
        prompt = _build_simple_prompt(regime, params)
        result = _ollama_generate(prompt)
        if result:
            return result
        return _rule_insight(regime, params)

    # ── Claude path ────────────────────────────────────────────────────────────
    if provider == "claude":
        prompt = _build_ict_prompt(regime, params, data)
        result = _claude_generate(prompt)
        if result:
            return result
        return _rule_insight(regime, params)

    # ── Hybrid path ────────────────────────────────────────────────────────────
    if provider == "hybrid":
        if complex_regime:
            prompt = _build_ict_prompt(regime, params, data)
            result = _claude_generate(prompt)
            if result:
                return result
            # Claude failed — try Ollama as secondary fallback
            result = _ollama_generate(_build_simple_prompt(regime, params))
            if result:
                return result
        else:
            # Simple regime → try Ollama first, fallback to Claude
            result = _ollama_generate(_build_simple_prompt(regime, params))
            if result:
                return result
            result = _claude_generate(_build_ict_prompt(regime, params, data))
            if result:
                return result
        return _rule_insight(regime, params)

    # Unknown provider → rule fallback
    log.warning("Unknown OMNI_AI_PROVIDER '%s' — using rule insight", provider)
    return _rule_insight(regime, params)
