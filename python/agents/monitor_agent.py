"""
monitor_agent.py — OPERATIONS business: system health watchdog.

GOAL: Ensure all swarm agents and supporting services are healthy.
      Alert on failures, trigger restarts via watchdog, escalate to Telegram.

Tasks handled:
  SERVICE_ERROR     — from any agent reporting an error
  HEALTH_CHECK      — on-demand health report
  RESTART_SERVICE   — request service restart via launchctl

Self-tasks/broadcasts emitted:
  HEALTH_REPORT     — periodic broadcast of full system state
  CRITICAL_ALERT    — when multiple services fail or drawdown is extreme
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_base import BaseAgent, Task

HERE         = Path(__file__).resolve().parent.parent
PROJECT_ROOT = HERE.parent
LOG_DIR      = PROJECT_ROOT / "logs"
WATCHDOG_STATE = LOG_DIR / "watchdog_state.json"
ALERTS_PATH    = LOG_DIR / "alerts.json"


class MonitorAgent(BaseAgent):
    NAME   = "monitor_agent"
    GOAL   = "Keep all OMNI services healthy; alert and auto-recover on failures"
    DOMAIN = "OPERATIONS"
    HANDLES = ["SERVICE_ERROR", "HEALTH_CHECK", "RESTART_SERVICE"]
    CYCLE_INTERVAL_S = 120.0  # health check every 2 min

    def __init__(self):
        super().__init__()
        self._error_counts: dict = {}   # service → count
        self._last_alert_ts: float = 0.0
        self._alert_cooldown_s = 300.0  # 5 min between same alerts

    async def _run_cycle(self) -> dict:
        health = self._build_health_report()
        self.event("HEALTH_REPORT", health)  # event log, not a task — avoids queue bloat

        # Check for dead watchdog services
        dead = [s for s, st in health.get("watchdog", {}).items()
                if not st.get("alive", True)]
        if dead:
            for svc in dead:
                self._handle_dead_service(svc)

        # Check signal freshness
        sig_age = health.get("signal_age_s", 9999)
        if sig_age > 300:
            self.log.warning("signals are stale: age=%ds", sig_age)
            self.send("signal_agent", "FORCE_SIGNAL_CYCLE", {
                "reason": f"stale_signals_age={sig_age}s"
            }, priority=2)

        # Check for stalled learning
        if health.get("last_learning_minutes", 9999) > 120:
            self.send("learning_agent", "FORCE_RETRAIN", {
                "reason": "learning_stalled"
            }, priority=3)

        return health

    async def _handle_task(self, task: Task) -> dict:
        if task.type == "SERVICE_ERROR":
            return self._record_error(task.payload)
        if task.type == "HEALTH_CHECK":
            return self._build_health_report()
        if task.type == "RESTART_SERVICE":
            return self._restart_service(task.payload.get("service", ""))
        return {"status": "unknown"}

    # ── Health report ─────────────────────────────────────────────────────────

    def _build_health_report(self) -> dict:
        report = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "watchdog": {},
            "agents": {},
            "signal_age_s": 9999,
            "last_learning_minutes": 9999,
        }

        # Watchdog services
        try:
            if WATCHDOG_STATE.exists():
                ws = json.loads(WATCHDOG_STATE.read_text())
                report["watchdog"] = ws.get("services", {})
        except Exception:
            pass

        # Agent states
        try:
            agent_states = self.bus.get_agent_states()
            now = datetime.now(timezone.utc)
            for name, state in agent_states.items():
                ts_str = state.get("ts", "")
                age_s = 9999
                if ts_str:
                    try:
                        from datetime import datetime as dt
                        ts = dt.fromisoformat(ts_str)
                        age_s = (now - ts).total_seconds()
                    except Exception:
                        pass
                report["agents"][name] = {
                    "status":  state.get("status"),
                    "age_s":   age_s,
                    "healthy": age_s < 600 and state.get("status") not in ("error", "stopped"),
                }
        except Exception:
            pass

        # Signal freshness
        try:
            signals_path = PROJECT_ROOT / "shared" / "signals.json"
            if signals_path.exists():
                data = json.loads(signals_path.read_text())
                gen_at = data.get("generated_at", "")
                if gen_at:
                    from datetime import datetime as dt
                    ts = dt.fromisoformat(gen_at)
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    report["signal_age_s"] = int(age)
        except Exception:
            pass

        # Learning log freshness
        try:
            if (LOG_DIR / "learning.log").exists():
                mtime = (LOG_DIR / "learning.log").stat().st_mtime
                age_min = (time.time() - mtime) / 60.0
                report["last_learning_minutes"] = int(age_min)
        except Exception:
            pass

        return report

    def _record_error(self, payload: dict) -> dict:
        source = payload.get("source", "unknown")
        errors = payload.get("errors", [])
        self._error_counts[source] = self._error_counts.get(source, 0) + len(errors)
        total = self._error_counts[source]
        self.log.warning("[%s] errors: %s (total=%d)", source, errors, total)

        # Write to alerts.json
        try:
            alerts = []
            if ALERTS_PATH.exists():
                alerts = json.loads(ALERTS_PATH.read_text())
            for err in errors:
                alerts.append({
                    "ts":      datetime.now(timezone.utc).isoformat(),
                    "service": source,
                    "code":    str(err),
                })
            tmp = ALERTS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(alerts[-200:], indent=2))
            os.replace(tmp, ALERTS_PATH)
        except Exception:
            pass

        # Escalate to Telegram if critical
        if total >= 5:
            self._send_telegram_alert(f"⚠️ {source}: {total} errors. Last: {errors[-1] if errors else '?'}")
            self._error_counts[source] = 0  # reset after alert

        return {"source": source, "total_errors": total}

    def _handle_dead_service(self, service: str) -> None:
        self.log.warning("service %s appears dead — requesting restart", service)
        count = self._error_counts.get(f"dead_{service}", 0) + 1
        self._error_counts[f"dead_{service}"] = count
        if count <= 3:  # max 3 auto-restarts
            self._restart_service(service)
        elif count == 4:
            self._send_telegram_alert(f"🚨 {service} crashed {count} times — NOT auto-restarting")

    def _restart_service(self, service: str) -> dict:
        self.log.info("attempting restart of service: %s", service)
        try:
            uid = os.getuid()
            label = f"com.omni.ict.autonomy"
            result = subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                capture_output=True, text=True, timeout=10,
            )
            msg = result.stdout.strip() or result.stderr.strip()
            self.log.info("restart %s: %s", service, msg)
            return {"restarted": True, "service": service, "msg": msg}
        except Exception as e:
            self.log.warning("restart failed for %s: %s", service, e)
            return {"restarted": False, "error": str(e)}

    def _send_telegram_alert(self, msg: str) -> None:
        now = time.time()
        if now - self._last_alert_ts < self._alert_cooldown_s:
            return
        self._last_alert_ts = now
        try:
            from telegram_bot import send_telegram
            send_telegram(f"[MONITOR] {msg}")
        except Exception:
            self.log.warning("telegram alert failed: %s", msg)
