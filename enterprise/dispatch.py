#!/usr/bin/env python3
"""
Enterprise Agent Dispatch System
Routes tasks from CEO to workers with proper context, constraints, and monitoring.
"""

import json
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass

ENTERPRISE_DIR = Path("~/Omni-full-ALGO-Trading-Bot/enterprise").expanduser()
LOGS_DIR = ENTERPRISE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

@dataclass
class TaskDispatch:
    task_id: str
    target: str
    priority: str
    instruction: str
    tools: list
    deadline_minutes: int
    context_files: list


def log_event(event: str):
    ts = datetime.now(timezone.utc).isoformat()
    with open(LOGS_DIR / "dispatch.log", "a") as f:
        f.write(f"{ts} {event}\n")


def load_agent_profile(agent_name: str) -> dict:
    path = ENTERPRISE_DIR / "agents" / f"{agent_name}.md"
    if path.exists():
        return {"profile": path.read_text(), "exists": True}
    return {"exists": False}


def dispatch_task(task: TaskDispatch) -> str:
    """Spawn a subagent with proper context."""
    log_event(f"DISPATCH {task.task_id} -> {task.target} [{task.priority}]")
    
    # Build the prompt
    profile = load_agent_profile(task.target)
    prompt = f"""[ROUTING_TARGET: {task.target}]
[TASK_ID: {task.task_id}]
[PRIORITY: {task.priority}]
[DEADLINE: {task.deadline_minutes} minutes]
[TOOLS: {', '.join(task.tools)}]

You are {task.target}, reporting to VP-Quant-Trading/CEA.
Your profile: {profile.get('profile', 'No profile found')}

CONTEXT FROM LIBRARIAN:
- Check enterprise/knowledge_base/ for relevant project history
- Query known issues before starting

YOUR INSTRUCTION:
{task.instruction}

CONSTRAINTS:
- Only use tools listed above
- Report progress every 5 minutes if task >10 min
- Output format: [STATUS], [CHANGES], [TESTS], [SAFETY], [NOTES]
- If blocked for >5 min, escalate to CEA
"""
    
    # For now, print the prompt (would call sessions_spawn in real implementation)
    print(f"\n{'='*60}")
    print(f"DISPATCH: {task.task_id} -> {task.target}")
    print(f"{'='*60}")
    print(prompt[:1000] + "..." if len(prompt) > 1000 else prompt)
    print(f"{'='*60}\n")
    
    return task.task_id


def main():
    if len(sys.argv) < 3:
        print("Usage: python dispatch.py <agent> '<instruction>' [priority] [deadline]")
        print("Agents: quant-worker-1, quant-worker-2, quant-worker-3, product-worker-1, product-worker-2")
        sys.exit(1)
    
    agent = sys.argv[1]
    instruction = sys.argv[2]
    priority = sys.argv[3] if len(sys.argv) > 3 else "Med"
    deadline = int(sys.argv[4]) if len(sys.argv) > 4 else 30
    
    task = TaskDispatch(
        task_id=f"T-{datetime.now(timezone.utc).strftime('%H%M%S')}",
        target=agent,
        priority=priority,
        instruction=instruction,
        tools=["read", "edit", "write", "exec"],
        deadline_minutes=deadline,
        context_files=[],
    )
    
    dispatch_task(task)


if __name__ == "__main__":
    main()
