# Hermes ↔ OpenClaw Bridge Protocol

## Communication Method: File-based IPC
Both agents are on the same MacBook Pro (2018, 16GB RAM). No network calls needed.

## Files

| File | Direction | Purpose |
|---|---|---|
| `shared/hermes_inbox.json` | Hermes → OpenClaw | Tasks from Hermes user |
| `shared/openclaw_outbox.json` | OpenClaw → Hermes | Results from OpenClaw workers |
| `shared/bridge.log` | Bridge internal | Debug log |

## Task Format (hermes_inbox.json)

```json
{
  "id": "T-001",
  "department": "quant|product|infra|general",
  "command": "exact instruction",
  "priority": "Low|Med|High|Critical",
  "deadline_minutes": 30,
  "from": "hermes_user_chat_id"
}
```

## Result Format (openclaw_outbox.json)

```json
{
  "id": "T-001",
  "status": "OK|ERROR|BLOCKED|QUEUED",
  "stdout": "output text",
  "stderr": "error text (if any)",
  "timestamp": "2026-05-06T..."
}
```

## Departments

| Department | Scope | Safety Level |
|---|---|---|
| **quant** | Trading bot tasks | 🔒 HIGH — blocks live trading changes |
| **product** | APEX, Circle, curriculum | 🔒 MED — no direct deploys without approval |
| **infra** | System, files, processes | 🔒 LOW — can run safe commands |
| **general** | Any other request | 🔒 LOW |

## Blocking Rules (quant department)

These commands are BLOCKED and return `status: BLOCKED`:
- Any mention of "live trading", "real money", "paper off", "OMNI_PAPER_MODE=false"
- Any command to modify config.py PAPER_MODE
- Any command to delete .env or modify API keys

## Usage

### From Hermes
```python
# Write a task to inbox
import json
with open("~/Omni-full-ALGO-Trading-Bot/shared/hermes_inbox.json", "w") as f:
    json.dump([{"id": "T-001", "department": "quant", "command": "run backtest on EURUSD"}], f)
```

### From OpenClaw
```python
# Read results from outbox
import json
with open("~/Omni-full-ALGO-Trading-Bot/shared/openclaw_outbox.json") as f:
    results = json.load(f)
```

---

_Last updated: 2026-05-06 01:52 CDT_
