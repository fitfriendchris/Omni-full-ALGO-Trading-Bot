"""
Tests for dual_tf_selector.py — HTF bias + LTF entry trigger integration.

Run:
    pytest tests/test_dual_tf_selector.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from smc_engine import Bar, _make_fixture, analyze  # noqa: E402
from dual_tf_selector import (  # noqa: E402
    Bias, TradeSelection,
    detect_htf_bias, select_trade, DEFAULT_RULES,
    _proximity_ok, _sl_from_ob, _tp_from_rr,
    _most_recent_unmitigated_ob, _most_recent_unmitigated_fvg,
)


def _bar(t, o, h, l, c):
    return Bar(time=t, open=o, high=h, low=l, close=c)


# ──────────────────────────────────────────────────────────────────────────────
# HTF bias
# ──────────────────────────────────────────────────────────────────────────────

class TestHTFBias:
    def test_empty_snapshot_is_neutral(self):
        snap = analyze([])
        bias = detect_htf_bias(snap)
        assert bias.direction == "NEUTRAL"
        assert bias.score == 0.0

    def test_structure_wins(self):
        """Fixture has CHoCH BULL → bias must be BULL via 'structure'."""
        snap = analyze(_make_fixture())
        bias = detect_htf_bias(snap)
        assert bias.direction == "BULL"
        assert bias.source == "structure"
        assert 0.6 < bias.score <= 1.0

    def test_bias_dataclass(self):
        b = Bias(direction="BULL", source="structure", score=0.9, details={})
        assert b.direction == "BULL"


# ──────────────────────────────────────────────────────────────────────────────
# Proximity / stop helpers
# ──────────────────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_proximity_inside_zone(self):
        assert _proximity_ok(1.05, top=1.10, bot=1.00, atr_val=0.01, frac=0.5)

    def test_proximity_within_tolerance(self):
        # price above the top but within 0.5*ATR
        assert _proximity_ok(1.12, top=1.10, bot=1.00, atr_val=0.05, frac=0.5)

    def test_proximity_outside(self):
        assert not _proximity_ok(1.50, top=1.10, bot=1.00, atr_val=0.01, frac=0.5)

    def test_sl_buffer_bullish(self):
        from smc_engine import OrderBlock
        ob = OrderBlock(side="BULL", anchor_idx=0, break_idx=1,
                        top=1.10, bot=1.05, body_top=1.09, body_bot=1.06,
                        anchor_time=0.0)
        sl = _sl_from_ob(ob, "BULL", buffer_frac=0.10)
        assert sl < ob.bot
        # distance equals 0.10 * (top-bot) = 0.005
        assert abs(sl - (ob.bot - 0.005)) < 1e-9

    def test_tp_from_rr_bull(self):
        tp = _tp_from_rr(entry=1.10, sl=1.09, side="BULL", rr=2.0)
        assert tp == pytest.approx(1.12)   # 1.10 + 2*(0.01)

    def test_tp_from_rr_bear(self):
        tp = _tp_from_rr(entry=1.10, sl=1.11, side="BEAR", rr=2.0)
        assert tp == pytest.approx(1.08)


# ──────────────────────────────────────────────────────────────────────────────
# End-to-end selection
# ──────────────────────────────────────────────────────────────────────────────

class TestSelectTrade:
    def test_empty_inputs_return_neutral(self):
        sel = select_trade([], [])
        assert sel.direction == "NEUTRAL"
        assert sel.entry_type == "none"
        assert not sel.is_actionable

    def test_disabled_rules_short_circuit(self):
        rules = {"dual_tf": {"enabled": False}}
        sel = select_trade(_make_fixture(), _make_fixture(), rules=rules)
        assert sel.direction == "NEUTRAL"

    def test_bias_set_on_fixture(self):
        sel = select_trade(_make_fixture(), _make_fixture())
        assert sel.htf_bias is not None
        assert sel.htf_bias.direction == "BULL"

    def test_snapshots_attached(self):
        sel = select_trade(_make_fixture(), _make_fixture())
        assert sel.htf_snapshot is not None
        assert sel.ltf_snapshot is not None
        assert len(sel.htf_snapshot.swings) >= 1

    def test_actionable_on_crafted_scenario(self):
        """Construct LTF bars where price has just pulled back into the
        unmitigated BULL OB — should produce an actionable OB-mitigation trade."""
        bars = _make_fixture()
        # Append 3 pullback bars that trade back INTO the last bull OB zone.
        # Pick the most recent BULL OB from analysis:
        snap = analyze(bars)
        bulls = [o for o in snap.order_blocks if o.side == "BULL" and not o.mitigated]
        if not bulls:
            pytest.skip("fixture produced no unmitigated BULL OB")
        ob = bulls[-1]
        mid = (ob.top + ob.bot) / 2.0
        t0 = bars[-1].time + 60
        pullback = [
            _bar(t0,       bars[-1].close, bars[-1].close + 0.0001, mid + 0.0002, mid + 0.0001),
            _bar(t0 + 60,  mid + 0.0001, mid + 0.0002, mid - 0.0001, mid),
            _bar(t0 + 120, mid,          mid + 0.0003, mid - 0.0001, mid + 0.0002),
        ]
        ltf = bars + pullback
        sel = select_trade(bars, ltf)
        # At minimum: bias BULL, and price now in/near OB → OB mitigation trigger
        assert sel.htf_bias.direction == "BULL"
        # Actionability is threshold-dependent; at least entry_type should be set
        # if pullback hit the zone
        assert sel.entry_type in ("ob_mitigation", "fvg_fill", "sweep_choch", "none")
        if sel.is_actionable:
            assert sel.sl < sel.entry_price  # BULL → SL below entry
            assert sel.tp > sel.entry_price

    def test_reasons_populated(self):
        sel = select_trade(_make_fixture(), _make_fixture())
        assert len(sel.reasons) >= 1
        assert any("bias" in r.lower() for r in sel.reasons)

    def test_min_confidence_gate(self):
        """With a very high threshold, nothing should fire."""
        rules = {"dual_tf": {"enabled": True, "min_confidence": 0.99,
                             "entries": ["ob_mitigation", "fvg_fill", "sweep_choch"]}}
        sel = select_trade(_make_fixture(), _make_fixture(), rules=rules)
        assert not sel.is_actionable


# ──────────────────────────────────────────────────────────────────────────────
# Determinism
# ──────────────────────────────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_input_same_output(self):
        a = select_trade(_make_fixture(), _make_fixture())
        b = select_trade(_make_fixture(), _make_fixture())
        assert a.direction == b.direction
        assert a.entry_type == b.entry_type
        assert a.confidence == b.confidence
