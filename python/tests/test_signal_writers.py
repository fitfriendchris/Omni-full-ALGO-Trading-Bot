"""Tests for signal_writers.py — unified signal envelope + atomic JSON writes."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signal_writers import (  # noqa: E402
    SIGNALS_VERSION,
    Signal,
    build_signal,
    build_signals_payload,
    prune_signals,
    write_signals_json,
)
from dual_tf_selector import Bias, TradeSelection  # noqa: E402


def _mk_sel(direction: str = "BULL") -> TradeSelection:
    return TradeSelection(
        direction=direction,
        entry_type="ob_mitigation",
        entry_price=1.1000,
        sl=1.0970,
        tp=1.1060,
        confidence=0.75,
        reasons=["htf_bos_bull", "ltf_ob_mitigated"],
        htf_bias=Bias(direction=direction, source="structure", score=0.9, details={}),
    )


class TestBuildSignal(unittest.TestCase):
    def test_deterministic_id(self):
        ts = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
        s1 = build_signal("EURUSD", "M5", _mk_sel(), ts=ts)
        s2 = build_signal("EURUSD", "M5", _mk_sel(), ts=ts)
        self.assertEqual(s1.id, s2.id)
        self.assertIn("EURUSD", s1.id)
        self.assertIn("M5", s1.id)
        self.assertIn("BULL", s1.id)

    def test_different_direction_yields_different_id(self):
        ts = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
        s_bull = build_signal("EURUSD", "M5", _mk_sel("BULL"), ts=ts)
        s_bear = build_signal("EURUSD", "M5", _mk_sel("BEAR"), ts=ts)
        self.assertNotEqual(s_bull.id, s_bear.id)

    def test_copies_core_fields(self):
        sel = _mk_sel()
        sig = build_signal("XAUUSD", "H1", sel)
        self.assertEqual(sig.symbol, "XAUUSD")
        self.assertEqual(sig.timeframe, "H1")
        self.assertEqual(sig.direction, "BULL")
        self.assertEqual(sig.entry_price, 1.1000)
        self.assertEqual(sig.sl, 1.0970)
        self.assertEqual(sig.tp, 1.1060)
        self.assertAlmostEqual(sig.confidence, 0.75)
        self.assertEqual(sig.htf_bias, "BULL")


class TestPayload(unittest.TestCase):
    def test_payload_shape(self):
        sig = build_signal("EURUSD", "M5", _mk_sel())
        payload = build_signals_payload([sig], trail_proposals=[{"ticket": 42, "sl": 1.099}])
        self.assertEqual(payload["version"], SIGNALS_VERSION)
        self.assertIn("generated_at", payload)
        self.assertEqual(len(payload["signals"]), 1)
        self.assertEqual(payload["signals"][0]["symbol"], "EURUSD")
        self.assertEqual(len(payload["trail_proposals"]), 1)

    def test_empty_signals_ok(self):
        payload = build_signals_payload([], trail_proposals=None)
        self.assertEqual(payload["signals"], [])
        # Default trail_proposals is an empty container (dict per impl) — just
        # assert it's empty and falsy, not its concrete type.
        self.assertFalse(payload["trail_proposals"])


class TestAtomicWrite(unittest.TestCase):
    def test_roundtrip_and_no_tmp_leftover(self):
        sig = build_signal("EURUSD", "M5", _mk_sel())
        payload = build_signals_payload([sig])
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "signals.json"
            write_signals_json(payload, str(out))
            self.assertTrue(out.exists())
            tmp = out.with_suffix(out.suffix + ".tmp")
            self.assertFalse(tmp.exists(), "atomic write should not leave .tmp behind")
            with out.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded["signals"][0]["id"], sig.id)

    def test_overwrite_previous(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "signals.json"
            write_signals_json(build_signals_payload([build_signal("EURUSD", "M5", _mk_sel("BULL"))]), str(out))
            write_signals_json(build_signals_payload([build_signal("EURUSD", "M5", _mk_sel("BEAR"))]), str(out))
            with out.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded["signals"][0]["direction"], "BEAR")


class TestPrune(unittest.TestCase):
    def _mk_sig(self, ts_str: str, symbol: str = "EURUSD",
                direction: str = "BULL", entry_type: str = "ob_mitigation") -> Signal:
        return Signal(
            id=f"{symbol}-M5-{ts_str}",
            ts=ts_str,
            symbol=symbol,
            timeframe="M5",
            direction=direction,
            entry_type=entry_type,
            entry_price=1.1,
            sl=1.09,
            tp=1.12,
            confidence=0.7,
            reasons=(),
            scale_action="HOLD",
            scale_mult=0.0,
            htf_bias="BULL",
            source="orchestrator",
        )

    def test_prune_keeps_most_recent(self):
        # Three distinct symbols → three distinct dedup keys; max_kept=2 drops oldest
        sigs = [
            self._mk_sig("2026-04-17T10:00:00+00:00", symbol="EURUSD"),
            self._mk_sig("2026-04-17T12:00:00+00:00", symbol="GBPUSD"),
            self._mk_sig("2026-04-17T11:00:00+00:00", symbol="USDJPY"),
        ]
        kept = prune_signals(sigs, max_kept=2)
        self.assertEqual(len(kept), 2)
        # prune_signals returns newest-first, so kept[0] is the most recent
        ts_kept = [s.ts for s in kept]
        self.assertIn("2026-04-17T12:00:00+00:00", ts_kept)
        self.assertIn("2026-04-17T11:00:00+00:00", ts_kept)
        self.assertNotIn("2026-04-17T10:00:00+00:00", ts_kept)

    def test_prune_noop_when_under_limit(self):
        sigs = [self._mk_sig("2026-04-17T10:00:00+00:00")]
        kept = prune_signals(sigs, max_kept=10)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].symbol, sigs[0].symbol)

    def test_dedup_collapses_same_key(self):
        # Same (symbol, direction, entry_type) at three timestamps → collapse to 1 (most recent)
        sigs = [
            self._mk_sig("2026-04-17T10:00:00+00:00"),
            self._mk_sig("2026-04-17T12:00:00+00:00"),
            self._mk_sig("2026-04-17T11:00:00+00:00"),
        ]
        kept = prune_signals(sigs, max_kept=20)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].ts, "2026-04-17T12:00:00+00:00")
        self.assertEqual(kept[0].re_emit_count, 2)  # saw 3 → 2 re-emits


if __name__ == "__main__":
    unittest.main(verbosity=2)
