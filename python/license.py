"""
license.py — License validation for OMNI-ICT.

Checks OMNI_LICENSE_KEY against the license server on startup and every 24h.
Set OMNI_LICENSE_KEY=OWNER_BYPASS to skip all checks (personal/dev use).
Set OMNI_LICENSE_SERVER to override the validation endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("license")

LICENSE_KEY    = os.getenv("OMNI_LICENSE_KEY", "")
LICENSE_SERVER = os.getenv("OMNI_LICENSE_SERVER", "https://omni-full-algo-trading-bot-production.up.railway.app")
CACHE_FILE     = Path(__file__).resolve().parent.parent / "logs" / "license_cache.json"
RECHECK_HOURS  = 24
OWNER_BYPASS   = "OWNER_BYPASS"


def _cache_read() -> dict:
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def _cache_write(data: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def _validate_remote(key: str) -> dict:
    """Call license server. Returns {"valid": bool, "plan": str, "message": str}."""
    url = f"{LICENSE_SERVER}/validate?key={key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "omni-ict/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"valid": False, "message": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"valid": False, "message": f"Network error: {e}"}


def check(raise_on_fail: bool = True) -> dict:
    """
    Validate the license. Returns the license info dict.
    Raises SystemExit if invalid and raise_on_fail=True.
    Owner bypass key always passes.
    """
    if not LICENSE_KEY:
        msg = (
            "No license key set.\n"
            "Set OMNI_LICENSE_KEY in .env\n"
            "Purchase a subscription at https://omni-ict.com\n"
            "Use referral broker: https://www.midasfx.com/?ib=1128101"
        )
        log.error(msg)
        if raise_on_fail:
            raise SystemExit(1)
        return {"valid": False, "message": msg}

    if LICENSE_KEY == OWNER_BYPASS:
        log.debug("License: owner bypass — skipping validation")
        return {"valid": True, "plan": "owner", "message": "Owner bypass"}

    # Check cache first — avoid hammering the server
    cache = _cache_read()
    if cache.get("key") == LICENSE_KEY:
        checked_at = cache.get("checked_at", 0)
        age_hours = (time.time() - checked_at) / 3600
        if age_hours < RECHECK_HOURS and cache.get("valid"):
            log.debug("License: cache hit (age %.1fh) — %s", age_hours, cache.get("plan"))
            return cache

    log.info("Validating license key %s…", LICENSE_KEY[:8] + "****")
    result = _validate_remote(LICENSE_KEY)
    result["key"]        = LICENSE_KEY
    result["checked_at"] = time.time()

    if result.get("valid"):
        plan = result.get("plan", "unknown")
        exp  = result.get("expires_at", "")[:10]
        log.info("License valid — plan=%s expires=%s", plan, exp)
        _cache_write(result)
    else:
        msg = result.get("message", "License invalid")
        log.error("License check failed: %s", msg)
        log.error("Purchase/renew at https://omni-ict.com")
        if raise_on_fail:
            raise SystemExit(1)

    return result


def plan() -> str:
    """Return current plan name without raising (for info display)."""
    if LICENSE_KEY == OWNER_BYPASS:
        return "owner"
    cache = _cache_read()
    return cache.get("plan", "unknown")
