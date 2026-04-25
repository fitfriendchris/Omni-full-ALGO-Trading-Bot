#!/bin/bash
echo "Stopping OMNI ICT processes..."
pkill -f "python dashboard.py"    2>/dev/null && echo "  Dashboard stopped." || echo "  Dashboard was not running."
pkill -f "python auto_trader.py"  2>/dev/null && echo "  Trader stopped."    || echo "  Trader was not running."
pkill -f "python tradingview_connector.py" 2>/dev/null
echo "Done."
