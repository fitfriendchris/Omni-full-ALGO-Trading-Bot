#!/usr/bin/env python3
"""
Cross-Platform Setup Guide for AurumFlow

This guide covers setting up the ZeroMQ-based MT5 connector on Windows and macOS.
No Windows-only MetaTrader5 Python package needed.
"""

# ============================================================================
# PREREQUISITES
# ============================================================================
"""
## 1. Install Python Dependencies
```bash
pip install pyzmq pandas numpy pyyaml
```

## 2. Install MetaTrader 5

### Windows
Download from your broker's website and install normally.

### macOS
Option A: Use a native broker build (e.g., IC Markets, FTMO)
Option B: Run via Wine/CrossOver
  1. Install Wine (brew install --cask wine-stable)
  2. Download MT5 installer, run: wine mt5setup.exe
  3. MT5 will be available in ~/.wine/drive_c/...

## 3. Install ZeroMQ Library for MQL5

### Option A (Recommended): Use built-in ZMQ DLL
1. Download zmq.dll from https://zeromq.org/download/
2. Copy to: MT5/Libraries/zmq.dll
   - Windows: C:\\Program Files\\MetaTrader 5\\Libraries\\
   - macOS Wine: ~/.wine/drive_c/Program Files/MetaTrader 5/Libraries/

### Option B: Compile ZMQ from source
1. Install Visual Studio Build Tools (Windows) or Xcode (macOS)
2. Build libzmq following instructions at https://github.com/zeromq/libzmq
3. Rename output to zmq.dll and place in MT5/Libraries/
"""

# ============================================================================
# SETUP STEPS
# ============================================================================
"""
## Step 1: Compile the MQL5 EA
1. Open MetaEditor (Tools -> MetaQuotes Language Editor)
2. File -> Open -> Navigate to mql5/AurumFlow_EA.mq5
3. Press F7 to compile
4. Output: mql5/AurumFlow_EA.ex5

## Step 2: Install the EA in MT5
1. Open MT5
2. Navigate to File -> Open Data Folder
3. Copy AurumFlow_EA.ex5 to: MQL5/Experts/
4. Restart MT5 or refresh Navigator

## Step 3: Attach EA to a Chart
1. Open a XAUUSD chart (any timeframe)
2. Drag AurumFlow_EA from Navigator onto the chart
3. Configure inputs:
   - Publish Address: tcp://*:5555 (default)
   - Subscribe Address: tcp://*:5556 (default)
   - Symbol: XAUUSD
   - Magic Number: 202405
   - Trade Allowed: true (when ready)

## Step 4: Enable AutoTrading
1. Click the "AutoTrading" button in MT5 (must be green)
2. Check Tools -> Options -> Expert Advisors:
   - "Allow Automated Trading" ✓
   - "Allow DLL imports" ✓ (required for zmq.dll)

## Step 5: Start the Python Bot
```bash
# From the project root:
cd /path/to/aurumflow

# Shadow mode (recommended first):
AURUM_USE_ZMQ=true python -m src.bot.main --config config/default.yaml

# Or dry-run to verify connection:
AURUM_USE_ZMQ=true python -m src.bot.main --config config/default.yaml --dry-run
```

## Step 6: Verify Connection
Check the logs for:
- "PULL socket connected to tcp://localhost:5555"
- "PUSH socket connected to tcp://localhost:5556"
- "Connected to MT5 via ZMQ"

On the EA side, look for:
- "AurumFlow EA initialized successfully"
- "PUSH socket bound to tcp://*:5555"
- "PULL socket bound to tcp://*:5556"
"""

# ============================================================================
# TROUBLESHOOTING
# ============================================================================
"""
### "ZMQ library not found"
- Make sure zmq.dll is in MT5/Libraries/
- Restart MT5 after copying the DLL
- Check DLL imports are enabled in MT5 settings

### "Failed to bind socket"
- Ports 5555/5556 may be in use
- Change ports in both the EA inputs and Python config
- Or kill the process using them: netstat -ano | findstr :5555

### "No connection to EA"
- Make sure EA is attached to a chart with AutoTrading enabled
- Check the EA logs in MT5 (Experts tab)
- Verify zmq.dll is loaded (no DLL errors in Experts tab)

### macOS Wine specifics
- MT5 in Wine uses its own virtual network stack
- Use tcp://127.0.0.1:5555 instead of tcp://localhost:5555
- The Python side should also use 127.0.0.1
"""

# ============================================================================
# FALLING BACK TO LEGACY CONNECTOR
# ============================================================================
"""
If you're on Windows and the ZMQ approach doesn't work, you can fall back
to the Windows-only MetaTrader5 Python package:

1. Install: pip install MetaTrader5
2. Set env var: AURUM_USE_ZMQ=false
3. Configure mt5 settings in config/default.yaml

```bash
AURUM_USE_ZMQ=false python -m src.bot.main
```
"""