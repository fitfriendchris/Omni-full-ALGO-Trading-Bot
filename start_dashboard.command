#!/bin/bash
cd "$(dirname "$0")/python"
source .venv/bin/activate
echo "================================================"
echo "  OMNI ICT Dashboard"
echo "  Open your browser at: http://localhost:8050"
echo "================================================"
python dashboard.py
