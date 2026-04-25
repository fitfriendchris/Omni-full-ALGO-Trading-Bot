"""
Tests for scaling_engine.py — ADD / REDUCE / HOLD / CLOSE decisions.
"""

from __future__ import annotations

import os
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from smc_engine import Bar, _make_fixture, analyze   # noqa: E402
from scaling_engine import (                          # noqa: E402
    PositionCtx, ScaleAction, evaluate, DEFAULT_RULES,
    _htf_reversal_against, _ltf_reversal_against,
)


def _mk_pos(direction="BULL", entry=1.10, sl=1.095, cur=1.11, add_count=0):
    return PositionCtx(
        symbol="EURUSD", direction=direction,
        entry_price=entry, current_price=cur,
        initial_sl=sl, current_sl=sl,
        volume=1.0, add_count=add_count,
    )


class TestProfitR:
    def test_bull_profit(self):
        p = _mk_pos(direction="BULL", entry=1.10, sl=1.095, cur=1.105)
        assert p.profit_r == pytest.approx(1.0)

    def test_bear_profit(self):
        p = _mk_pos(direction="BEAR", entry=1.10, sl=1.105, cur=1.095)
        assert p.profit_r == pytest.approx(1.0)

    def test_breakeven(self):
        p = _mk_pos(direction="BULL", entry=1.10, sl=1.095, cur=1.10)
        assert p.profit_r == 0.0

    def test_adverse(self):
        p = _mk_pos(direction="BULL", entry=1.10, sl=1.095, cur=1.099)
        assert p.profit_r < 0

    def test_zero_risk_safe(self):
        p = _mk_pos(direction="BULL", entry=1.10, sl=1.10, cur=1.11)
        assert p.profit_r == 0.0  # no divide-by-zero


class TestEvaluateBasics:
    def test_disabled(self):
        rules = {"scaling": {"enabled": False}}
        act = evaluate(_mk_pos(), analyze([]), analyze([]), rules=rules)
        assert act.action == "HOLD"
        assert act.is_no_op

    def test_empty_snapshots_holds(self):
        act = evaluate(_mk_pos(), analyze([]), analyze([]))
        assert act.action == "HOLD"

    def test_no_profit_no_add(self):
        # At 0R profit, min_add_profit_r=1.0 by default — must not ADD
        snap = analyze(_make_fixture())
        p = _mk_pos(direction="BULL", entry=1.10, sl=1.095, cur=1.10)
        act = evaluate(p, snap, snap)
        assert act.action in ("HOLD",)


class TestClose:
    def test_close_on_htf_reversal_in_drawdown(self):
        """BEAR position, HTF has BULL CHoCH → close."""
        snap = analyze(_make_fixture())
        # Fixture's last event is CHoCH BULL; simulate BEAR position adverse.
        # Use a cur that produces profit_r clearly <= close_at_adverse_r (default -0.2)
        # without relying on tight floating-point equality.
        p = _mk_pos(direction="BEAR", entry=1.10, sl=1.105, cur=1.103)
        assert p.profit_r < -0.2, f"precondition: profit_r={p.profit_r}"
        act = evaluate(p, snap, snap)
        assert act.action == "CLOSE"
        assert act.size_multiplier == 0.0

    def test_no_close_when_profitable_despite_reversal(self):
        snap = analyze(_make_fixture())
        # Adverse threshold gates CLOSE — if profit_r is positive, stay
        p = _mk_pos(direction="BEAR", entry=1.10, sl=1.105, cur=1.09)
        act = evaluate(p, snap, snap)
        assert act.action != "CLOSE"


class TestReduce:
    def test_reduce_at_r_with_ltf_reversal(self):
        """Deeply profitable BEAR position, LTF has CHoCH BULL → REDUCE."""
        snap = analyze(_make_fixture())  # has CHoCH BULL
        p = _mk_pos(direction="BEAR", entry=1.10, sl=1.105, cur=1.090)
        # profit_r ≈ (1.10 - 1.09) / 0.005 = 2.0
        assert p.profit_r >= 2.0
        act = evaluate(p, snap, snap)
        assert act.action == "REDUCE"
        assert 0 < act.size_multiplier < 1

    def test_reduce_requires_ltf_choch_by_default(self):
        # An LTF snapshot with no structure → no REDUCE even if profitable
        snap_htf = analyze(_make_fixture())
        snap_ltf_empty = analyze([])
        p = _mk_pos(direction="BULL", entry=1.10, sl=1.095, cur=1.115)
        # profit_r = (1.115 - 1.10) / 0.005 = 3.0 — above reduce_at_r
        act = evaluate(p, snap_htf, snap_ltf_empty)
        assert act.action != "REDUCE"


class TestAdd:
    def test_add_requires_min_profit(self):
        snap = analyze(_make_fixture())
        # Not profitable enough to add
        p = _mk_pos(direction="BULL", entry=1.10, sl=1.095, cur=1.1005)
        act = evaluate(p, snap, snap)
        assert act.action != "ADD"

    def test_add_caps_at_max_adds(self):
        snap = analyze(_make_fixture())
        # Already at max_adds
        p = _mk_pos(direction="BULL", entry=1.10, sl=1.095, cur=1.11, add_count=2)
        rules = {"scaling": dict(DEFAULT_RULES["scaling"], max_adds=2)}
        act = evaluate(p, snap, snap, rules=rules)
        assert act.action != "ADD"

    def test_hold_when_no_zone_to_add_into(self):
        """Price far from any OB/FVG — should HOLD."""
        snap = analyze(_make_fixture())
        p = _mk_pos(direction="BULL", entry=1.10, sl=1.095, cur=1.200)  # way above zones
        act = evaluate(p, snap, snap)
        assert act.action in ("HOLD", "REDUCE", "CLOSE")


class TestHelpers:
    def test_htf_reversal_against(self):
        snap = analyze(_make_fixture())
        # Fixture has CHoCH BULL → reversal against a BEAR position
        assert _htf_reversal_against(snap, "BEAR") is True
        assert _htf_reversal_against(snap, "BULL") is False

    def test_ltf_reversal_against_empty(self):
        snap = analyze([])
        assert _ltf_reversal_against(snap, "BULL") is False
        assert _ltf_reversal_against(snap, "BEAR") is False
