"""
test_confluence_engine.py — Comprehensive test harness for Phase 4 Confluence Engine

Tests:
  1. Manipulation leg detection on known synthetic patterns
  2. STDV profile correctness (CE midpoint, OTE ratios)
  3. Confluence counting with controlled confluence inputs
  4. Kill zone gating (inside vs outside)
  5. Entry pricing at true OTE/STDV levels (not market price)
  6. SL placement beyond manipulation leg wick
  7. R:R correctness (min 2:1)
  8. AMD alignment bonuses

Run: python test_confluence_engine.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ── Module imports ────────────────────────────────────────────────────────────
from smc_engine import Bar as SMCBar, analyze, _make_fixture as _smc_fixture
from manipulation_leg_detector import (
    Bar as MLDBar, detect_manipulation_legs,
    get_primary_manipulation_leg, _make_fixture as _manip_fixture,
)
from stdv_ote_engine import (
    STDVOTEProfile, compute_profile as compute_stdv_profile,
    nearest_level, is_price_in_ote_zone, get_entry_candidates,
)
from dual_tf_selector import (
    select_trade, TradeSelection, HTFBias,
    _to_mld, _check_c1_ote_level, _check_c2_ob_present,
    _check_c3_fvg_present, _check_c4_sweep_confirmed,
    _check_c5_structure_aligned, _check_c6_killzone_amd_aligned,
    _kill_zone_bonus, _amd_alignment, _compute_entry_price, _compute_sl, _compute_tp,
    _htf_bias, MIN_CONFIDENCE, MIN_CONFLUENCE, MIN_RR,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_bullish_manip_fixture() -> List[SMCBar]:
    """
    Synthetic BEARISH manipulation leg -> BULLISH displacement:
    - Asian range: 3300-3305
    - London sweep BELOW Asian low to 3297.5 (Judas low)
    - Sharp bullish rejection candle
    - Distribution up
    """
    base = 1704067200
    bars = []
    for i in range(28):
        bars.append(SMCBar(time=base + i*900, open=3302.0, high=3305.0, low=3300.0, close=3303.0))
    # Judas sweep below Asian low
    bars.append(SMCBar(time=base + 28*900, open=3300.0, high=3300.5, low=3297.5, close=3298.0))
    # Rejection candle (bullish)
    bars.append(SMCBar(time=base + 29*900, open=3298.0, high=3305.0, low=3297.5, close=3304.0))
    # Distribution up
    for i in range(5):
        bars.append(SMCBar(
            time=base + (30+i)*900,
            open=3303.0 + i*1.5, high=3304.0 + i*1.5,
            low=3302.0 + i*1.5, close=3303.5 + i*1.5,
        ))
    return bars


def _make_bearish_manip_fixture() -> List[SMCBar]:
    """
    The original JUDAS_HIGH fixture from manipulation_leg_detector.py.
    Returns smc_engine.Bar format.
    """
    raw = _manip_fixture()
    return [SMCBar(time=b.time, open=b.o, high=b.h, low=b.l, close=b.c) for b in raw]


def _make_no_manip_fixture() -> List[SMCBar]:
    """Boring consolidation with no clear manipulation leg."""
    base = 1704067200
    bars = []
    for i in range(40):
        o = 3300.0 + (i % 5) * 0.3
        c = o + (0.2 if i % 2 == 0 else -0.1)
        h = max(o, c) + 0.5
        l = min(o, c) - 0.3
        bars.append(SMCBar(time=base + i*900, open=o, high=h, low=l, close=c))
    return bars


def _assert_near(a: float, b: float, tol: float = 0.01, msg: str = "") -> None:
    assert abs(a - b) <= tol, f"{msg}: {a} vs {b} (tol={tol})"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: Manipulation leg detection
# ═══════════════════════════════════════════════════════════════════════════════

def test_bearish_manipulation_leg():
    """Detect JUDAS_HIGH on known bearish manipulation pattern."""
    bars = _make_bearish_manip_fixture()
    mld = _to_mld(bars)
    leg = get_primary_manipulation_leg(mld, bias_direction="BEAR",
                                       pip_size=0.01, min_recent_bars=25)
    assert leg.detected, "Must detect manipulation leg"
    assert leg.direction == "BEAR", f"Expected BEAR, got {leg.direction}"
    assert leg.leg_type == "JUDAS_HIGH", f"Expected JUDAS_HIGH, got {leg.leg_type}"
    assert leg.wick_high >= 3307.0, f"Wick high {leg.wick_high} too low"
    assert leg.wick_low <= 3304.5, f"Wick low {leg.wick_low} too high"
    print("[PASS] test_bearish_manipulation_leg")


def test_bullish_manipulation_leg():
    """Detect JUDAS_LOW on known bullish manipulation pattern."""
    bars = _make_bullish_manip_fixture()
    mld = _to_mld(bars)
    leg = get_primary_manipulation_leg(mld, bias_direction="BULL",
                                       pip_size=0.01, min_recent_bars=25)
    assert leg.detected, "Must detect manipulation leg"
    assert leg.direction == "BULL", f"Expected BULL, got {leg.direction}"
    assert leg.leg_type == "JUDAS_LOW", f"Expected JUDAS_LOW, got {leg.leg_type}"
    print("[PASS] test_bullish_manipulation_leg")


def test_no_manipulation_leg():
    """No manipulation leg in truly flat consolidation."""
    base = 1704067200
    bars = []
    # 40 bars of identical flat candles — absolutely no structure
    for i in range(40):
        bars.append(SMCBar(time=base + i*900, open=3300.0, high=3300.2, low=3299.8, close=3300.0))
    mld = _to_mld(bars)
    leg = get_primary_manipulation_leg(mld, bias_direction="BEAR",
                                       pip_size=0.01, min_recent_bars=35)
    assert not leg.detected, f"Should NOT detect leg in flat data: got {leg.leg_type}"
    print("[PASS] test_no_manipulation_leg")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: STDV profile correctness
# ═══════════════════════════════════════════════════════════════════════════════

def test_stdv_ce_midpoint():
    """CE must be exact midpoint of wick_high and wick_low."""
    bars = _make_bearish_manip_fixture()
    mld = _to_mld(bars)
    leg = get_primary_manipulation_leg(mld, bias_direction="BEAR",
                                       pip_size=0.01, min_recent_bars=25)
    profile = compute_stdv_profile(leg, mld)
    expected_ce = (leg.wick_high + leg.wick_low) / 2.0
    _assert_near(profile.ce, expected_ce, tol=0.001,
                 msg=f"CE {profile.ce} != midpoint {expected_ce}")
    print("[PASS] test_stdv_ce_midpoint")


def test_stdv_ote_zone_direction():
    """OTE zone must extend in displacement direction from CE."""
    bars = _make_bearish_manip_fixture()
    mld = _to_mld(bars)
    leg = get_primary_manipulation_leg(mld, bias_direction="BEAR",
                                       pip_size=0.01, min_recent_bars=25)
    profile = compute_stdv_profile(leg, mld)
    assert profile.ote_zone_top <= profile.ce, \
        f"BEAR: OTE top {profile.ote_zone_top} should be <= CE {profile.ce}"
    assert profile.ote_zone_bottom <= profile.ote_zone_top, \
        f"OTE bottom {profile.ote_zone_bottom} > top {profile.ote_zone_top}"
    print("[PASS] test_stdv_ote_zone_direction")


def test_stdv_levels_monotonic():
    """STDV levels must be monotonic in the displacement direction."""
    bars = _make_bearish_manip_fixture()
    mld = _to_mld(bars)
    leg = get_primary_manipulation_leg(mld, bias_direction="BEAR",
                                       pip_size=0.01, min_recent_bars=25)
    profile = compute_stdv_profile(leg, mld)
    # Sort by price; for BEAR they should decrease
    prices = sorted([lv.price for lv in profile.stdv_levels], reverse=True)
    for i in range(1, len(prices)):
        assert prices[i] <= prices[i-1] + 0.001, \
            f"Level {i} {prices[i]} > level {i-1} {prices[i-1]}"
    print("[PASS] test_stdv_levels_monotonic")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: Confluence counting with controlled inputs
# ═══════════════════════════════════════════════════════════════════════════════

def test_confluence_no_leg_zero():
    """Without manipulation leg, confluence must be low."""
    base = 1704067200
    bars = []
    for i in range(40):
        bars.append(SMCBar(time=base + i*900, open=3300.0, high=3300.2, low=3299.8, close=3300.0))
    sel = select_trade(bars, bars, pip_size=0.01)
    # Should be NEUTRAL (no bias, no leg)
    assert sel.direction == "NEUTRAL", f"Expected NEUTRAL, got {sel.direction}"
    print("[PASS] test_confluence_no_leg_zero")


def test_confluence_bearish_fixture():
    """Bearish fixture: at least sweep (C4) and killzone (C6) should hit."""
    bars = _make_bearish_manip_fixture()
    sel = select_trade(bars, bars, amd_phase="DISTRIBUTION", pip_size=0.01)
    assert sel.confluence_count >= 2, f"Expected >=2, got {sel.confluence_count}"
    assert "C4_SWEEP_CONFIRMED" in str(sel.confluence_details)
    assert "C6_KILLZONE_AMD" in str(sel.confluence_details)
    print("[PASS] test_confluence_bearish_fixture")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Kill zone gating
# ═══════════════════════════════════════════════════════════════════════════════

def test_kill_zone_inside():
    """Bars during London open should be in kill zone."""
    base = 1704067200  # 2024-01-01 00:00 UTC
    # Create bars at 08:00 UTC (inside London open 07-10)
    bars = []
    for i in range(4):
        ts = base + 28*900 + i*900  # ~07:00-08:00 UTC
        bars.append(SMCBar(time=ts, open=3300.0, high=3301.0, low=3299.0, close=3300.5))
    kz, reason = _kill_zone_bonus(bars)
    assert kz > 0, f"Expected kill zone bonus >0, got {kz}"
    assert "LONDON" in reason.upper() or "EUROPEAN" in reason.upper(), reason
    print("[PASS] test_kill_zone_inside")


def test_kill_zone_outside():
    """Bars at 03:00 UTC should be outside kill zone."""
    base = 1704067200
    bars = []
    for i in range(4):
        ts = base + 12*900 + i*900  # ~03:00 UTC
        bars.append(SMCBar(time=ts, open=3300.0, high=3301.0, low=3299.0, close=3300.5))
    kz, reason = _kill_zone_bonus(bars)
    assert kz == 0, f"Expected 0 outside KZ, got {kz}"
    assert "outside" in reason.lower() or "no" in reason.lower(), reason
    print("[PASS] test_kill_zone_outside")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: Entry pricing at true OTE/STDV levels
# ═══════════════════════════════════════════════════════════════════════════════

def test_entry_at_ote_not_market():
    """Entry must be at an OTE/STDV level, not the current market price."""
    bars = _make_bearish_manip_fixture()
    sel = select_trade(bars, bars, amd_phase="DISTRIBUTION", pip_size=0.01)
    if sel.entry_price is not None:
        profile = sel.stdv_profile
        # Entry should be near a known level
        nearest, dist = nearest_level(profile, sel.entry_price, sel.direction)
        assert dist < 2.0, f"Entry {sel.entry_price} far from nearest level {nearest.name if nearest else 'none'}"
        print(f"[INFO] Entry {sel.entry_price} near {nearest.name if nearest else 'none'}")
    print("[PASS] test_entry_at_ote_not_market")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: SL placement beyond manipulation leg wick
# ═══════════════════════════════════════════════════════════════════════════════

def test_sl_beyond_manipulation_wick():
    """For BEAR: SL must be above wick_high. For BULL: SL below wick_low."""
    bars = _make_bearish_manip_fixture()
    mld = _to_mld(bars)
    leg = get_primary_manipulation_leg(mld, bias_direction="BEAR",
                                       pip_size=0.01, min_recent_bars=25)
    atr = sum(b.h - b.l for b in mld[-14:]) / 14
    entry = leg.wick_high - 1.0  # fake entry below wick
    sl = _compute_sl(entry, leg, "BEAR", atr)
    assert sl >= leg.wick_high, f"BEAR SL {sl} must be >= wick_high {leg.wick_high}"
    print("[PASS] test_sl_beyond_manipulation_wick")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7: R:R correctness
# ═══════════════════════════════════════════════════════════════════════════════

def test_rr_minimum():
    """Any actionable signal must have R:R >= MIN_RR."""
    bars = _make_bearish_manip_fixture()
    sel = select_trade(bars, bars, amd_phase="DISTRIBUTION", pip_size=0.01)
    if sel.is_actionable:
        risk = abs(sel.entry_price - sel.sl)
        reward = abs(sel.tp - sel.entry_price)
        rr = reward / risk if risk > 0 else 0
        assert rr >= MIN_RR, f"R:R {rr:.2f} < minimum {MIN_RR}"
        print(f"[INFO] R:R = {rr:.2f}")
    print("[PASS] test_rr_minimum")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8: AMD alignment bonuses
# ═══════════════════════════════════════════════════════════════════════════════

def test_amd_distribution_permits():
    """AMD DISTRIBUTION phase should permit trading with bonus."""
    permitted, bonus, reason = _amd_alignment("DISTRIBUTION", "BEAR")
    assert permitted, f"DISTRIBUTION should permit: {reason}"
    assert bonus > 0, f"Expected bonus >0, got {bonus}"
    print("[PASS] test_amd_distribution_permits")


def test_amd_accumulation_blocks():
    """AMD ACCUMULATION phase should block trading."""
    permitted, bonus, reason = _amd_alignment("ACCUMULATION", "BEAR")
    assert not permitted, f"ACCUMULATION should block: {reason}"
    print("[PASS] test_amd_accumulation_blocks")


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    tests = [
        test_bearish_manipulation_leg,
        test_bullish_manipulation_leg,
        test_no_manipulation_leg,
        test_stdv_ce_midpoint,
        test_stdv_ote_zone_direction,
        test_stdv_levels_monotonic,
        test_confluence_no_leg_zero,
        test_confluence_bearish_fixture,
        test_kill_zone_inside,
        test_kill_zone_outside,
        test_entry_at_ote_not_market,
        test_sl_beyond_manipulation_wick,
        test_rr_minimum,
        test_amd_distribution_permits,
        test_amd_accumulation_blocks,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"[ERR ] {t.__name__}: {type(e).__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if failed == 0:
        print("ALL TESTS PASSED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
