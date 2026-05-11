"""
agent_base.py — Base class for all OMNI autonomous agents.

Every agent:
  · Declares a GOAL (what it's optimising for)
  · Declares a DOMAIN (which business it belongs to)
  · Declares HANDLES (task types it can process)
  · Runs an async loop: sense → decide → act → report
  · Can self-task (submit new tasks for itself or others)
  · Publishes heartbeats so the swarm knows it's alive

Usage
-----
    class MyAgent(BaseAgent):
        NAME   = "my_agent"
        GOAL   = "Do X optimally"
        DOMAIN = "SIGNAL_INTELLIGENCE"
        HANDLES = ["DO_X", "DO_Y"]

        async def _run_cycle(self) -> dict:
            ... do work ...
            return {"done": True}

        async def _handle_task(self, task: Task) -> dict:
            ... handle inbound task ...
            return {"result": ...}
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from agent_bus import AgentBus, Task

log = logging.getLogger("agent_base")


class BaseAgent(ABC):
    NAME:    str = "base_agent"
    GOAL:    str = "undefined"
    DOMAIN:  str = "OPERATIONS"
    HANDLES: list[str] = []

    # How often the agent's own cycle runs (override per agent)
    CYCLE_INTERVAL_S: float = 60.0
    # Max consecutive errors before backing off
    MAX_ERRORS: int = 5

    def __init__(self):
        self.bus       = AgentBus()
        self.log       = logging.getLogger(self.NAME)
        self._stop_evt = asyncio.Event()
        self._errors   = 0
        self._cycles   = 0
        self._started  = datetime.now(timezone.utc).isoformat()

    # ── Public control ────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main entry point — runs until stop() is called."""
        self.log.info("starting  goal=%r  domain=%s", self.GOAL, self.DOMAIN)
        self.bus.heartbeat(self.NAME, "starting", {"goal": self.GOAL})

        cycle_task = asyncio.create_task(self._cycle_loop())
        inbox_task = asyncio.create_task(self._inbox_loop())

        try:
            await asyncio.gather(cycle_task, inbox_task)
        except asyncio.CancelledError:
            cycle_task.cancel()
            inbox_task.cancel()
        finally:
            self.bus.heartbeat(self.NAME, "stopped")
            self.log.info("stopped")

    def stop(self) -> None:
        self._stop_evt.set()

    # ── Self-tasking helpers ──────────────────────────────────────────────────

    def self_task(self, task_type: str, payload: dict = None,
                  delay_s: float = 0, priority: int = 3) -> str:
        """Submit a task addressed to self — used for deferred or retry work."""
        return self.bus.submit(task_type, from_agent=self.NAME, to=self.NAME,
                               payload=payload, priority=priority,
                               ttl_s=max(600, int(delay_s) + 300))

    def broadcast(self, task_type: str, payload: dict = None, priority: int = 3) -> str:
        """Broadcast a task for any capable agent to pick up."""
        return self.bus.submit(task_type, from_agent=self.NAME, to="broadcast",
                               payload=payload, priority=priority)

    def send(self, agent_name: str, task_type: str, payload: dict = None,
             priority: int = 3) -> str:
        return self.bus.submit(task_type, from_agent=self.NAME, to=agent_name,
                               payload=payload, priority=priority)

    def event(self, event_type: str, data: dict = None) -> None:
        self.bus.emit_event(self.NAME, event_type, data)

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _cycle_loop(self) -> None:
        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            try:
                result = await self._run_cycle()
                self._errors = 0
                self._cycles += 1
                self.bus.heartbeat(self.NAME, "running", {
                    "cycles":    self._cycles,
                    "last_cycle": datetime.now(timezone.utc).isoformat(),
                    "last_result": result or {},
                })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._errors += 1
                self.log.exception("cycle error #%d: %s", self._errors, exc)
                self.bus.heartbeat(self.NAME, "error", {"error": str(exc), "count": self._errors})
                self.event("CYCLE_ERROR", {"error": str(exc)})
                if self._errors >= self.MAX_ERRORS:
                    backoff = min(self._errors * 30, 300)
                    self.log.warning("too many errors — backing off %ds", backoff)
                    await asyncio.sleep(backoff)
                    self._errors = 0

            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, self.CYCLE_INTERVAL_S - elapsed)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass

    async def _inbox_loop(self) -> None:
        """Poll the bus for tasks addressed to this agent."""
        while not self._stop_evt.is_set():
            if self.HANDLES:
                task = self.bus.claim_next(self.NAME, self.HANDLES)
                if task:
                    await self._process_task(task)
                    continue  # poll again immediately while tasks available
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def _process_task(self, task: Task) -> None:
        self.log.debug("handling task %s type=%s", task.id, task.type)
        try:
            result = await self._handle_task(task)
            self.bus.complete(task.id, result)
        except asyncio.CancelledError:
            self.bus.fail(task.id, "cancelled")
            raise
        except Exception as exc:
            self.log.exception("task %s failed: %s", task.id, exc)
            self.bus.fail(task.id, str(exc))

    # ── To override in subclass ───────────────────────────────────────────────

    @abstractmethod
    async def _run_cycle(self) -> dict:
        """Main periodic work. Return a summary dict."""

    async def _handle_task(self, task: Task) -> dict:
        """Handle an inbound task from the bus. Override per agent."""
        self.log.debug("no handler for task type=%s", task.type)
        return {"status": "unhandled"}
