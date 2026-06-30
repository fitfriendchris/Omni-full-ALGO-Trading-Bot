#!/bin/bash
cd "$(dirname "$0")/python"
source .venv/bin/activate
echo "================================================"
echo "  OMNI ICT Auto-Trader  — LIVE (DEMO) | HIGH | AGGRESSIVE"
echo "  Monitor at: http://localhost:8050"
echo "  Press Ctrl+C to stop"
echo "================================================"
python auto_trader.py --risk HIGH --freq AGGRESSIVE
