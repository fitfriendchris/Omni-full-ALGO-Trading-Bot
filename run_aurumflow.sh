#!/usr/bin/env bash
# AurumFlow (gold / XAUUSD) launcher — SHADOW mode by default (no live orders).
# For live data it needs AurumFlow_EA.mq5 attached to an MT5 chart (ZMQ :5555/:5556).
# Independent of the live Omni ICT bot (different magic 202405, ZMQ bridge — no collision).
set -euo pipefail
D="$HOME/Omni-full-ALGO-Trading-Bot/aurumflow"
cd "$D"
[ -d .venv ] || { echo "venv missing — run: python3 -m venv .venv && source .venv/bin/activate && pip install pyzmq pandas numpy pyyaml scipy matplotlib yfinance"; exit 1; }
source .venv/bin/activate
export AURUM_USE_ZMQ=true
echo "▶ AurumFlow starting (shadow=$(grep -A1 '^trading:' config/default.yaml | grep enabled | grep -o 'true\|false'))  Ctrl-C to stop"
exec python src/bot/main.py
