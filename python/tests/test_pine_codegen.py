"""Tests for pine_codegen.py — Pine v5 script emission."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pine_codegen import PINE_HEADER, _esc, generate_pine, write_pine  # noqa: E402
from signal_writers import Signal  # noqa: E402


def _mk_actionable_bull() -> Signal:
    return Signal(
        id="t-bull-1", ts="2026-04-17T12:00:00+00:00",
        symbol="EURUSD", timeframe="M5", direction="BULL",
        entry_type="ob_mitigation",
        entry_price=1.1234, sl=1.1200, tp=1.1300,
        confidence=0.82, reasons=["HTF BULL", "LTF OB"],
        scale_action="HOLD", scale_mult=1.0, htf_bias="BULL",
    )


def _mk_actionable_bear() -> Signal:
    return Signal(
        id="t-bear-1", ts="2026-04-17T12:05:00+00:00",
        symbol="GBPUSD", timeframe="M5", direction="BEAR",
        entry_type="fvg_fill",
        entry_price=1.2500, sl=1.2550, tp=1.2400,
        confidence=0.71, reasons=["HTF BEAR"],
        scale_action="HOLD", scale_mult=1.0, htf_bias="BEAR",
    )


def _mk_neutral() -> Signal:
    return Signal(
        id="t-neutral-1", ts="2026-04-17T12:10:00+00:00",
        symbol="XAUUSD", timeframe="M5", direction="NEUTRAL",
        entry_type="none",
        entry_price=None, sl=None, tp=None,
        confidence=0.10, reasons=["no trigger"],
        scale_action="HOLD", scale_mult=1.0, htf_bias="NEUTRAL",
    )


class TestEscape(unittest.TestCase):
    def test_escapes_double_quotes(self):
        self.assertEqual(_esc('a"b'), 'a\\"b')

    def test_escapes_backslash(self):
        self.assertEqual(_esc("a\\b"), "a\\\\b")

    def test_strips_newlines(self):
        self.assertNotIn("\n", _esc("line1\nline2"))
        self.assertNotIn("\r", _esc("line1\rline2"))


class TestGeneratePine(unittest.TestCase):
    def test_header_always_present(self):
        script = generate_pine([])
        self.assertIn("indicator(", script)
        self.assertIn("@version=5", script)
        self.assertIn("overlay=true", script)

    def test_empty_signals_has_placeholder_comment(self):
        script = generate_pine([])
        self.assertIn("no signals this cycle", script)

    def test_bull_signal_draws_entry_line(self):
        script = generate_pine([_mk_actionable_bull()])
        # Entry label carries symbol + direction + entry_type + confidence
        self.assertIn("EURUSD BULL ob_mitigation", script)
        # Uses bullCol for bullish signals
        self.assertIn("color=bullCol", script)
        # Entry, SL, TP price levels all appear
        self.assertIn("1.1234", script)
        self.assertIn("1.12", script)
        self.assertIn("1.13", script)
        # line.new + label.new are emitted
        self.assertIn("line.new(", script)
        self.assertIn("label.new(", script)
        # Gated on barstate.islast
        self.assertIn("barstate.islast", script)

    def test_bear_signal_uses_bear_color(self):
        script = generate_pine([_mk_actionable_bear()])
        self.assertIn("color=bearCol", script)
        self.assertIn("GBPUSD BEAR fvg_fill", script)
        self.assertNotIn("color=bullCol", script)

    def test_neutral_signal_is_skipped_as_comment(self):
        script = generate_pine([_mk_neutral()])
        self.assertIn("skipped: XAUUSD NEUTRAL none", script)
        # No draw primitives for a skipped signal
        self.assertNotIn("label.new(", script)

    def test_mixed_list_produces_one_block_per_signal(self):
        signals = [_mk_actionable_bull(), _mk_neutral(), _mk_actionable_bear()]
        script = generate_pine(signals)
        self.assertEqual(script.count("// [0] "), 1)
        self.assertEqual(script.count("// [1] skipped: "), 1)
        self.assertEqual(script.count("// [2] "), 1)

    def test_confidence_formatted_two_decimals(self):
        script = generate_pine([_mk_actionable_bull()])
        # 0.82 → "(0.82)" exactly (two decimals)
        self.assertIn("(0.82)", script)


class TestAtomicWrite(unittest.TestCase):
    def test_roundtrip_write_pine(self):
        signals = [_mk_actionable_bull()]
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "sub" / "omni_pine_overlay.pine"
            write_pine(signals, str(out))
            self.assertTrue(out.exists())
            content = out.read_text(encoding="utf-8")
            self.assertIn("EURUSD BULL ob_mitigation", content)
            self.assertIn("@version=5", content)
            # Atomic tmp file should not linger
            self.assertFalse(out.with_suffix(out.suffix + ".tmp").exists())


class TestPineHeaderConstant(unittest.TestCase):
    def test_header_declares_color_inputs(self):
        self.assertIn("bullCol", PINE_HEADER)
        self.assertIn("bearCol", PINE_HEADER)
        self.assertIn("slCol",   PINE_HEADER)
        self.assertIn("tpCol",   PINE_HEADER)
        self.assertIn("lineW",   PINE_HEADER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
