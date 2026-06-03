#!/usr/bin/env python3
"""
journal_sync.py — Standalone cron-friendly journal sync.

Runs every 60s via cron or launchd:
  • Pulls OMNI trades → formats as Zella Trade Scribe entries
  • Writes logs/journal.json (Obsidian dashboard)
  • Writes journal/entries.jsonl (GitHub repo sync)
  • Writes journal/zella_export.json (web app import)

Usage:
  python3 journal_sync.py        # one-shot
  python3 journal_sync.py --loop # daemon mode (60s)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from journal_bridge import main as bridge_main


def run_once():
    print(f"[{time.strftime('%H:%M:%S')}] journal_sync running…")
    try:
        bridge_main()
    except Exception as e:
        print(f"✗ journal_sync error: {e}")


def run_loop(interval: int = 60):
    print(f"journal_sync daemon started — interval {interval}s")
    while True:
        run_once()
        time.sleep(interval)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="OMNI Trading Journal Sync")
    ap.add_argument("--loop", action="store_true", help="Run as daemon")
    ap.add_argument("--interval", type=int, default=60, help="Seconds between runs")
    args = ap.parse_args()

    if args.loop:
        run_loop(args.interval)
    else:
        run_once()
