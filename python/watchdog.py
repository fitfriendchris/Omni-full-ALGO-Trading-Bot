"""
watchdog.py — Supervisor process for OMNI-ICT autonomy.

Keeps the three long-lived services alive and healthy:

    · server       (FastAPI dashboard + API, uvicorn)
    · orchestrator (signal generator, loop mode)
    · auto_trader  (MT5 executor, if enabled)

Each service is described by a `ServiceSpec`. The watchdog starts them as
subprocesses, pipes their stdout/stderr to per-service log files under
`logs/`, and restarts any that exit with non-zero status — throttled by an
exponential back-off so a broken service doesn't thrash.

A simple "alive" check reads the child process status each cycle. For richer
health-checks you can extend `ServiceSpec.healthcheck` to hit an HTTP endpoint
or parse a heartbeat file.

CLI
---
    python watchdog.py                       # supervise default services
    python watchdog.py --only orchestrator   # run a subset
    python watchdog.py --status              # print running state then exit
    python watchdog.py --stop                # kill all tracked children

All stop signals are graceful (SIGTERM, then SIGKILL after --grace seconds).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("watchdog")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = LOG_DIR / "watchdog_state.json"
# Always use the venv Python so child services get all installed packages
# (uvicorn, fastapi, etc.). Fall back to sys.executable if venv not found.
_venv_py = PROJECT_ROOT / "venv" / "bin" / "python3"
PY = str(_venv_py) if _venv_py.exists() else sys.executable


# ──────────────────────────────────────────────────────────────────────────────
# Service specs
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ServiceSpec:
    name:         str
    argv:         list[str]
    cwd:          Path
    env:          dict = field(default_factory=dict)
    restart:      bool = True
    max_restarts: int = 20                          # per process lifetime
    backoff_s:    float = 2.0                        # initial back-off
    backoff_cap:  float = 120.0                      # max back-off
    healthcheck:  Optional[callable] = None          # optional fn → bool


def _default_specs(project_root: Path) -> list[ServiceSpec]:
    """The default process graph for OMNI-ICT autonomy."""
    return [
        ServiceSpec(
            name="server",
            argv=[PY, "-u", "-m", "uvicorn", "server:app",
                  "--host", "127.0.0.1", "--port", "8787"],
            cwd=project_root,
            env={"PYTHONUNBUFFERED": "1"},
        ),
        ServiceSpec(
            name="orchestrator",
            argv=[PY, "-u", str(HERE / "orchestrator.py"), "--loop", "60"],
            cwd=HERE,
            env={"PYTHONUNBUFFERED": "1"},
        ),
        ServiceSpec(
            name="auto_trader",
            argv=[PY, "-u", str(HERE / "auto_trader.py"), "--risk", "MODERATE", "--freq", "AGGRESSIVE"],
            cwd=HERE,
            env={"PYTHONUNBUFFERED": "1"},
            restart=True,
        ),
        ServiceSpec(
            name="telegram_bot",
            argv=[PY, "-u", str(HERE / "telegram_bot.py")],
            cwd=HERE,
            env={"PYTHONUNBUFFERED": "1"},
            restart=True,
            max_restarts=50,
        ),
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Running state
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RunningService:
    spec:        ServiceSpec
    proc:        Optional[subprocess.Popen] = None
    restarts:    int = 0
    next_delay:  float = 0.0
    started_at:  Optional[str] = None
    log_path:    Optional[Path] = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def _write_state(running: dict[str, RunningService]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "services": {
            name: {
                "pid":        (r.proc.pid if r.proc else None),
                "alive":      r.is_alive(),
                "restarts":   r.restarts,
                "started_at": r.started_at,
                "log":        str(r.log_path) if r.log_path else None,
            }
            for name, r in running.items()
        },
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Supervise loop
# ──────────────────────────────────────────────────────────────────────────────

def _write_alert(service_name: str, code: str) -> None:
    """Append an alert entry to logs/alerts.json (atomic write)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    alerts_path = LOG_DIR / "alerts.json"
    alerts = []
    if alerts_path.exists():
        try:
            alerts = json.loads(alerts_path.read_text(encoding="utf-8"))
        except Exception:
            alerts = []
    alerts.append({
        "ts":      datetime.now(timezone.utc).isoformat(),
        "service": service_name,
        "code":    code,
    })
    tmp = alerts_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(alerts, indent=2), encoding="utf-8")
    os.replace(tmp, alerts_path)


def _spawn(spec: ServiceSpec) -> tuple[subprocess.Popen, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{spec.name}.log"
    env = os.environ.copy()
    env.update(spec.env)
    log.info("spawning %s: %s (cwd=%s, log=%s)", spec.name,
             " ".join(spec.argv), spec.cwd, log_path)
    # Open log file in a with-block so the Python-side handle closes after Popen;
    # the child process inherits the fd and keeps writing until it exits.
    with open(log_path, "ab") as fh:
        proc = subprocess.Popen(
            spec.argv,
            cwd=str(spec.cwd),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,   # detach from TTY so SIGINT on watchdog won't cascade accidentally
        )
    return proc, log_path


def _terminate(rs: RunningService, grace: float = 5.0) -> None:
    if rs.proc is None or rs.proc.poll() is not None:
        return
    log.info("terminating %s (pid=%s)", rs.spec.name, rs.proc.pid)
    try:
        rs.proc.terminate()
        rs.proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        log.warning("force-killing %s (pid=%s)", rs.spec.name, rs.proc.pid)
        try:
            rs.proc.kill()
        except Exception:
            pass


def supervise(specs: list[ServiceSpec],
              *,
              poll_s: float = 2.0,
              grace_s: float = 5.0) -> int:
    """Run the supervise loop until SIGINT / SIGTERM."""
    running: dict[str, RunningService] = {
        s.name: RunningService(spec=s) for s in specs
    }

    def _shutdown(sig, frame):
        log.info("shutdown signal %s — stopping all services", sig)
        for rs in running.values():
            _terminate(rs, grace=grace_s)
        _write_state(running)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Initial start
    for rs in running.values():
        _start(rs)
    _write_state(running)

    while True:
        for rs in running.values():
            if rs.is_alive():
                continue
            if rs.proc is None:
                # never started, skip
                continue
            rc = rs.proc.returncode
            log.warning("%s exited rc=%s (restart=%s, restarts=%s)",
                        rs.spec.name, rc, rs.spec.restart, rs.restarts)
            if not rs.spec.restart:
                continue
            if rs.restarts >= rs.spec.max_restarts:
                log.critical("SERVICE %s HIT MAX RESTARTS (%d) — LEAVING STOPPED",
                             rs.spec.name, rs.spec.max_restarts)
                _write_alert(rs.spec.name, "max_restarts_exceeded")
                continue
            # Back-off before restart
            if rs.next_delay > 0:
                log.info("%s backing off %.1fs", rs.spec.name, rs.next_delay)
                time.sleep(rs.next_delay)
            _start(rs)
            rs.next_delay = min(max(rs.next_delay * 2, rs.spec.backoff_s),
                                rs.spec.backoff_cap)

        _write_state(running)
        # Jitter ±10% to spread I/O load and avoid deterministic timing
        time.sleep(poll_s * (0.9 + 0.2 * random.random()))


def _start(rs: RunningService) -> None:
    try:
        rs.proc, rs.log_path = _spawn(rs.spec)
        rs.started_at = datetime.now(timezone.utc).isoformat()
        rs.restarts += 1
        # fresh back-off on a manual (re)start
        if rs.restarts == 1:
            rs.next_delay = 0.0
    except Exception as e:
        log.exception("failed to spawn %s: %s", rs.spec.name, e)
        rs.next_delay = min(max(rs.next_delay * 2, rs.spec.backoff_s),
                            rs.spec.backoff_cap)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _print_status() -> int:
    state = _read_state()
    if not state:
        print("watchdog: no state file (not running or never started)")
        return 1
    print(json.dumps(state, indent=2))
    return 0


def _stop_all(grace: float) -> int:
    state = _read_state()
    services = state.get("services", {})
    stopped = 0
    for name, info in services.items():
        pid = info.get("pid")
        if not pid:
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
            stopped += 1
            print(f"sent SIGTERM to {name} (pid={pid})")
        except ProcessLookupError:
            print(f"{name} pid={pid} not running")
        except Exception as e:
            print(f"failed to stop {name} pid={pid}: {e}")
    if stopped:
        print(f"waiting {grace}s for graceful shutdown...")
        time.sleep(grace)
    return 0


def _validate_configs(project_root: Path) -> list[str]:
    """
    Validate rules.json and config.json before spawning services.
    Returns a list of error strings; empty list means all good.
    """
    errors = []

    rules_path = project_root / "python" / "rules.json"
    if not rules_path.exists():
        errors.append(f"rules.json not found: {rules_path}")
    else:
        try:
            rules = json.loads(rules_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"rules.json parse error: {e}")
            rules = {}

        for field in ("watchlist", "dual_tf", "scaling", "risk_rules"):
            if field not in rules:
                errors.append(f"rules.json missing required field: '{field}'")
        if not isinstance(rules.get("watchlist", None), list) or not rules.get("watchlist"):
            errors.append("rules.json: 'watchlist' must be a non-empty list")
        dual_tf = rules.get("dual_tf", {})
        for sub in ("enabled", "htf_timeframe", "ltf_timeframe"):
            if sub not in dual_tf:
                errors.append(f"rules.json: dual_tf missing field '{sub}'")

    cfg_path = project_root / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"config.json parse error: {e}")
            cfg = {}
        accounts = cfg.get("accounts", [])
        if not isinstance(accounts, list):
            errors.append("config.json: 'accounts' must be a list")
        for i, acc in enumerate(accounts):
            if not acc.get("id"):
                errors.append(f"config.json: accounts[{i}] missing 'id'")
            if not acc.get("data_path"):
                errors.append(f"config.json: accounts[{i}] missing 'data_path'")

    return errors


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="OMNI-ICT autonomy supervisor")
    p.add_argument("--only", nargs="+", help="run only these services by name")
    p.add_argument("--poll", type=float, default=2.0, help="supervise poll interval (s)")
    p.add_argument("--grace", type=float, default=5.0, help="graceful stop timeout (s)")
    p.add_argument("--status", action="store_true", help="print state and exit")
    p.add_argument("--stop", action="store_true", help="stop all tracked children and exit")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.status:
        return _print_status()
    if args.stop:
        return _stop_all(args.grace)

    # License check (owner bypass key skips this instantly)
    try:
        import license as _lic
        _lic.check(raise_on_fail=True)
    except SystemExit:
        return 1
    except Exception as e:
        log.error("License module error: %s", e)
        return 1

    cfg_errors = _validate_configs(PROJECT_ROOT)
    if cfg_errors:
        for err in cfg_errors:
            log.error("config validation: %s", err)
        log.error("aborting — fix config errors before starting services")
        return 2

    specs = _default_specs(PROJECT_ROOT)
    if args.only:
        specs = [s for s in specs if s.name in set(args.only)]
        if not specs:
            print(f"no services match --only {args.only}", file=sys.stderr)
            return 2
    return supervise(specs, poll_s=args.poll, grace_s=args.grace)


if __name__ == "__main__":
    sys.exit(main())
