#!/usr/bin/env python3
"""
diag_omni_data.py — quickly inspect what the MT5 EA is exporting.

Run:
    python python/diag_omni_data.py

Prints:
  · path of omni_data.json and its file age
  · top-level keys in the JSON
  · per-symbol summary: timeframes available, newest bar age, count of bars
  · which canonical watchlist symbols don't have a direct match (and what
    aliases would be tried)

Use this when orchestrator complains about stale HTF bars — the output makes it
obvious whether the issue is broker symbol naming, the EA not exporting a
symbol, or genuinely stale data.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from mt5_connector import JSON_PATH, SYMBOL_ALIASES, _detect_broker_offset
except Exception as e:
    print(f"could not import mt5_connector: {e}")
    sys.exit(2)


def _load_raw(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = re.sub(r",\s*([\]}])", r"\1", raw)
    return json.loads(raw)


def _parse_bar_time(t) -> float:
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        # "2026.04.27 19:00:00"
        try:
            return datetime.strptime(t, "%Y.%m.%d %H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def main() -> int:
    p = JSON_PATH
    print(f"omni_data.json path: {p}")
    if not os.path.exists(p):
        print("  ✗ FILE DOES NOT EXIST — MT5 EA isn't writing data.")
        print("    Check the EA is loaded on a chart, AlgoTrading is on, and")
        print("    'Allow file access' is enabled in MT5 settings.")
        return 1
    age = time.time() - os.path.getmtime(p)
    print(f"file age: {age:.1f}s ({'FRESH' if age < 60 else 'STALE — EA may be stopped'})")

    try:
        data = _load_raw(p)
    except Exception as e:
        print(f"  ✗ parse error: {e}")
        return 1

    print(f"\ntop-level keys: {list(data.keys())}")

    # Detect broker server-time offset so we can show *corrected* freshness
    offset = _detect_broker_offset(data)
    if offset != 0.0:
        print(f"\nbroker server-time offset: {offset/3600:+.1f}h "
              f"({int(offset):+d}s) — bar timestamps will be normalised to UTC")
    else:
        print("\nbroker server-time offset: 0 (or not detectable)")

    charts = data.get("charts", {}) or {}
    print(f"\n{'symbol':<18} {'timeframes (top-6)':<32} {'raw_age':<10} {'utc_age':<10} {'count(H1)':<10}")
    print("-" * 84)
    for sym in sorted(charts.keys()):
        tfs = [k for k in list(charts[sym].keys()) if k in ("M1","M5","M15","H1","H4","D1","W1")][:6]
        h1 = charts[sym].get("H1", [])
        newest = 0.0
        for b in h1:
            t = _parse_bar_time(b.get("t", 0))
            if t > newest:
                newest = t
        raw_age = f"{(time.time() - newest):.0f}s" if newest > 0 else "—"
        utc_age = f"{(time.time() - (newest - offset)):.0f}s" if newest > 0 else "—"
        print(f"{sym:<18} {','.join(tfs):<32} {raw_age:<10} {utc_age:<10} {len(h1):<10}")

    # Compare against canonical watchlist
    canonical = list(SYMBOL_ALIASES.keys()) + [
        "USDCHF", "NZDUSD", "EURJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
        "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD", "EURCAD", "EURAUD", "EURNZD",
        "EURCHF", "EURGBP", "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF",
        "CADCHF",
    ]
    missing = []
    aliased = []
    for sym in canonical:
        if sym in charts:
            continue
        # Try alias resolution
        for alt in SYMBOL_ALIASES.get(sym, []):
            if alt in charts:
                aliased.append((sym, alt))
                break
        else:
            # prefix scan
            hit = next((k for k in charts if k.startswith(sym) and k != sym), None)
            if hit:
                aliased.append((sym, hit))
            else:
                missing.append(sym)

    if aliased:
        print("\nALIAS RESOLUTIONS (canonical → broker key):")
        for c, a in aliased:
            print(f"  {c:<10} → {a}")
    if missing:
        print("\nNOT EXPORTED BY EA (probably need to add to MT5 Market Watch):")
        for s in missing:
            print(f"  · {s}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
