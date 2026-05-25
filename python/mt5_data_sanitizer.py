#!/usr/bin/env python3
"""mt5_data_sanitizer.py — strip unparseable string garbage from omni_data.json candles.

Run:  cd ~/Omni-full-ALGO-Trading-Bot/python && python3 mt5_data_sanitizer.py
"""
import json, re, os, sys
from pathlib import Path

JSON_PATH = str(Path.home() / "Library/Application Support"
    / "net.metaquotes.wine.metatrader5/drive_c/users/user"
    / "AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json")

def sanitize(data: dict) -> tuple[dict, int]:
    """Return cleaned data + count of fixes applied."""
    fixes = 0
    charts = data.get("charts", {})
    for sym, tfs in charts.items():
        if not isinstance(tfs, dict):
            continue
        for tf, bars in tfs.items():
            if not isinstance(bars, list):
                continue
            for i, bar in enumerate(bars):
                if not isinstance(bar, dict):
                    continue
                for key in ("o", "h", "l", "c", "v"):
                    val = bar.get(key)
                    if isinstance(val, str):
                        # Strip leading/trailing garbage quotes/brackets
                        cleaned = val.strip('"').strip("'").strip()
                        # If it still looks like JSON fragment, drop it
                        if cleaned.startswith("[") or cleaned.startswith("{") or cleaned == "":
                            bar[key] = 0.0
                            fixes += 1
                            continue
                        try:
                            bar[key] = float(cleaned)
                        except ValueError:
                            bar[key] = 0.0
                            fixes += 1
    return data, fixes

def main():
    if not os.path.exists(JSON_PATH):
        print(f"ERROR: {JSON_PATH} not found")
        sys.exit(1)

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        raw = f.read()
    # Strip trailing commas before ] or }
    raw = re.sub(r",\s*([\]}])", r"\1", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: JSON decode failed at char {e.pos}: {e.msg}")
        sys.exit(1)

    print(f"Loaded ok. charts={len(data.get('charts',{}))}")
    data, fixes = sanitize(data)
    if fixes:
        bak = JSON_PATH + ".bak"
        os.rename(JSON_PATH, bak)
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"Fixed {fixes} malformed candle fields. Backup: {bak}")
    else:
        print("No malformed floats found — data is clean.")

if __name__ == "__main__":
    main()
