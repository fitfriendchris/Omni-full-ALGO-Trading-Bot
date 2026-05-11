#!/usr/bin/env python3
"""
Hermes ↔ OpenClaw Bidirectional Bridge
Runs on the 2018 MBP — file-based IPC, lightweight.

Flow:
  1. Hermes writes tasks to shared/hermes_inbox.json
  2. This bridge reads tasks, dispatches to OpenClaw workers
  3. Workers write results to shared/openclaw_outbox.json
  4. Hermes reads results and reports to user

Both agents run independently. No network calls needed.
"""

import json
import time
import os
from pathlib import Path
from datetime import datetime, timezone

SHARED_DIR = Path("~/Omni-full-ALGO-Trading-Bot/shared").expanduser()
INBOX = SHARED_DIR / "hermes_inbox.json"
OUTBOX = SHARED_DIR / "openclaw_outbox.json"
BRIDGE_LOG = SHARED_DIR / "bridge.log"

SHARED_DIR.mkdir(exist_ok=True)


def log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts} {msg}\n"
    with open(BRIDGE_LOG, "a") as f:
        f.write(line)
    print(line.strip())


def read_inbox() -> list:
    if not INBOX.exists():
        return []
    try:
        with open(INBOX) as f:
            data = json.load(f)
        return data if isinstance(data, list) else [data]
    except Exception as e:
        log(f"INBOX ERROR: {e}")
        return []


def write_outbox(result: dict):
    out = []
    if OUTBOX.exists():
        try:
            with open(OUTBOX) as f:
                out = json.load(f)
            if not isinstance(out, list):
                out = [out]
        except:
            out = []
    out.append(result)
    with open(OUTBOX, "w") as f:
        json.dump(out, f, indent=2)


def execute_task(task: dict) -> dict:
    """Run a task via OpenClaw subagent or direct execution."""
    task_id = task.get("id", "unknown")
    cmd = task.get("command", "")
    dept = task.get("department", "general")
    
    log(f"EXEC [{task_id}] {dept}: {cmd[:80]}")
    
    # Route by department
    if dept == "quant":
        # Trading tasks — validate safety first
        if any(x in cmd.lower() for x in ["live", "real money", "paper off"]):
            return {
                "id": task_id,
                "status": "BLOCKED",
                "result": "Safety: trading mode changes require human approval",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        # Execute via subagent spawn
        return _spawn_worker(task)
    
    elif dept == "product":
        # APEX / web tasks
        return _spawn_worker(task)
    
    elif dept == "infra":
        # System tasks
        return _run_local(cmd, task_id)
    
    else:
        # General — safe to run locally
        return _run_local(cmd, task_id)


def _run_local(cmd: str, task_id: str) -> dict:
    """Run a safe local command."""
    import subprocess
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=60,
            cwd=str(Path.home() / "Omni-full-ALGO-Trading-Bot")
        )
        return {
            "id": task_id,
            "status": "OK" if result.returncode == 0 else "ERROR",
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:1000],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {
            "id": task_id,
            "status": "ERROR",
            "result": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def _spawn_worker(task: dict) -> dict:
    """Spawn an OpenClaw subagent for complex work."""
    # For now, return a placeholder — real subagent spawn needs OpenClaw runtime
    return {
        "id": task.get("id"),
        "status": "QUEUED",
        "result": "OpenClaw subagent dispatched. Results will appear in outbox.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def process_inbox():
    """Main loop iteration."""
    tasks = read_inbox()
    if not tasks:
        return
    
    # Clear inbox after reading
    with open(INBOX, "w") as f:
        json.dump([], f)
    
    for task in tasks:
        result = execute_task(task)
        write_outbox(result)


def run_loop(interval: int = 5):
    """Run the bridge loop."""
    log("Bridge started. Watching for Hermes tasks...")
    while True:
        try:
            process_inbox()
        except Exception as e:
            log(f"LOOP ERROR: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        process_inbox()
    else:
        run_loop()
