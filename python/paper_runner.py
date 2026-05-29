"""
paper_runner.py — Forward test the sequential ICT engine on the LIVE MT5 feed,
in pure paper mode. NEVER writes omni_cmd.txt, NEVER places a real order.

It reads real broker bars via mt5_connector.get_bars(), runs ict_sequential on
H1(HTF)+M15(LTF), and simulates limit fills / SL / TP management against the
incoming M5 bars — recording R-multiples and paper P&L with the real risk_sizing
rules. This collects honest forward-test evidence on real prices while live
trading stays paused.

Usage:
  ../.venv/bin/python paper_runner.py --once            # one evaluation pass
  ../.venv/bin/python paper_runner.py --loop 60         # every 60s (Ctrl-C to stop)
  ../.venv/bin/python paper_runner.py --status          # print journal summary

State : shared/seq_paper_state.json
Journal: shared/seq_paper_journal.jsonl  (one JSON event per line)
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import List, Optional

import mt5_connector as mc
from smc_engine import Bar
from ict_sequential import evaluate, SequentialConfig
from risk_sizing import (RiskConfig, DayState, size_position, can_open,
                         trade_risk_usd, _value_per_unit)

HERE = os.path.dirname(os.path.abspath(__file__))
SHARED = os.path.normpath(os.path.join(HERE, "..", "shared"))
STATE_PATH = os.path.join(SHARED, "seq_paper_state.json")
JOURNAL_PATH = os.path.join(SHARED, "seq_paper_journal.jsonl")

SYMBOL = "XAUUSD"
HTF_TF, LTF_TF, MGMT_TF = "H1", "M15", "M5"
SPREAD, SLIPPAGE, COMMISSION_PER_LOT = 0.30, 0.10, 7.0
FILL_WINDOW_SEC = 6 * 15 * 60      # ~6 M15 bars to fill a limit, else cancel


# ──────────────────────────────────────────────────────────────────────────────
def _bars(tf: str, n: int = 300) -> List[Bar]:
    raw = mc.get_bars(SYMBOL, tf, n)
    out = []
    for b in raw:
        t = b.get("time_utc")
        if t is None:
            continue
        out.append(Bar(time=float(t), open=b["open"], high=b["high"],
                       low=b["low"], close=b["close"]))
    out.sort(key=lambda x: x.time)
    return out


def _load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH))
        except Exception:
            pass
    acct = mc.get_account_info() or {}
    eq = float(acct.get("equity") or acct.get("balance") or 133.42)
    return {"equity": eq, "start_equity": eq, "pending": None, "open": None,
            "closed": [], "day_start_equity": eq, "day_profit": 0.0,
            "loss_streak": 0, "last_day": "", "last_m5_ts": 0.0}


def _save_state(s: dict) -> None:
    os.makedirs(SHARED, exist_ok=True)
    json.dump(s, open(STATE_PATH, "w"), indent=2)


def _journal(event: dict) -> None:
    os.makedirs(SHARED, exist_ok=True)
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(event) + "\n")


def _roll_day(s: dict) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if s.get("last_day") != today:
        s["last_day"] = today
        s["day_start_equity"] = s["equity"]
        s["day_profit"] = 0.0
        s["loss_streak"] = s.get("loss_streak", 0)  # streak persists across days? reset:
        s["loss_streak"] = 0


# ──────────────────────────────────────────────────────────────────────────────
def tick(verbose: bool = True) -> None:
    cfg = SequentialConfig(symbol=SYMBOL)
    rcfg = RiskConfig()
    s = _load_state()
    _roll_day(s)

    htf = _bars(HTF_TF, 300)
    ltf = _bars(LTF_TF, 300)
    mgmt = _bars(MGMT_TF, 300)
    if len(htf) < 20 or len(ltf) < 30 or not mgmt:
        if verbose:
            print("waiting for data…")
        return

    price = mgmt[-1].close
    new_m5 = [b for b in mgmt if b.time > s.get("last_m5_ts", 0.0)]

    # ── 1) Manage an OPEN paper trade against new M5 bars ──
    if s.get("open"):
        o = s["open"]
        exit_px = outcome = None
        for b in new_m5:
            if o["dir"] == "BULL":
                if b.low <= o["sl"]:
                    exit_px, outcome = o["sl"], "LOSS"; break
                if b.high >= o["tp"]:
                    exit_px, outcome = o["tp"], "WIN"; break
            else:
                if b.high >= o["sl"]:
                    exit_px, outcome = o["sl"], "LOSS"; break
                if b.low <= o["tp"]:
                    exit_px, outcome = o["tp"], "WIN"; break
        if outcome:
            half = (SPREAD + SLIPPAGE) / 2.0
            exit_adj = exit_px - half if o["dir"] == "BULL" else exit_px + half
            gross = (exit_adj - o["entry"]) if o["dir"] == "BULL" else (o["entry"] - exit_adj)
            pnl = gross * _value_per_unit(SYMBOL) * o["lot"] - COMMISSION_PER_LOT * o["lot"]
            r = gross / abs(o["entry"] - o["sl"]) if o["entry"] != o["sl"] else 0.0
            s["equity"] = round(s["equity"] + pnl, 2)
            s["day_profit"] = round(s["day_profit"] + pnl, 2)
            s["loss_streak"] = 0 if outcome == "WIN" else s.get("loss_streak", 0) + 1
            rec = {"event": "CLOSE", "dir": o["dir"], "entry": o["entry"], "exit": round(exit_adj, 2),
                   "outcome": outcome, "r": round(r, 2), "pnl": round(pnl, 2),
                   "lot": o["lot"], "equity": s["equity"]}
            s["closed"].append(rec); _journal(rec)
            s["open"] = None
            if verbose:
                print(f"CLOSE {o['dir']} {outcome} {r:+.2f}R ${pnl:+.2f} -> equity ${s['equity']}")

    # ── 2) Fill a PENDING limit against new M5 bars ──
    if s.get("pending") and not s.get("open"):
        p = s["pending"]
        filled = False
        for b in new_m5:
            if p["dir"] == "BULL":
                if b.low <= p["sl"]:        # invalidated before fill
                    s["pending"] = None; _journal({"event": "CANCEL", **p}); break
                if b.low <= p["entry"]:
                    filled = True; break
            else:
                if b.high >= p["sl"]:
                    s["pending"] = None; _journal({"event": "CANCEL", **p}); break
                if b.high >= p["entry"]:
                    filled = True; break
        if filled:
            s["open"] = {k: p[k] for k in ("dir", "entry", "sl", "tp", "lot", "risk_usd")}
            s["open"]["fill_ts"] = mgmt[-1].time
            _journal({"event": "FILL", **s["open"]})
            s["pending"] = None
            if verbose:
                print(f"FILL {s['open']['dir']} @ {s['open']['entry']}")
        elif price and (mgmt[-1].time - p.get("created_ts", mgmt[-1].time)) > FILL_WINDOW_SEC:
            s["pending"] = None; _journal({"event": "EXPIRE", **p})

    # ── 3) Look for a NEW setup only when flat ──
    if not s.get("open") and not s.get("pending"):
        ok, why = can_open(DayState(day_start_equity=s["day_start_equity"],
                                    day_profit=s["day_profit"],
                                    loss_streak=s.get("loss_streak", 0)), rcfg)
        if not ok:
            if verbose:
                print(f"flat — daily gate closed: {why}")
        else:
            setup = evaluate(htf, ltf, cfg=cfg, now_ts=ltf[-1].time)
            if setup.actionable:
                lot, risk_usd, why = size_position(s["equity"], setup.entry, setup.sl,
                                                   SYMBOL, open_risk_usd=0.0, cfg=rcfg)
                if lot > 0:
                    s["pending"] = {"dir": setup.direction, "entry": setup.entry,
                                    "sl": setup.sl, "tp": setup.tp, "lot": lot,
                                    "risk_usd": round(risk_usd, 2),
                                    "rr": setup.rr, "created_ts": mgmt[-1].time}
                    _journal({"event": "SIGNAL", **s["pending"], "why": why})
                    if verbose:
                        print(f"SIGNAL {setup.direction} entry={setup.entry} sl={setup.sl} "
                              f"tp={setup.tp} {setup.rr}R lot={lot} (${risk_usd} risk)")
                else:
                    if verbose:
                        print(f"setup found but not sized: {why}")
            else:
                if verbose:
                    print(f"flat — no setup (died at {setup.failed_at})")

    if new_m5:
        s["last_m5_ts"] = new_m5[-1].time
    _save_state(s)


# ──────────────────────────────────────────────────────────────────────────────
def status() -> None:
    s = _load_state()
    closed = s.get("closed", [])
    wins = [c for c in closed if c["outcome"] == "WIN"]
    n = len(closed)
    wr = (len(wins) / n * 100) if n else 0.0
    total_r = sum(c["r"] for c in closed)
    print("=== SEQUENTIAL PAPER FORWARD TEST ===")
    print(f"  start equity : ${s.get('start_equity')}")
    print(f"  equity now   : ${s.get('equity')}")
    print(f"  closed trades: {n}  | wins {len(wins)} | WR {wr:.1f}%")
    print(f"  total R      : {total_r:+.2f}")
    print(f"  open         : {s.get('open')}")
    print(f"  pending      : {s.get('pending')}")
    for c in closed[-10:]:
        print(f"   {c['dir']:4} {c['outcome']:4} {c['r']:+.2f}R ${c['pnl']:+.2f} -> ${c['equity']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="seconds between ticks")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        status(); return
    if args.loop:
        print(f"paper forward test running every {args.loop}s (Ctrl-C to stop)…")
        try:
            while True:
                tick(verbose=True)
                time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\nstopped.")
    else:
        tick(verbose=True)


if __name__ == "__main__":
    main()
