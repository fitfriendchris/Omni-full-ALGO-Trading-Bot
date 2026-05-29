"""
risk_sizing.py — Position sizing & portfolio risk control (Chris's rules).

RULES (as specified by the trader):
  1. Minimum lot (0.01) while the account is small; lot scales UP as equity grows.
     -> small account = small lot, NOT bigger risk %. Growth funds bigger size.
  2. AGGREGATE open risk across ALL positions is capped at 23% of balance.
     (Not 23% per trade — 23% total exposure-to-stop at any moment.)
  3. Target +30% of account balance, then lock: stop opening new trades for the
     day once the daily profit target is reached, to bank the gains.
  4. Ruin guards: halt new entries on a daily loss limit or a loss streak.

WHY THIS SHAPE
  On a $133 gold account the 0.01-lot floor already risks ~$3–6 per trade
  (gold's normal stop distance), i.e. ~3–5% — you physically cannot risk "1%".
  So the lever is NOT risk-% (fixed by the lot floor) but HOW MANY concurrent
  positions you allow (the 23% aggregate cap) and WHEN you size up (equity grows).

Gold contract math (XAUUSD): 1.00 lot = 100 oz -> a $1 price move = $100/lot,
so $1 move = $1.00 per 0.01 lot. Risk($) for a trade = sl_distance($) *
value_per_point_per_lot * lots, where for XAUUSD value_per_point_per_lot = 100
(per $1) and 1 "point" here = $1 of price. We keep it explicit and configurable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


# Dollar value of a 1.0 price-unit ($1) move, per 1.0 lot, by symbol.
# XAUUSD: 100 oz/lot -> $100 per $1 move.  XAGUSD: 5000 oz/lot -> $5000 per $1 move.
VALUE_PER_PRICE_UNIT_PER_LOT = {
    "XAUUSD": 100.0,
    "XAGUSD": 5000.0,
}


@dataclass
class RiskConfig:
    min_lot: float = 0.01
    lot_step: float = 0.01
    max_lot: float = 1.00
    # Lot scales: +1 lot_step for each `growth_step_usd` of equity above `base_equity`.
    base_equity: float = 130.0
    growth_step_usd: float = 130.0      # double equity ≈ double lot (keeps risk-% steady)
    # Portfolio risk ceiling (sum of all open trades' risk-to-SL).
    aggregate_risk_pct: float = 0.23
    # Daily profit target — once hit, stop opening new trades (bank it).
    daily_target_pct: float = 0.30
    # Ruin guards.
    daily_loss_halt_pct: float = 0.15   # stop for the day at -15% of day-start equity
    loss_streak_halt: int = 3           # stop after N consecutive losses (resume next day)


@dataclass
class DayState:
    day_start_equity: float = 0.0
    day_profit: float = 0.0
    loss_streak: int = 0
    halted: bool = False
    halt_reason: str = ""


def _value_per_unit(symbol: str) -> float:
    return VALUE_PER_PRICE_UNIT_PER_LOT.get(symbol.upper(), 100.0)


def base_lot_for_equity(equity: float, cfg: RiskConfig) -> float:
    """Lot size driven purely by equity (rule 1). Floors at min_lot, grows in
    lot_step increments as equity climbs through growth_step_usd bands."""
    if equity <= cfg.base_equity:
        return cfg.min_lot
    bands = math.floor((equity - cfg.base_equity) / cfg.growth_step_usd)
    lot = cfg.min_lot + bands * cfg.lot_step
    return max(cfg.min_lot, min(cfg.max_lot, round(lot, 2)))


def trade_risk_usd(entry: float, sl: float, lots: float, symbol: str) -> float:
    """Dollar risk to stop for a given lot size."""
    return abs(entry - sl) * _value_per_unit(symbol) * lots


def can_open(day: DayState, cfg: RiskConfig) -> Tuple[bool, str]:
    """Daily-level gate: target reached? loss limit? streak halt?"""
    if day.halted:
        return False, f"halted: {day.halt_reason}"
    if day.day_start_equity > 0:
        if day.day_profit >= cfg.daily_target_pct * day.day_start_equity:
            return False, f"daily +{cfg.daily_target_pct*100:.0f}% target reached — banking gains"
        if day.day_profit <= -cfg.daily_loss_halt_pct * day.day_start_equity:
            return False, f"daily -{cfg.daily_loss_halt_pct*100:.0f}% loss limit — stop for the day"
    if day.loss_streak >= cfg.loss_streak_halt:
        return False, f"{day.loss_streak} losses in a row — stop for the day"
    return True, "ok"


def size_position(equity: float, entry: float, sl: float, symbol: str,
                  open_risk_usd: float = 0.0,
                  cfg: Optional[RiskConfig] = None) -> Tuple[float, float, str]:
    """Decide lot size for a new trade.

    Returns (lots, risk_usd, reason). lots == 0.0 means: do NOT open (the
    aggregate 23% cap leaves no room even for the minimum lot).
    """
    cfg = cfg or RiskConfig()
    if entry is None or sl is None or entry == sl or equity <= 0:
        return 0.0, 0.0, "invalid entry/sl/equity"

    lots = base_lot_for_equity(equity, cfg)
    risk = trade_risk_usd(entry, sl, lots, symbol)

    cap = cfg.aggregate_risk_pct * equity
    room = cap - open_risk_usd
    if room <= 0:
        return 0.0, 0.0, f"aggregate risk cap reached (open ${open_risk_usd:.2f} / cap ${cap:.2f})"

    # If even the minimum lot exceeds remaining room, do not open.
    min_risk = trade_risk_usd(entry, sl, cfg.min_lot, symbol)
    if min_risk > room:
        return 0.0, 0.0, (f"min lot risks ${min_risk:.2f} but only ${room:.2f} room "
                          f"under 23% cap — skip")

    # If the equity-scaled lot would breach the cap, step it down to fit.
    while lots > cfg.min_lot and risk > room:
        lots = round(lots - cfg.lot_step, 2)
        risk = trade_risk_usd(entry, sl, lots, symbol)

    return lots, risk, (f"lot {lots} (equity-scaled), risk ${risk:.2f} "
                        f"({risk/equity*100:.1f}% of equity); aggregate "
                        f"${open_risk_usd+risk:.2f}/{cap:.2f}")


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    cfg = RiskConfig()
    print("equity -> base lot")
    for eq in (133, 260, 400, 600, 1300):
        print(f"  ${eq:>5} -> {base_lot_for_equity(eq, cfg)} lot")

    # $133 account, gold long, $4 stop.
    lots, risk, why = size_position(133.42, 4500.0, 4496.0, "XAUUSD", open_risk_usd=0.0, cfg=cfg)
    print(f"\nfirst trade: {lots} lot, ${risk:.2f} risk -> {why}")
    assert lots == 0.01 and abs(risk - 4.0) < 1e-6, "0.01 lot, $4 stop = $4 risk"

    # Stack until the 23% cap ($30.69) blocks a new one.
    open_risk, n = 0.0, 0
    while True:
        lots, risk, why = size_position(133.42, 4500.0, 4496.0, "XAUUSD",
                                        open_risk_usd=open_risk, cfg=cfg)
        if lots == 0.0:
            print(f"blocked after {n} positions (open ${open_risk:.2f}): {why}")
            break
        open_risk += risk; n += 1
        if n > 20:
            break
    assert n == 7, f"expected ~7 x $4 = $28 under $30.69 cap, got {n}"

    # Daily gates.
    day = DayState(day_start_equity=133.42, day_profit=40.5)  # +30% = 40.0
    ok, why = can_open(day, cfg)
    print(f"\ndaily target gate: can_open={ok} ({why})")
    assert not ok, "should stop after +30% day"

    day = DayState(day_start_equity=133.42, loss_streak=3)
    ok, why = can_open(day, cfg)
    assert not ok and "row" in why

    print("\n[OK] risk_sizing self-test passed")


if __name__ == "__main__":
    _self_test()
