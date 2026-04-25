# OMNI-ICT — Weekly Autonomy Operator Guide

Goal: **run OMNI-ICT unattended, week after week, with minimal manual touch.**
This document is the single source of truth for starting, stopping, verifying,
and recovering the stack.

---

## 1. What runs

Three long-lived services, supervised by one watchdog:

| Service        | Script                   | Purpose                                         |
|----------------|--------------------------|-------------------------------------------------|
| `server`       | `server.py` (uvicorn)    | FastAPI dashboard + API at http://127.0.0.1:8787 |
| `orchestrator` | `python/orchestrator.py` | Per-cycle SMC analysis → writes `shared/signals.json` + `pine/omni_pine_overlay.pine` |
| `auto_trader`  | `python/auto_trader.py`  | MT5 executor (reads signals, places orders)     |
| `watchdog`     | `python/watchdog.py`     | Supervises the three above; restarts on failure |

Outputs the rest of the stack consumes:

- `shared/signals.json` — atomic JSON consumed by the MT5 indicator
  `mql5/OmniSignalOverlay.mq5` (polls every 3 s).
- `pine/omni_pine_overlay.pine` — copy-paste TradingView indicator.
- `logs/` — one file per service: `server.log`, `orchestrator.log`,
  `auto_trader.log`, plus `watchdog_state.json` for introspection.

---

## 2. First-time setup (one-time)

```bash
cd ~/omni-ict
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Then sanity-check the engines:

```bash
cd python
python3 -m pytest tests/ -v
python3 orchestrator.py --dry-run --symbols EURUSD
```

The dry-run should print a `CycleResult` with `errors: []` and write
`shared/signals.json` + `pine/omni_pine_overlay.pine`.

---

## 3. Weekly Monday start (manual)

The simplest way — just double-click in Finder, or from Terminal:

```bash
cd ~/omni-ict
./start_autonomous.command
```

This activates the venv (if present), validates `rules.json`, and starts
`watchdog.py`. Leave the Terminal window open; closing it (or Ctrl-C) stops
the watchdog, which gracefully stops every child.

Verify everything is up:

```bash
python3 python/watchdog.py --status
```

Expected output:

```json
{
  "ts": "...",
  "services": {
    "server":       { "pid": 1234, "alive": true, "restarts": 1, ... },
    "orchestrator": { "pid": 1235, "alive": true, "restarts": 1, ... },
    "auto_trader":  { "pid": 1236, "alive": true, "restarts": 1, ... }
  }
}
```

Open the dashboard: http://127.0.0.1:8787

---

## 4. Fully-automatic start (LaunchAgent — no double-click needed)

Installs a macOS LaunchAgent that starts the watchdog at login and restarts it
if it ever crashes.

```bash
cd ~/omni-ict
# 1. Edit paths in com.omni.ict.autonomy.plist to match your install.
#    (The defaults assume /Users/chris/omni-ict — replace if different.)
open com.omni.ict.autonomy.plist

# 2. Install it.
cp com.omni.ict.autonomy.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.omni.ict.autonomy.plist
launchctl start com.omni.ict.autonomy
```

To pause autonomy for a week (e.g. you're on vacation):

```bash
launchctl stop com.omni.ict.autonomy
launchctl unload ~/Library/LaunchAgents/com.omni.ict.autonomy.plist
```

Re-enable with the two `launchctl load/start` lines from step 2.

---

## 5. Weekly checklist (takes < 5 min)

Run every Monday (or automate with a cron — the plist has a commented example):

1. **Confirm all services alive**
   ```bash
   python3 python/watchdog.py --status
   ```
2. **Eyeball logs for crashes / tracebacks**
   ```bash
   tail -n 50 logs/orchestrator.log
   tail -n 50 logs/auto_trader.log
   tail -n 50 logs/server.log
   ```
3. **Check `signals.json` is fresh** (should update every orchestrator cycle)
   ```bash
   jq .generated_at shared/signals.json
   ```
4. **Run the regression suite against engines** (non-disruptive)
   ```bash
   cd python && python3 -m pytest tests/ -q
   ```
5. **Review previous week's trades in dashboard** — navigate to
   http://127.0.0.1:8787 and export the P&L CSV.

---

## 6. Turning features on/off

All feature flags live in `python/rules.json`:

```jsonc
{
  "dual_tf":  { "enabled": false, ... },   // Phase 2 dual-TF selector
  "scaling":  { "enabled": false, ... },   // Phase 2 position scaling
  "smart_trail": { "enabled": true, ... } // Phase 1 trailing stops
}
```

Recommended enablement order, one week apart, with paper-forward first:

1. **Week 1**: `smart_trail.enabled = true` (already rolled out).
2. **Week 2**: `dual_tf.enabled = true`, leave `scaling.enabled = false`.
   Review signals-only behaviour on dashboard for 5 trading days.
3. **Week 3**: `scaling.enabled = true`. Watch `auto_trader.log` for ADDs and
   REDUCEs firing as expected.

Rollback = flip the flag back to `false` and restart just the orchestrator:

```bash
launchctl kickstart -k gui/$UID/com.omni.ict.autonomy   # LaunchAgent users
# or for the plain command launcher:
python3 python/watchdog.py --stop && ./start_autonomous.command
```

---

## 7. Recovery playbook

### 7.1 Watchdog is stopped (no `watchdog_state.json`, services down)

```bash
cd ~/omni-ict && ./start_autonomous.command
```

If using LaunchAgent:

```bash
launchctl start com.omni.ict.autonomy
```

### 7.2 One service keeps dying

The watchdog keeps a per-process restart counter. After 20 failed restarts it
leaves the service stopped and logs `exceeded max_restarts`. Diagnose with:

```bash
tail -n 100 logs/<service>.log
```

Typical causes:

- **server** fails → port 8787 already in use. Either kill the other process
  or change the port in `watchdog.py → _default_specs()`.
- **orchestrator** fails → most commonly `rules.json` parse error, or an MT5
  connection issue. Run `python3 orchestrator.py --dry-run` to test the
  pipeline without MT5.
- **auto_trader** fails → MT5 terminal not running, or credentials missing
  from `config.json`. Open MT5 and log in first.

### 7.3 Signals stopped updating

1. Check the orchestrator is alive: `python3 python/watchdog.py --status`.
2. `jq .generated_at shared/signals.json` — should be recent (< 2 cycles old).
3. If old, `tail -n 50 logs/orchestrator.log` for recent errors.
4. Force a fresh cycle: `python3 python/orchestrator.py --dry-run`.

### 7.4 MT5 overlay shows nothing

- Confirm `shared/signals.json` has `signals[...]` with `direction ∈
  {BULL,BEAR}` and numeric `entry_price`/`sl`.
- Confirm the path inside `OmniSignalOverlay.mq5` (`InpSignalsFile`) matches
  where the orchestrator writes. Default is MT5/Files relative; if you point
  it at the `shared/` folder, set `FILE_COMMON` symlink.
- Reload the indicator: right-click chart → Indicators list → select OMNI →
  OK.

### 7.5 Everything is weird — nuclear reset

Stops all services, clears transient state, restarts:

```bash
cd ~/omni-ict
python3 python/watchdog.py --stop
rm -f logs/watchdog_state.json shared/signals.json
./start_autonomous.command
```

Trade history, `rules.json`, `config.json`, and open positions are **not**
touched by this reset.

---

## 8. Scheduled weekly ops (optional — zero-touch)

You can schedule common weekly tasks via `launchd` so you don't even have to
log in. See comments inside `com.omni.ict.autonomy.plist` for the
`StartCalendarInterval` pattern; or add one-off cron jobs like:

```cron
# Every Sunday 23:55 UTC — rotate logs and archive last week's signals
55 23 * * 0  cd ~/omni-ict && mv logs logs.$(date -u +\%Y-\%m-\%d) && mkdir logs
```

---

## 9. Where to look when debugging

| Symptom                        | File to open                                   |
|--------------------------------|------------------------------------------------|
| Service restarted              | `logs/<service>.log` (tail)                     |
| Watchdog confused              | `logs/watchdog_state.json`                      |
| Signal shape wrong             | `shared/signals.json` (pretty-print with `jq`)  |
| Pine overlay broken            | `pine/omni_pine_overlay.pine`                   |
| Detection engine bug           | `python/smc_engine.py` + `tests/test_smc_engine.py` |
| Selector picked wrong side     | `python/dual_tf_selector.py` + its tests        |
| Sizing decision off            | `python/scaling_engine.py` + its tests          |
| Trail stop misbehaviour        | `python/smart_trailing_stop.py`                 |
| Rule values                    | `python/rules.json`                             |

---

## 10. FAQ

**Q: Do I need to restart anything after editing `rules.json`?**
A: Yes — the orchestrator loads rules at cycle start. Safest path:
`python3 python/watchdog.py --stop && ./start_autonomous.command`.

**Q: Will the MT5 overlay read a half-written signals file?**
A: No. `signal_writers.write_signals_json()` writes to `<path>.tmp` then
`os.replace()`s it in. `os.replace` is atomic on macOS/APFS.

**Q: What happens if my Mac goes to sleep?**
A: LaunchAgent resumes the watchdog on wake; MT5 runs in its own process
independent of OMNI. Your order state on the broker side is preserved.

**Q: How do I add a new symbol?**
A: Add it to `rules.watchlist` in `rules.json`, then restart the watchdog.
No code changes required.
