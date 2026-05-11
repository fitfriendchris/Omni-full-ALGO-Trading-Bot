#!/usr/bin/env python3
"""
OMNI Project Monitor — Autonomous Multi-Project Health & Audit System

Monitors all Chris's ventures continuously:
  • APEX Coaching (production)
  • Omni ICT Algo Bot (critical)
  • THE CIRCLE (development)
  • Vape Vending (research)
  • Digital Products (maintenance)

Runs health checks, detects issues, and spawns subagent audits.
"""

import json
import os
import sys
import time
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict

# ── PATHS ────────────────────────────────────────────────────────────────────
MONITOR_DIR = Path(__file__).parent.resolve()
LOG_DIR = MONITOR_DIR / "logs"
REPORT_DIR = MONITOR_DIR / "reports"
PROJECTS_FILE = MONITOR_DIR / "projects.json"
STATE_FILE = MONITOR_DIR / "monitor_state.json"

LOG_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)

# ── LOGGING ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "monitor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("OmniMonitor")

# ── PROJECT MODEL ────────────────────────────────────────────────────────────
@dataclass
class CheckResult:
    check_name: str
    status: str      # "ok", "warning", "critical"
    message: str
    timestamp: str
    details: Optional[dict] = None

@dataclass
class ProjectHealth:
    project_id: str
    project_name: str
    overall_status: str   # "healthy", "warning", "critical"
    checks: List[CheckResult]
    last_check: str
    next_audit_due: Optional[str] = None

@dataclass
class MonitorReport:
    timestamp: str
    projects: List[ProjectHealth]
    issues_found: int
    audits_triggered: int
    summary: str

# ── CHECK RUNNERS ────────────────────────────────────────────────────────────

def check_git_sync(repo_path: str, branch: str = "main") -> CheckResult:
    """Check if local repo is clean and up to date."""
    try:
        path = Path(repo_path).expanduser()
        if not path.exists():
            return CheckResult("git_sync", "critical", f"Repo path not found: {path}",
                               _now(), None)
        
        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path, capture_output=True, text=True, timeout=10
        )
        dirty = bool(result.stdout.strip())
        
        # Check if behind remote
        subprocess.run(["git", "fetch", "origin"], cwd=path, capture_output=True, timeout=30)
        result2 = subprocess.run(
            ["git", "rev-list", f"HEAD...origin/{branch}", "--count"],
            cwd=path, capture_output=True, text=True, timeout=10
        )
        behind = int(result2.stdout.strip()) if result2.stdout.strip().isdigit() else 0
        
        if dirty and behind > 0:
            return CheckResult("git_sync", "critical",
                f"{behind} commits behind + uncommitted changes", _now(),
                {"behind": behind, "dirty_files": len(result.stdout.strip().split("\n"))})
        elif dirty:
            return CheckResult("git_sync", "warning",
                f"Uncommitted changes ({len(result.stdout.strip().split(chr(10)))} files)", _now())
        elif behind > 0:
            return CheckResult("git_sync", "warning",
                f"{behind} commits behind origin/{branch}", _now(), {"behind": behind})
        else:
            return CheckResult("git_sync", "ok", "Clean and up to date", _now())
    except Exception as e:
        return CheckResult("git_sync", "critical", f"Git check failed: {e}", _now())


def check_mt5_connection(data_path: str, max_stale: int = 60) -> CheckResult:
    """Check MT5 data freshness."""
    try:
        path = Path(data_path).expanduser()
        if not path.exists():
            return CheckResult("mt5_connection", "critical",
                f"MT5 data file missing: {path}", _now())
        
        age = time.time() - path.stat().st_mtime
        if age > max_stale:
            return CheckResult("mt5_connection", "critical",
                f"Data stale: {age:.0f}s old (max {max_stale}s)", _now(), {"age_seconds": age})
        elif age > max_stale / 2:
            return CheckResult("mt5_connection", "warning",
                f"Data getting old: {age:.0f}s", _now(), {"age_seconds": age})
        else:
            return CheckResult("mt5_connection", "ok",
                f"Fresh: {age:.0f}s old", _now(), {"age_seconds": age})
    except Exception as e:
        return CheckResult("mt5_connection", "critical", f"Check failed: {e}", _now())


def check_bot_processes(processes: List[str]) -> CheckResult:
    """Check if expected bot processes are running."""
    try:
        ps = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=10)
        missing = []
        found = []
        for proc in processes:
            if proc in ps.stdout:
                found.append(proc)
            else:
                missing.append(proc)
        
        if missing:
            return CheckResult("bot_processes", "critical" if len(missing) == len(processes) else "warning",
                f"Missing: {', '.join(missing)} | Running: {', '.join(found)}", _now(),
                {"missing": missing, "running": found})
        else:
            return CheckResult("bot_processes", "ok",
                f"All {len(processes)} processes running", _now(), {"running": found})
    except Exception as e:
        return CheckResult("bot_processes", "critical", f"Process check failed: {e}", _now())


def check_account_health(data_path: str, min_balance: float = 10.0) -> CheckResult:
    """Check trading account health from MT5 data."""
    try:
        path = Path(data_path).expanduser()
        with open(path) as f:
            data = json.load(f)
        
        account = data.get("account", {})
        balance = account.get("balance", 0.0)
        equity = account.get("equity", 0.0)
        
        issues = []
        if balance < min_balance:
            issues.append(f"Balance ${balance:.2f} below ${min_balance}")
        if equity < balance * 0.8:
            issues.append(f"Equity ${equity:.2f} is {(1-equity/balance)*100:.1f}% below balance")
        
        if issues:
            return CheckResult("account_health", "critical",
                " | ".join(issues), _now(), {"balance": balance, "equity": equity})
        else:
            return CheckResult("account_health", "ok",
                f"Balance ${balance:.2f} | Equity ${equity:.2f}", _now(),
                {"balance": balance, "equity": equity})
    except Exception as e:
        return CheckResult("account_health", "warning", f"Could not read account: {e}", _now())


def check_deploy_status(url: str) -> CheckResult:
    """Check if a deployed URL is reachable."""
    try:
        import urllib.request
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "OmniMonitor/1.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                return CheckResult("deploy_status", "ok", f"{url} reachable", _now())
            else:
                return CheckResult("deploy_status", "warning",
                    f"{url} returned HTTP {resp.status}", _now())
    except Exception as e:
        return CheckResult("deploy_status", "critical", f"{url} unreachable: {e}", _now())


def check_test_suite(repo_path: str) -> CheckResult:
    """Run the project's test suite."""
    try:
        path = Path(repo_path).expanduser()
        venv_python = path / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else "python3"
        
        result = subprocess.run(
            [python, "-m", "pytest", "python/tests/", "-q", "--tb=no"],
            cwd=path, capture_output=True, text=True, timeout=120
        )
        
        # Parse output for pass/fail
        output = result.stdout + result.stderr
        if "passed" in output:
            parts = output.split("passed")
            count = parts[0].strip().split()[-1] if parts else "?"
            return CheckResult("test_suite", "ok", f"{count} tests passed", _now())
        elif "failed" in output:
            return CheckResult("test_suite", "warning", "Tests failing", _now(),
                {"output": output[-500:]})
        else:
            return CheckResult("test_suite", "warning", "No tests found or error", _now(),
                {"output": output[-500:]})
    except Exception as e:
        return CheckResult("test_suite", "warning", f"Test run failed: {e}", _now())


# ── ORCHESTRATOR ─────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_project_checks(project: dict) -> ProjectHealth:
    """Run all enabled checks for a project."""
    checks = []
    worst_status = "ok"
    
    for check_name, check_cfg in project.get("checks", {}).items():
        if not check_cfg.get("enabled", True):
            continue
        
        result = None
        if check_name == "git_sync":
            result = check_git_sync(project.get("local_path", "~"), check_cfg.get("branch", "main"))
        elif check_name == "mt5_connection":
            result = check_mt5_connection(check_cfg.get("data_path", ""), check_cfg.get("max_stale_seconds", 60))
        elif check_name == "bot_processes":
            result = check_bot_processes(check_cfg.get("processes", []))
        elif check_name == "account_health":
            result = check_account_health(check_cfg.get("data_path", ""), check_cfg.get("min_balance", 10.0))
        elif check_name == "deploy_status":
            result = check_deploy_status(check_cfg.get("url", ""))
        elif check_name == "test_suite":
            result = check_test_suite(project.get("local_path", "~"))
        
        if result:
            checks.append(result)
            if result.status == "critical":
                worst_status = "critical"
            elif result.status == "warning" and worst_status != "critical":
                worst_status = "warning"
    
    # Override status for critical-tier projects
    if project.get("tier") == "critical" and worst_status == "warning":
        worst_status = "critical"  # Elevate warnings for critical projects
    
    return ProjectHealth(
        project_id=project["id"],
        project_name=project["name"],
        overall_status=worst_status,
        checks=checks,
        last_check=_now(),
    )


def should_trigger_audit(project: ProjectHealth, state: dict) -> bool:
    """Determine if an audit subagent should be triggered."""
    if project.overall_status == "critical":
        return True
    if project.overall_status == "warning" and project.project_id in ["apex", "omni"]:
        return True
    
    # Deep audit schedule
    last_audit = state.get("last_audits", {}).get(project.project_id, "")
    if last_audit:
        hours_since = (datetime.now(timezone.utc) - datetime.fromisoformat(last_audit)).total_seconds() / 3600
        return hours_since > 24
    return False


def save_report(report: MonitorReport):
    """Save report to disk."""
    filename = f"report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    path = REPORT_DIR / filename
    with open(path, "w") as f:
        json.dump(asdict(report), f, indent=2, default=str)
    log.info(f"Report saved: {path}")


def load_state() -> dict:
    """Load monitor state."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_audits": {}, "last_alerts": {}, "alert_count": 0}


def save_state(state: dict):
    """Save monitor state."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def run_monitor_cycle() -> MonitorReport:
    """Run one full monitoring cycle."""
    with open(PROJECTS_FILE) as f:
        config = json.load(f)
    
    projects = config.get("projects", [])
    settings = config.get("settings", {})
    state = load_state()
    
    health_results = []
    issues = 0
    audits_triggered = 0
    
    log.info(f"=== Monitor Cycle Started === {len(projects)} projects")
    
    for project in projects:
        log.info(f"Checking {project['name']}...")
        health = run_project_checks(project)
        health_results.append(health)
        
        if health.overall_status != "ok":
            issues += 1
            log.warning(f"  {project['name']}: {health.overall_status.upper()}")
            for check in health.checks:
                if check.status != "ok":
                    log.warning(f"    - {check.check_name}: {check.message}")
        else:
            log.info(f"  {project['name']}: OK")
        
        # Trigger audit if needed
        if should_trigger_audit(health, state):
            log.info(f"  → Audit triggered for {project['name']}")
            state["last_audits"][project["id"]] = _now()
            audits_triggered += 1
            # Subagent spawn would happen here (OpenClaw sessions_spawn)
    
    summary_parts = []
    for h in health_results:
        emoji = {"ok": "✅", "warning": "⚠️", "critical": "🚨"}.get(h.overall_status, "❓")
        summary_parts.append(f"{emoji} {h.project_name}: {h.overall_status.upper()}")
    
    report = MonitorReport(
        timestamp=_now(),
        projects=health_results,
        issues_found=issues,
        audits_triggered=audits_triggered,
        summary="\n".join(summary_parts),
    )
    
    save_report(report)
    save_state(state)
    
    log.info(f"=== Cycle Complete === {issues} issues, {audits_triggered} audits")
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="OMNI Project Monitor")
    p.add_argument("--loop", type=int, default=0, metavar="MIN",
                   help="Run continuously, check every N minutes")
    p.add_argument("--once", action="store_true", help="Run one cycle and exit")
    p.add_argument("--project", help="Check only one project by ID")
    args = p.parse_args()
    
    if args.once or not args.loop:
        report = run_monitor_cycle()
        print("\n" + "="*60)
        print(report.summary)
        print("="*60)
        sys.exit(0 if report.issues_found == 0 else 1)
    
    # Loop mode
    interval = args.loop * 60
    log.info(f"Starting monitor loop: {args.loop}min interval")
    while True:
        try:
            run_monitor_cycle()
        except Exception as e:
            log.exception("Monitor cycle failed")
        time.sleep(interval)
