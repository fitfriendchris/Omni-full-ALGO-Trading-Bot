"""
llm_client.py — Async LLM client for OMNI swarm.

Priority chain:
  1. OpenRouter → moonshotai/kimi-k2   (fast, cheap cloud)
  2. OpenRouter → moonshotai/kimi-k2.5 (cheaper fallback)
  3. Ollama     → qwen3.5:9b           (local, slow)
  4. None                               (pure-Python mode, zero LLM)

API key is read from ~/.hermes/.env (OPENROUTER_API_KEY).
Never blocks the main event loop — all calls are asyncio-native.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("llm_client")

# ── Config ────────────────────────────────────────────────────────────────────

OPENROUTER_BASE  = "https://openrouter.ai/api/v1"
OLLAMA_BASE      = os.getenv("OMNI_OLLAMA_HOST", "http://localhost:11434")
# Try Kimi via Ollama /v1 first (works if Hermes proxies it), then OpenRouter directly
KIMI_VIA_OLLAMA  = ["kimi-k2.6:cloud"]          # Ollama endpoint, cloud-routed by Hermes
KIMI_VIA_OR      = ["moonshotai/kimi-k2",        # OpenRouter — confirmed working
                     "moonshotai/kimi-k2.5"]     # cheaper fallback
OLLAMA_LOCAL     = ["qwen3.5:9b", "llama3.2:3b"] # pure local, slow
TIMEOUT_CLOUD    = 45.0
TIMEOUT_LOCAL    = 120.0

_or_key: Optional[str] = None


def _get_or_key() -> Optional[str]:
    global _or_key
    if _or_key:
        return _or_key
    # Try env first, then ~/.hermes/.env
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        env_path = Path.home() / ".hermes" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    _or_key = key or None
    return _or_key


# ── Core async HTTP ───────────────────────────────────────────────────────────

async def _post_json(url: str, payload: dict, headers: dict,
                     timeout: float) -> Optional[dict]:
    """Pure asyncio HTTP POST — no third-party deps required."""
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    host   = parsed.hostname
    port   = parsed.port or (443 if parsed.scheme == "https" else 80)
    path   = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    body = json.dumps(payload).encode()
    header_lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {host}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    for k, v in headers.items():
        header_lines.append(f"{k}: {v}")
    request = ("\r\n".join(header_lines) + "\r\n\r\n").encode() + body

    try:
        if parsed.scheme == "https":
            import ssl
            ctx = ssl.create_default_context()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ctx), timeout=10
            )
        else:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10
            )

        writer.write(request)
        await writer.drain()

        # Read response with timeout
        raw = await asyncio.wait_for(reader.read(131072), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        # Parse HTTP response
        header_end = raw.find(b"\r\n\r\n")
        if header_end == -1:
            return None
        body_bytes = raw[header_end + 4:]
        # Handle chunked encoding simply
        try:
            return json.loads(body_bytes.decode("utf-8", errors="replace"))
        except Exception:
            return None

    except Exception as e:
        log.debug("HTTP error %s: %s", url, e)
        return None


# ── OpenRouter ────────────────────────────────────────────────────────────────

async def _call_openrouter(model: str, messages: list[dict],
                           max_tokens: int = 800) -> Optional[str]:
    key = _get_or_key()
    if not key:
        return None

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://omni-trading-bot",
        "X-Title": "OMNI Trading Bot",
    }

    resp = await _post_json(
        f"{OPENROUTER_BASE}/chat/completions",
        payload, headers, TIMEOUT_CLOUD,
    )
    if not resp:
        return None
    try:
        content = resp["choices"][0]["message"]["content"]
        return content
    except (KeyError, IndexError, TypeError):
        log.debug("openrouter bad response: %s", str(resp)[:200])
        return None


# ── Ollama (OpenAI-compatible /v1 — for cloud-routed models like kimi-k2.6:cloud) ──

async def _call_ollama_chat(model: str, messages: list[dict],
                            max_tokens: int = 800) -> Optional[str]:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    resp = await _post_json(
        f"{OLLAMA_BASE}/v1/chat/completions",
        payload,
        {"Authorization": "Bearer ollama", "Content-Type": "application/json"},
        TIMEOUT_CLOUD,
    )
    if not resp:
        return None
    try:
        content = resp["choices"][0]["message"]["content"]
        return content if content else None  # treat empty string as failure
    except (KeyError, IndexError, TypeError):
        return None


# ── Ollama (local generate — for on-device models) ────────────────────────────

async def _call_ollama_local(model: str, prompt: str, max_tokens: int = 400) -> Optional[str]:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": max_tokens},
    }
    resp = await _post_json(
        f"{OLLAMA_BASE}/api/generate",
        payload, {}, TIMEOUT_LOCAL,
    )
    if not resp:
        return None
    return resp.get("response") or None


# ── Public API ────────────────────────────────────────────────────────────────

async def ask(system: str, user: str, max_tokens: int = 800) -> Optional[str]:
    """
    Ask the best available LLM. Returns a JSON string or None (pure-Python fallback).

    Priority:
      1. Kimi via Ollama /v1 (kimi-k2.6:cloud — routes through Hermes if configured)
      2. Kimi via OpenRouter (direct API — confirmed working)
      3. Local Ollama qwen3.5:9b / llama3.2:3b (slow, always free)
      4. None — pure-Python mode, swarm keeps running without LLM
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]

    # 1. Try Kimi via Ollama /v1 (preferred — stays local if Hermes proxies it)
    for model in KIMI_VIA_OLLAMA:
        try:
            result = await _call_ollama_chat(model, messages, max_tokens)
            if result:
                log.debug("llm via ollama-chat/%s  len=%d", model, len(result))
                return result
        except Exception as e:
            log.debug("ollama-chat/%s failed: %s", model, e)

    # 2. Kimi via OpenRouter (direct cloud, confirmed working)
    for model in KIMI_VIA_OR:
        try:
            result = await _call_openrouter(model, messages, max_tokens)
            if result:
                log.debug("llm via openrouter/%s  len=%d", model, len(result))
                return result
        except Exception as e:
            log.debug("openrouter/%s failed: %s", model, e)

    # 3. Local Ollama (slow but completely free)
    prompt = f"[SYSTEM]\n{system}\n\n[USER]\n{user}\n\n[ASSISTANT]\n"
    for model in OLLAMA_LOCAL:
        try:
            result = await _call_ollama_local(model, prompt, max_tokens)
            if result:
                log.debug("llm via ollama-local/%s  len=%d", model, len(result))
                return result
        except Exception as e:
            log.debug("ollama-local/%s failed: %s", model, e)

    log.info("llm: all providers failed — pure-Python fallback active")
    return None


def parse_json(text: Optional[str]) -> Optional[dict]:
    """Safely parse LLM JSON output. Returns None on failure."""
    if not text:
        return None
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.startswith("```"))
    try:
        return json.loads(text)
    except Exception:
        # Try to extract first {...} block
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except Exception:
                pass
    return None
