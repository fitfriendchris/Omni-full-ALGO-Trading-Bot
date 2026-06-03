#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
redistribution_detector.py — OMNI ICT Production Bot v28.0
Phase 2A: Sweep → CHoCH → FVG Pipeline + Confluence Engine

Detects ICT redistribution setups by chaining MQL5-exported structural events:
  1. LIQUIDITY SWEEP  (wick beyond level, close back inside)
  2. STRUCTURAL BREAK  (CHoCH or BOS in reversal direction)
  3. ENTRY ZONE        (unmitigated FVG in that direction)

Requires: SessionTracker, SweepDetector, StructureDetector, FVGDetector in MQL5
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone, timedelta
import json
import logging
from enum import Enum

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────────────────
class SetupDirection(Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class SessionName(Enum):
    ASIAN = "asian"
    LONDON = "london"
    NY = "ny"
    OFF = "off"


class CyclePhase(Enum):
    ACCUMULATION = "accumulation"
    MANIPULATION = "manipulation"
    DISTRIBUTION = "distribution"
    UNKNOWN = "unknown"


# ── Data classes ───────────────────────────────────────────────────────
@dataclass
class SessionRanges:
    """Parsed from omni_data.json session_ranges block."""
    current_session: str = "off"
    asian_high: Optional[float] = None
    asian_low: Optional[float] = None
    london_high: Optional[float] = None
    london_low: Optional[float] = None
    ny_high: Optional[float] = None
    ny_low: Optional[float] = None
    pdh: Optional[float] = None
    pdl: Optional[float] = None
    p2dh: Optional[float] = None
    p2dl: Optional[float] = None
    weekly_high: Optional[float] = None
    weekly_low: Optional[float] = None
    monthly_high: Optional[float] = None
    monthly_low: Optional[float] = None
    equal_highs: List[float] = field(default_factory=list)
    equal_lows: List[float] = field(default_factory=list)


@dataclass
class SweepEvent:
    """Parsed from omni_data.json sweeps array."""
    type: str          # "bullish" | "bearish"
    level: float
    level_type: str    # "asian_high", "pdh", "equal_high", etc.
    time: datetime
    wick_extreme: float
    body_close: float
    confirmed: bool = True
    volume_ratio: float = 1.0
    multi_touch: bool = False
    touch_count: int = 1


@dataclass
class StructureState:
    """Parsed from omni_data.json structure block."""
    trend: str = "ranging"  # "up" | "down" | "ranging"
    last_swing_high: Optional[float] = None
    last_swing_low: Optional[float] = None
    last_choch_dir: str = "none"   # "bullish" | "bearish" | "none"
    last_choch_time: Optional[datetime] = None
    last_bos_dir: str = "none"     # "bullish" | "bearish" | "none"
    last_bos_time: Optional[datetime] = None
    last_hh: Optional[float] = None
    last_ll: Optional[float] = None
    valid: bool = False


@dataclass
class FVG:
    """Parsed from omni_data.json fvgs array."""
    direction: str       # "bullish" | "bearish"
    top: float
    bottom: float
    size_pips: float
    time: datetime
    mitigated: bool
    optimal_entry: float


@dataclass
class RedistributionSetup:
    """Final output: an A+ setup ready for execution."""
    direction: SetupDirection
    symbol: str
    entry_price: float       # FVG optimal_entry
    stop_loss: float         # Below/above FVG opposite side, floored at 1.5x ATR
    take_profit_1: float     # 3:1 R:R
    take_profit_2: Optional[float]  # Next major liquidity pool
    take_profit_3: Optional[float]  # Extended run target
    size_lots: float         # Calculated by position_sizer
    fvg: Optional[FVG]
    sweep: Optional[SweepEvent]
    structure: Optional[StructureState]
    session: Optional[SessionRanges]
    confluences: int         # 0-10 count
    confluence_list: List[str] = field(default_factory=list)
    setup_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    killzone: str = ""
    cycle_phase: CyclePhase = CyclePhase.UNKNOWN
    h4_bias: str = ""
    d1_bias: str = ""
    confidence: float = 0.0   # 0.0-1.0 derived from confluence count


# ── Configuration ────────────────────────────────────────────────────
class RedistributionConfig:
    # ICT confluence requirements
    MIN_CONFLUENCES: int = 5           # Hard minimum for ANY setup (your rule)
    MIN_CONFLUENCES_A_PLUS: int = 7    # Required for aggressive sizing
    
    # Timing windows
    MAX_SWEEP_AGE_SECONDS: int = 600   # 10 min after sweep for CHoCH
    MAX_CHOCH_AGE_SECONDS: int = 300   # 5 min after CHoCH for FVG
    MAX_FVG_AGE_SECONDS: int = 3600   # FVG valid for 60 min
    
    # Killzone windows (UTC)
    KILLZONE_EUROPEAN_START: int = 7   # 07:00 UTC
    KILLZONE_EUROPEAN_END: int = 12    # 12:00 UTC
    KILLZONE_NY_START: int = 13        # 13:00 UTC
    KILLZONE_NY_END: int = 17          # 17:00 UTC
    
    # Risk management
    MIN_RR: float = 3.0                # Minimum risk:reward
    MAX_SL_ATR_MULTIPLIER: float = 1.5 # 1.5x ATR max SL
    
    # Cycle phase gating
    SKIP_ACCUMULATION: bool = True     # Skip signals during accumulation
    ACCUMULATION_SIZE_PCT: float = 0.5  # Reduce to 50% during accumulation
    MANIPULATION_WAIT_FOR_CHOCH: bool = True  # Must see CHoCH in manipulation phase


# ── JSON Parser ────────────────────────────────────────────────────────
class MQL5DataParser:
    """Parses the MQL5 omni_data.json export containing structural data."""
    
    @staticmethod
    def parse_sessions(raw: Dict) -> SessionRanges:
        sr = raw.get("session_ranges", {})
        return SessionRanges(
            current_session=sr.get("current_session", "off"),
            asian_high=sr.get("asian", {}).get("high"),
            asian_low=sr.get("asian", {}).get("low"),
            london_high=sr.get("london", {}).get("high"),
            london_low=sr.get("london", {}).get("low"),
            ny_high=sr.get("ny", {}).get("high"),
            ny_low=sr.get("ny", {}).get("low"),
            pdh=sr.get("pdh"),
            pdl=sr.get("pdl"),
            p2dh=sr.get("p2dh"),
            p2dl=sr.get("p2dl"),
            weekly_high=sr.get("weekly_high"),
            weekly_low=sr.get("weekly_low"),
            monthly_high=sr.get("monthly_high"),
            monthly_low=sr.get("monthly_low"),
            equal_highs=sr.get("equal_highs", []),
            equal_lows=sr.get("equal_lows", []),
        )
    
    @staticmethod
    def parse_sweeps(raw: Dict) -> List[SweepEvent]:
        sweeps = raw.get("sweeps", [])
        result = []
        for s in sweeps:
            try:
                t = datetime.strptime(s["time"], "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except (ValueError, KeyError):
                t = datetime.now(timezone.utc)
            result.append(SweepEvent(
                type=s.get("type", "bearish"),
                level=s.get("level", 0.0),
                level_type=s.get("level_type", "other"),
                time=t,
                wick_extreme=s.get("wick_extreme", 0.0),
                body_close=s.get("body_close", 0.0),
                confirmed=s.get("confirmed", True),
                volume_ratio=s.get("volume_ratio", 1.0),
                multi_touch=s.get("multi_touch", False),
                touch_count=s.get("touch_count", 1),
            ))
        return result
    
    @staticmethod
    def parse_structure(raw: Dict) -> StructureState:
        st = raw.get("structure", {})
        t_choch = st.get("last_choch_time")
        t_bos = st.get("last_bos_time")
        return StructureState(
            trend=st.get("trend", "ranging"),
            last_swing_high=st.get("last_swing_high"),
            last_swing_low=st.get("last_swing_low"),
            last_choch_dir=st.get("last_choch_dir", "none"),
            last_choch_time=datetime.strptime(t_choch, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc) if t_choch else None,
            last_bos_dir=st.get("last_bos_dir", "none"),
            last_bos_time=datetime.strptime(t_bos, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc) if t_bos else None,
            last_hh=st.get("last_hh"),
            last_ll=st.get("last_ll"),
            valid=st.get("valid", False),
        )
    
    @staticmethod
    def parse_fvgs(raw: Dict) -> List[FVG]:
        fvgs = raw.get("fvgs", [])
        result = []
        for f in fvgs:
            try:
                t = datetime.strptime(f["time"], "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except (ValueError, KeyError):
                t = datetime.now(timezone.utc)
            result.append(FVG(
                direction=f.get("direction", "bullish"),
                top=f.get("top", 0.0),
                bottom=f.get("bottom", 0.0),
                size_pips=f.get("size_pips", 0.0),
                time=t,
                mitigated=f.get("mitigated", False),
                optimal_entry=f.get("optimal_entry", 0.0),
            ))
        return result


# ── Core Confluence Engine ─────────────────────────────────────────────
class RedistributionDetector:
    """
    ICT redistribution setup detector.
    
    Sequences:
      BULLISH:  sweep BEARISH liquidity (below asian_low/pdl/equal_low)
                → CHoCH bullish or BOS bullish on M15
                → bullish FVG unmitigated
                → LONG toward opposing liquidity (asian_high/pdh/etc.)
                
      BEARISH:  sweep BULLISH liquidity (above asian_high/pdh/equal_high)
                → CHoCH bearish or BOS bearish on M15
                → bearish FVG unmitigated
                → SHORT toward opposing liquidity
    """
    
    def __init__(self, config: Optional[RedistributionConfig] = None):
        self.cfg = config or RedistributionConfig()
        self.parser = MQL5DataParser()
        self.last_setup_time: Optional[datetime] = None
        self.recent_setups: List[RedistributionSetup] = []
    
    # ── Main entry point ──────────────────────────────────────────────
    def evaluate(self, omni_data: Dict, atr_14: float, h4_bias: str = "", d1_bias: str = "", cycle_phase: str = "unknown") -> Optional[RedistributionSetup]:
        """
        Given MQL5-exported omni_data.json content, return an A+ setup or None.
        
        Args:
            omni_data: Full parsed JSON dict from MQL5 EA export
            atr_14: Current 14-period ATR in price units (for SL calculation)
            h4_bias: "bullish" | "bearish" | "neutral"
            d1_bias: "bullish" | "bearish" | "neutral"
            cycle_phase: "accumulation" | "manipulation" | "distribution" | "unknown"
        """
        now = datetime.now(timezone.utc)
        symbol = omni_data.get("symbol", "XAUUSD")
        
        # Parse structural data from MQL5 export
        sessions = self.parser.parse_sessions(omni_data)
        sweeps = self.parser.parse_sweeps(omni_data)
        structure = self.parser.parse_structure(omni_data)
        fvgs = self.parser.parse_fvgs(omni_data)
        
        # Phase gating
        phase = CyclePhase(cycle_phase) if cycle_phase in [p.value for p in CyclePhase] else CyclePhase.UNKNOWN
        if phase == CyclePhase.ACCUMULATION and self.cfg.SKIP_ACCUMULATION:
            logger.info("REDIST: Skipping — accumulation phase")
            return None
        
        # Killzone gating
        current_hour = now.hour
        in_european = self.cfg.KILLZONE_EUROPEAN_START <= current_hour <= self.cfg.KILLZONE_EUROPEAN_END
        in_ny = self.cfg.KILLZONE_NY_START <= current_hour <= self.cfg.KILLZONE_NY_END
        if not (in_european or in_ny):
            logger.debug("REDIST: Outside killzone hours")
            return None
        killzone = "european" if in_european else "ny"
        
        # ── STEP 1: SWEEP DETECTION ─────────────────────────────────────
        # Must have a recent, confirmed sweep
        valid_sweep = self._find_valid_sweep(sweeps, now)
        if not valid_sweep:
            logger.debug("REDIST: No valid recent sweep")
            return None
        
        sweep_dir = valid_sweep.type  # "bullish" sweep = swept below, expect UP
        
        # ── STEP 2: STRUCTURAL CONFIRMATION ─────────────────────────────
        # After sweep, we need CHoCH or BOS in the SAME direction as the sweep
        has_structure = self._check_structure_confirmation(structure, sweep_dir, now)
        if not has_structure:
            logger.debug("REDIST: Sweep present but no CHoCH/BOS confirmation")
            return None
        
        # ── STEP 3: FVG ENTRY ZONE ──────────────────────────────────────
        # Find unmitigated FVG in the direction we want to trade
        target_fvg = self._find_entry_fvg(fvgs, sweep_dir, valid_sweep.level, now)
        if not target_fvg:
            logger.debug("REDIST: Structure confirmed but no valid FVG")
            return None
        
        # ── STEP 4: CONFLUENCE COUNTING ───────────────────────────────────
        confluences, confluence_list = self._count_confluences(
            valid_sweep, structure, target_fvg, sessions,
            h4_bias, d1_bias, phase, killzone, atr_14
        )
        
        if confluences < self.cfg.MIN_CONFLUENCES:
            logger.info(f"REDIST: Setup found but only {confluences}/{self.cfg.MIN_CONFLUENCES} confluences")
            return None
        
        # ── STEP 5: CALCULATE ENTRY / SL / TP ────────────────────────────
        direction = SetupDirection.LONG if sweep_dir == "bullish" else SetupDirection.SHORT
        entry = target_fvg.optimal_entry
        
        # SL: Below/above FVG opposite side, floored at 1.5x ATR
        if direction == SetupDirection.LONG:
            sl_raw = target_fvg.bottom - (symbol_tick_size(symbol) * 5)  # 5 ticks below FVG
            sl_floor = entry - (atr_14 * self.cfg.MAX_SL_ATR_MULTIPLIER)
            sl = max(sl_raw, sl_floor)
        else:
            sl_raw = target_fvg.top + (symbol_tick_size(symbol) * 5)
            sl_floor = entry + (atr_14 * self.cfg.MAX_SL_ATR_MULTIPLIER)
            sl = min(sl_raw, sl_floor)
        
        risk = abs(entry - sl)
        if risk <= 0:
            logger.warning("REDIST: Zero risk calculated, aborting")
            return None
        
        # TP1: 3:1 minimum R:R
        tp1 = entry + (risk * self.cfg.MIN_RR) if direction == SetupDirection.LONG else entry - (risk * self.cfg.MIN_RR)
        
        # TP2: Next major opposing liquidity pool
        tp2 = self._calculate_liquidity_target(direction, sessions, valid_sweep.level_type)
        
        # TP3: Extended (2x TP1 distance)
        tp3 = entry + (risk * self.cfg.MIN_RR * 2) if direction == SetupDirection.LONG else entry - (risk * self.cfg.MIN_RR * 2)
        
        # Override TP2 if it would give less than 3:1
        if direction == SetupDirection.LONG and tp2:
            if (tp2 - entry) / risk < self.cfg.MIN_RR:
                tp2 = None
        elif direction == SetupDirection.SHORT and tp2:
            if (entry - tp2) / risk < self.cfg.MIN_RR:
                tp2 = None
        
        # ── STEP 6: CONFIDENCE SCORING ───────────────────────────────────
        confidence = min(1.0, confluences / 10.0)
        
        setup = RedistributionSetup(
            direction=direction,
            symbol=symbol,
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            size_lots=0.0,  # Set by position sizer
            fvg=target_fvg,
            sweep=valid_sweep,
            structure=structure,
            session=sessions,
            confluences=confluences,
            confluence_list=confluence_list,
            setup_time=now,
            killzone=killzone,
            cycle_phase=phase,
            h4_bias=h4_bias,
            d1_bias=d1_bias,
            confidence=confidence,
        )
        
        self.last_setup_time = now
        self.recent_setups.append(setup)
        if len(self.recent_setups) > 100:
            self.recent_setups = self.recent_setups[-50:]
        
        logger.info(f"REDIST: A+ Setup detected — {direction.value.upper()} {confluences}/10 confluences, confidence={confidence:.2f}")
        return setup
    
    # ── Internal helpers ────────────────────────────────────────────────
    def _find_valid_sweep(self, sweeps: List[SweepEvent], now: datetime) -> Optional[SweepEvent]:
        """Find most recent confirmed sweep within expiry window."""
        for sweep in reversed(sweeps):
            if not sweep.confirmed:
                continue
            age = (now - sweep.time).total_seconds()
            if age <= self.cfg.MAX_SWEEP_AGE_SECONDS:
                return sweep
        return None
    
    def _check_structure_confirmation(self, structure: StructureState, sweep_dir: str, now: datetime) -> bool:
        """
        After a bullish sweep (swept below), we need:
          - CHoCH bullish (trend changed from down to up)
          - OR BOS bullish (continuation breaking new HH)
        After a bearish sweep (swept above), we need:
          - CHoCH bearish (trend changed from up to down)
          - OR BOS bearish (continuation breaking new LL)
        """
        if not structure.valid:
            return False
        
        # Check CHoCH
        if structure.last_choch_time:
            choch_age = (now - structure.last_choch_time).total_seconds()
            if choch_age <= self.cfg.MAX_CHOCH_AGE_SECONDS:
                if sweep_dir == "bullish" and structure.last_choch_dir == "bullish":
                    return True
                if sweep_dir == "bearish" and structure.last_choch_dir == "bearish":
                    return True
        
        # Check BOS (continuation is also valid)
        if structure.last_bos_time:
            bos_age = (now - structure.last_bos_time).total_seconds()
            if bos_age <= self.cfg.MAX_CHOCH_AGE_SECONDS:  # Same window for freshness
                if sweep_dir == "bullish" and structure.last_bos_dir == "bullish":
                    return True
                if sweep_dir == "bearish" and structure.last_bos_dir == "bearish":
                    return True
        
        return False
    
    def _find_entry_fvg(self, fvgs: List[FVG], sweep_dir: str, sweep_level: float, now: datetime) -> Optional[FVG]:
        """
        Find unmitigated FVG matching the trade direction, within valid age,
        and ideally in the direction of the sweep's opposing liquidity.
        """
        valid_fvgs = []
        for fvg in fvgs:
            if fvg.mitigated:
                continue
            age = (now - fvg.time).total_seconds()
            if age > self.cfg.MAX_FVG_AGE_SECONDS:
                continue
            
            # Direction must match sweep implication
            if sweep_dir == "bullish" and fvg.direction != "bullish":
                continue
            if sweep_dir == "bearish" and fvg.direction != "bearish":
                continue
            
            valid_fvgs.append(fvg)
        
        if not valid_fvgs:
            return None
        
        # Prefer largest, most recent FVG closest to current price
        valid_fvgs.sort(key=lambda f: (f.size_pips, -(now - f.time).total_seconds()), reverse=True)
        return valid_fvgs[0]
    
    def _count_confluences(self, sweep: SweepEvent, structure: StructureState, fvg: FVG,
                           sessions: SessionRanges, h4_bias: str, d1_bias: str,
                           phase: CyclePhase, killzone: str, atr: float) -> Tuple[int, List[str]]:
        confluences = 0
        cl = []
        
        # 1. Sweep confirmed and recent
        confluences += 1
        cl.append("sweep_confirmed")
        if sweep.multi_touch:
            confluences += 1
            cl.append("multi_touch_sweep")
        
        # 2. Structural CHoCH/BOS within window
        confluences += 1
        cl.append("structural_break")
        
        # 3. Unmitigated FVG present
        confluences += 1
        cl.append("unmitigated_fvg")
        
        # 4. Killzone timing
        confluences += 1
        cl.append(f"killzone_{killzone}")
        
        # 5. Session liquidity context
        if sweep.level_type in ("asian_high", "asian_low", "pdh", "pdl"):
            confluences += 1
            cl.append(f"major_liquidity_{sweep.level_type}")
        elif sweep.level_type in ("equal_high", "equal_low"):
            confluences += 1
            cl.append("equal_level_liquidity")
        
        # 6. H4 bias alignment
        if h4_bias and ((sweep.type == "bullish" and h4_bias == "bullish") or (sweep.type == "bearish" and h4_bias == "bearish")):
            confluences += 1
            cl.append("h4_bias_aligned")
        
        # 7. D1 bias alignment
        if d1_bias and ((sweep.type == "bullish" and d1_bias == "bullish") or (sweep.type == "bearish" and d1_bias == "bearish")):
            confluences += 1
            cl.append("d1_bias_aligned")
        
        # 8. Cycle phase (distribution = best, manipulation = good with CHoCH)
        if phase == CyclePhase.DISTRIBUTION:
            confluences += 1
            cl.append("distribution_phase")
        elif phase == CyclePhase.MANIPULATION:
            confluences += 0  # Neutral, already gated by CHoCH requirement
        
        # 9. FVG size (reward potential)
        if fvg.size_pips >= atr / SymbolInfoDouble("XAUUSD", SYMBOL_POINT) / 10.0 * 0.3:
            confluences += 1
            cl.append("fvg_size_significant")
        
        # 10. Volume signature on sweep (if available)
        if sweep.volume_ratio > 1.5:
            confluences += 1
            cl.append("volume_spike_on_sweep")
        
        return confluences, cl
    
    def _calculate_liquidity_target(self, direction: SetupDirection, sessions: SessionRanges, sweep_level_type: str) -> Optional[float]:
        """
        After sweeping one level, target the opposing major liquidity.
        
        Bullish sweep of asian_low → target asian_high or pdh (whichever is nearest)
        Bearish sweep of asian_high → target asian_low or pdl
        """
        targets = []
        if direction == SetupDirection.LONG:
            # We went long after sweeping below. Target: highs above.
            if sessions.asian_high:
                targets.append(("asian_high", sessions.asian_high))
            if sessions.pdh:
                targets.append(("pdh", sessions.pdh))
            if sessions.london_high and sweep_level_type in ("asian_low", "pdl"):
                targets.append(("london_high", sessions.london_high))
        else:
            # We went short after sweeping above. Target: lows below.
            if sessions.asian_low:
                targets.append(("asian_low", sessions.asian_low))
            if sessions.pdl:
                targets.append(("pdl", sessions.pdl))
            if sessions.london_low and sweep_level_type in ("asian_high", "pdh"):
                targets.append(("london_low", sessions.london_low))
        
        if not targets:
            return None
        
        # Return nearest opposing liquidity
        # This is simplistic; could be enhanced with ATR distance weighting
        if direction == SetupDirection.LONG:
            return min((t[1] for t in targets))
        else:
            return max((t[1] for t in targets))


# ── Helper for tick size ───────────────────────────────────────────────
# Note: In real usage, this gets SymbolInfoDouble from MT5 export or a wrapper
def symbol_tick_size(symbol: str) -> float:
    """Return tick size for symbol. XAUUSD ≈ 0.01."""
    if "XAU" in symbol or "GOLD" in symbol:
        return 0.01
    if "XAG" in symbol or "SILVER" in symbol:
        return 0.001
    return 0.00001  # Forex default


def SymbolInfoDouble(symbol: str, prop: int) -> float:
    """Stub — replaced by actual MT5 bridge in production."""
    if prop == 0:  # SYMBOL_POINT placeholder
        return 0.01
    return 0.01


# ── Standalone test ────────────────────────────────────────────────────
if __name__ == "__main__":
    import unittest
    
    class TestRedistributionDetector(unittest.TestCase):
        def setUp(self):
            self.det = RedistributionDetector()
        
        def test_no_sweep_returns_none(self):
            result = self.det.evaluate({"symbol": "XAUUSD", "sweeps": []}, atr_14=5.0)
            self.assertIsNone(result)
        
        def test_sweep_without_structure_returns_none(self):
            now = datetime.now(timezone.utc)
            data = {
                "symbol": "XAUUSD",
                "sweeps": [{
                    "type": "bullish",
                    "level": 3300.0,
                    "level_type": "asian_low",
                    "time": now.strftime("%Y.%m.%d %H:%M:%S"),
                    "wick_extreme": 3298.5,
                    "body_close": 3301.0,
                    "confirmed": True,
                }],
                "structure": {"valid": False, "trend": "ranging", "last_choch_dir": "none"},
                "fvgs": [],
                "session_ranges": {"current_session": "london"},
            }
            result = self.det.evaluate(data, atr_14=5.0)
            self.assertIsNone(result)
    
    unittest.main(verbosity=2, exit=False)
