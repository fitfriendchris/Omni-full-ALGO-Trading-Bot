"""
agent_bus.py — File-based event/task bus for the OMNI agent swarm.

All agents read/write through this module.  The bus state lives in
shared/agent_tasks.json (task queue) and shared/agent_events.json (event
log).  File locking ensures no corruption under concurrent access.

Task lifecycle:  pending → claimed → done | failed | timeout

Public API
----------
    AgentBus.submit(task_type, payload, to="broadcast", priority=3, ttl_s=300)
    AgentBus.claim_next(agent_name, task_types)   → Task | None
    AgentBus.complete(task_id, result=None)
    AgentBus.fail(task_id, error)
    AgentBus.emit_event(agent_name, event_type, data)
    AgentBus.get_tasks(status=None, to=None)     → list[Task]
    AgentBus.get_recent_events(n=20)             → list[dict]
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("agent_bus")

HERE         = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
SHARED_DIR   = PROJECT_ROOT / "shared"
TASKS_PATH   = SHARED_DIR / "agent_tasks.json"
EVENTS_PATH  = SHARED_DIR / "agent_events.json"
STATE_PATH   = SHARED_DIR / "agent_state.json"
MAX_EVENTS   = 200
MAX_TASKS    = 500     # prune oldest done tasks above this
LOCK_PATH    = None    # set lazily


@dataclass
class Task:
    id:         str
    type:       str
    from_agent: str
    to:         str          # agent name or "broadcast"
    priority:   int          # 1=highest, 5=lowest
    payload:    dict
    status:     str          # pending | claimed | done | failed | timeout
    created_at: str
    deadline:   float        # unix timestamp, 0 = no deadline
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    result:     Optional[dict] = None
    error:      Optional[str] = None
    completed_at: Optional[str] = None

    @property
    def is_expired(self) -> bool:
        return self.deadline > 0 and time.time() > self.deadline

    @classmethod
    def new(cls, type: str, from_agent: str, to: str = "broadcast",
            priority: int = 3, payload: dict = None, ttl_s: int = 300) -> "Task":
        now = datetime.now(timezone.utc)
        return cls(
            id=str(uuid.uuid4())[:12],
            type=type,
            from_agent=from_agent,
            to=to,
            priority=priority,
            payload=payload or {},
            status="pending",
            created_at=now.isoformat(),
            deadline=time.time() + ttl_s if ttl_s > 0 else 0,
        )


@contextmanager
def _bus_lock():
    """Cross-process exclusive lock for atomic read-modify-write on the task queue."""
    lock_file = SHARED_DIR / ".agent_bus.lock"
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _read_json(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        return []


def _write_json(path: Path, data: list) -> None:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    # Use a PID+UUID suffix so concurrent writers don't clobber each other's tmp
    tmp = path.parent / f".{path.stem}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(data, f, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


class AgentBus:
    """Thread-safe file-based task bus. Instantiate once per agent process."""

    def __init__(self):
        SHARED_DIR.mkdir(parents=True, exist_ok=True)

    # ── Task API ──────────────────────────────────────────────────────────────

    def submit(self, task_type: str, from_agent: str, payload: dict = None,
               to: str = "broadcast", priority: int = 3, ttl_s: int = 300) -> str:
        task = Task.new(task_type, from_agent, to, priority, payload, ttl_s)
        tasks = _read_json(TASKS_PATH)
        tasks.append(asdict(task))
        # Prune oldest completed tasks if over cap
        if len(tasks) > MAX_TASKS:
            done = [t for t in tasks if t["status"] in ("done", "failed", "timeout")]
            done.sort(key=lambda t: t.get("created_at", ""))
            cut = len(tasks) - MAX_TASKS
            done_ids = {t["id"] for t in done[:cut]}
            tasks = [t for t in tasks if t["id"] not in done_ids]
        _write_json(TASKS_PATH, tasks)
        log.debug("submitted task %s type=%s to=%s", task.id, task_type, to)
        return task.id

    def claim_next(self, agent_name: str, task_types: list[str]) -> Optional[Task]:
        # Full exclusive lock for atomic read-claim-write
        with _bus_lock():
            tasks = _read_json(TASKS_PATH)
            now_ts = time.time()
            now_iso = datetime.now(timezone.utc).isoformat()

            # Expire timed-out tasks
            for t in tasks:
                if t["status"] == "pending" and t.get("deadline", 0) > 0:
                    if now_ts > t["deadline"]:
                        t["status"] = "timeout"

            # Find best pending task for this agent
            candidates = [
                t for t in tasks
                if t["status"] == "pending"
                and t["type"] in task_types
                and (t["to"] in ("broadcast", agent_name))
            ]
            if not candidates:
                _write_json(TASKS_PATH, tasks)
                return None

            # Sort: priority ASC (1=urgent), then created_at ASC (oldest first)
            candidates.sort(key=lambda t: (t["priority"], t["created_at"]))
            chosen = candidates[0]

            # Mark claimed atomically
            for t in tasks:
                if t["id"] == chosen["id"]:
                    t["status"] = "claimed"
                    t["claimed_by"] = agent_name
                    t["claimed_at"] = now_iso
                    break

            _write_json(TASKS_PATH, tasks)
        return Task(**chosen)

    def complete(self, task_id: str, result: dict = None) -> None:
        self._update_task(task_id, {
            "status": "done",
            "result": result or {},
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

    def fail(self, task_id: str, error: str) -> None:
        self._update_task(task_id, {
            "status": "failed",
            "error": error,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

    def _update_task(self, task_id: str, updates: dict) -> None:
        tasks = _read_json(TASKS_PATH)
        for t in tasks:
            if t["id"] == task_id:
                t.update(updates)
                break
        _write_json(TASKS_PATH, tasks)

    def get_tasks(self, status: str = None, to: str = None) -> list[dict]:
        tasks = _read_json(TASKS_PATH)
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        if to:
            tasks = [t for t in tasks if t.get("to") in (to, "broadcast")]
        return tasks

    # ── Event API (append-only log) ───────────────────────────────────────────

    def emit_event(self, agent_name: str, event_type: str, data: dict = None) -> None:
        events = _read_json(EVENTS_PATH)
        events.append({
            "ts":    datetime.now(timezone.utc).isoformat(),
            "agent": agent_name,
            "type":  event_type,
            "data":  data or {},
        })
        if len(events) > MAX_EVENTS:
            events = events[-MAX_EVENTS:]
        _write_json(EVENTS_PATH, events)

    def get_recent_events(self, n: int = 20) -> list[dict]:
        return _read_json(EVENTS_PATH)[-n:]

    # ── Agent state heartbeat ─────────────────────────────────────────────────

    def heartbeat(self, agent_name: str, status: str, metadata: dict = None) -> None:
        state = {}
        try:
            if STATE_PATH.exists():
                state = json.loads(STATE_PATH.read_text())
        except Exception:
            pass
        state[agent_name] = {
            "status":   status,
            "ts":       datetime.now(timezone.utc).isoformat(),
            **(metadata or {}),
        }
        tmp = STATE_PATH.parent / f".{STATE_PATH.stem}.{os.getpid()}.tmp"
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.replace(tmp, STATE_PATH)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def get_agent_states(self) -> dict:
        try:
            if STATE_PATH.exists():
                return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
        return {}
