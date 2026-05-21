"""
swarm.py — OMNI Autonomous Agent Swarm coordinator.

Starts all business-domain agents as concurrent asyncio tasks and supervises
them.  Crashed agents are restarted with exponential back-off.

Business domains:
  SIGNAL_INTELLIGENCE  — SignalAgent, RegimeAgent
  EXECUTION_DESK       — ExecutionAgent, RiskAgent
  LEARNING_LAB         — LearningAgent
  OPERATIONS           — MonitorAgent, JournalAgent

CLI
---
    python swarm.py                        # run all agents
    python swarm.py --only signal_agent    # run subset
    python swarm.py --status               # print swarm state and exit
    python swarm.py --stop                 # signal swarm to stop
    python swarm.py --dry-run              # no MT5, fixture data only

The swarm writes shared/swarm_state.json every 30s for external monitoring.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE         = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
SHARED_DIR   = PROJECT_ROOT / "shared"
SWARM_STATE  = SHARED_DIR / "swarm_state.json"
STOP_FILE    = PROJECT_ROOT / "logs" / "swarm.stop"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("swarm")


# ── Agent registry ────────────────────────────────────────────────────────────

def _import_agents() -> list:
    """Import all agents, skip gracefully on import error."""
    agents = []
    registry = [
        ("signal_agent",    "agents.signal_agent",    "SignalAgent"),
        ("regime_agent",    "agents.regime_agent",    "RegimeAgent"),
        ("execution_agent", "agents.execution_agent", "ExecutionAgent"),
        ("risk_agent",      "agents.risk_agent",      "RiskAgent"),
        ("learning_agent",  "agents.learning_agent",  "LearningAgent"),
        ("monitor_agent",   "agents.monitor_agent",   "MonitorAgent"),
        ("journal_agent",   "agents.journal_agent",   "JournalAgent"),
        ("analyst_agent",   "agents.analyst_agent",   "AnalystAgent"),
        ("trail_manager",   "agents.position_trailing_manager", "TrailingManager"),
        ("entropy_signal",  "agents.entropy_signal_agent", "EntropySignalAgent"),
    ]
    for name, module, cls_name in registry:
        try:
            import importlib
            mod = importlib.import_module(module)
            cls = getattr(mod, cls_name)
            agents.append((name, cls))
        except Exception as e:
            log.warning("could not import %s: %s", cls_name, e)
    return agents


# ── Swarm runner ──────────────────────────────────────────────────────────────

async def run_swarm(only: list[str] = None, dry_run: bool = False) -> None:
    if dry_run:
        os.environ.setdefault("OMNI_PAPER_MODE", "true")
        os.environ["OMNI_DRY_RUN"] = "1"

    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)

    registry = _import_agents()
    if only:
        registry = [(n, c) for n, c in registry if n in only]
    if not registry:
        log.error("no agents available — check imports")
        return

    log.info("starting swarm with %d agents: %s",
             len(registry), [n for n, _ in registry])

    # Instantiate all agents
    instances = {}
    for name, cls in registry:
        try:
            instances[name] = cls()
            log.info("  ✓ %s  goal=%r", name, cls.GOAL)
        except Exception as e:
            log.error("  ✗ %s init failed: %s", name, e)

    stop_event = asyncio.Event()

    def _signal_handler(sig, frame):
        log.info("shutdown signal %s received", sig)
        stop_event.set()

    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Launch each agent as a supervised task
    tasks: dict[str, asyncio.Task] = {}
    restarts: dict[str, int] = {}
    backoffs:  dict[str, float] = {}

    async def _supervised(name: str, agent) -> None:
        while not stop_event.is_set():
            restarts[name] = restarts.get(name, 0) + 1
            log.info("agent %s starting (run #%d)", name, restarts[name])
            try:
                await agent.run()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.exception("agent %s crashed: %s", name, exc)
            if stop_event.is_set():
                break
            back = min(backoffs.get(name, 2) * 2, 120)
            backoffs[name] = back
            log.info("agent %s restarting in %.0fs (restart #%d)", name, back, restarts[name])
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=back)
            except asyncio.TimeoutError:
                pass
            # Re-instantiate to clear any bad state
            cls = type(agent)
            try:
                instances[name] = cls()
                agent = instances[name]
                backoffs[name] = max(2, backoffs[name] / 2)  # decay backoff on re-init success
            except Exception as e:
                log.error("agent %s re-init failed: %s", name, e)

    for name, agent in instances.items():
        tasks[name] = asyncio.create_task(_supervised(name, agent), name=name)

    # State writer
    async def _write_state():
        while not stop_event.is_set():
            try:
                _save_swarm_state({
                    "ts":       datetime.now(timezone.utc).isoformat(),
                    "agents":   {
                        n: {
                            "runs":   restarts.get(n, 0),
                            "alive":  not tasks[n].done(),
                            "domain": instances[n].DOMAIN if n in instances else "?",
                            "goal":   instances[n].GOAL   if n in instances else "?",
                        }
                        for n in tasks
                    },
                })
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    state_task = asyncio.create_task(_write_state())

    # Stop-file polling (so `touch logs/swarm.stop` also stops the swarm)
    async def _poll_stop_file():
        while not stop_event.is_set():
            if STOP_FILE.exists():
                log.info("stop file detected — shutting down")
                STOP_FILE.unlink(missing_ok=True)
                stop_event.set()
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass

    stop_poll = asyncio.create_task(_poll_stop_file())

    # Wait for shutdown
    await stop_event.wait()
    log.info("swarm shutting down…")

    for t in tasks.values():
        t.cancel()
    state_task.cancel()
    stop_poll.cancel()

    await asyncio.gather(*tasks.values(), state_task, stop_poll, return_exceptions=True)

    _save_swarm_state({
        "ts":      datetime.now(timezone.utc).isoformat(),
        "stopped": True,
        "agents":  {n: {"runs": restarts.get(n, 0), "alive": False} for n in tasks},
    })
    log.info("swarm stopped")


def _save_swarm_state(state: dict) -> None:
    try:
        SHARED_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SWARM_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, SWARM_STATE)
    except Exception:
        pass


# ── Status ────────────────────────────────────────────────────────────────────

def _print_status() -> None:
    if not SWARM_STATE.exists():
        print("swarm: no state file (not running or never started)")
        return
    try:
        state = json.loads(SWARM_STATE.read_text())
        print(json.dumps(state, indent=2))
    except Exception as e:
        print(f"swarm state read error: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="OMNI autonomous agent swarm")
    p.add_argument("--only", nargs="+", metavar="AGENT",
                   help="run only these agent names")
    p.add_argument("--status", action="store_true",
                   help="print swarm state and exit")
    p.add_argument("--stop", action="store_true",
                   help="touch the stop file to halt a running swarm")
    p.add_argument("--dry-run", action="store_true",
                   help="paper mode, fixture data (no live MT5 needed)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.status:
        _print_status()
        return 0

    if args.stop:
        STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_FILE.touch()
        print(f"stop file written: {STOP_FILE}")
        return 0

    # Load .env if present
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except ImportError:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    if k.strip() not in os.environ:
                        os.environ[k.strip()] = v.strip().strip('"').strip("'")

    asyncio.run(run_swarm(only=args.only, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
