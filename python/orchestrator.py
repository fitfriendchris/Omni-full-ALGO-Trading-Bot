"""
orchestrator.py — Per-cycle glue for OMNI-ICT (Phase 3 / Phase 4).

Responsibility
--------------
Once per cycle (driven externally — cron, LaunchAgent, or a while-loop in
auto_trader.py), for each symbol in rules.watchlist:

    1. Load HTF bars (default H1) and LTF bars (default M5).
    2. Run smc_engine.analyze() on each to get SMCSnapshots.
    3. Ask dual_tf_selector.select_trade() for a TradeSelection.
    4. If a position is open for this symbol, ask scaling_engine.evaluate()
       for an ADD/REDUCE/HOLD/CLOSE recommendation.
    5. Wrap into a unified Signal envelope and write signals.json +
       omni_pine_overlay.pine atomically.

The orchestrator is deliberately thin. It owns no detection logic — it only
composes pure functions from the engines and writes to disk.

Design notes
------------
 * Bar fetching is pluggable via a `BarFetcher` Protocol so this module can be
   driven by either the live MT5 connector or an offline fixture for tests and
   paper-forward simulation.
 * If rules.dual_tf.enabled is False, the orchestrator still runs but will
   emit only NEUTRAL/non-actionable signals — useful for sanity-checking the
   pipeline against live data without executing trades.
 * All file paths come from rules.signals (with inline fallback) so no code
   changes are needed to relocate the outputs.
 * No network or MT5 calls live here; keep this module pure-enough to unit test.

CLI
---
    python orchestrator.py --dry-run            # run one cycle on fixtures
    python orchestrator.py --symbols EURUSD     # limit to one symbol
    python orchestrator.py --loop 60            # run forever, one cycle / 60s
"""

from __future__ import annotations

import argparse
import concurrent.futures as _cf
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

# Local imports — pure modules, safe to import at module load.
from smc_engine import Bar, analyze, _make_fixture
from dual_tf_selector import select_trade, TradeSelection
from scaling_engine import evaluate as scale_evaluate, PositionCtx, ScaleAction
from signal_writers import (
    Signal, build_signal, build_signals_payload,
    write_signals_json, prune_signals,
)
from pine_codegen import write_pine
from amd_engine import detect_amd, AMDPhase, pip_size_for

log = logging.getLogger("orchestrator")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
DEFAULT_RULES_PATH = HERE / "rules.json"
DEFAULT_SIGNALS_DIR = PROJECT_ROOT / "shared"
DEFAULT_PINE_PATH = PROJECT_ROOT / "pine" / "omni_pine_overlay.pine"

# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 Confluence Engine — SINGLE signal path (no parallel confusion)
#
# The dual_tf_selector v3 is the ONLY signal source. It implements:
#   - Manipulation leg detection → STDV/OTE anchoring
#   - True confluence counting (min 3 of 6)
#   - Hard kill-zone gate + AMD alignment
#   - Limit-order entry pricing at true OTE/STDV levels
#
# Proven deterministic engine (proven_ict_signals.py) may still be loaded
# as an advisory layer for confidence boost, but it does NOT emit
# separate signals. All downstream consumers see one unified stream.
# ═══════════════════════════════════════════════════════════════════════════════

# Advisory proven engine — optional confidence overlay
try:
    from proven_ict_signals import (
        generate_signals_for_symbol as _proven_generate,
        Signal as _ProvenSig,
    )
    _HAVE_PROVEN = True
except Exception:
    _HAVE_PROVEN = False
    _proven_generate = None  # type: ignore
    _ProvenSig = None        # type: ignore


# ──────────────────────────────────────────────────────────────────────────────
# Pluggable bar fetcher
# ──────────────────────────────────────────────────────────────────────────────

class BarFetcher(Protocol):
    """Fetch recent bars for a given symbol + timeframe."""
    def fetch(self, symbol: str, timeframe: str, n: int) -> list[Bar]: ...


def _fetch_with_timeout(fetcher: BarFetcher, symbol: str, tf: str, n: int,
                        timeout_s: float = 30.0) -> list[Bar]:
    """Fetch bars with a hard timeout; raises TimeoutError if the fetcher hangs."""
    with _cf.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fetcher.fetch, symbol, tf, n)
        return fut.result(timeout=timeout_s)


_Y2000_TS = 946684800.0  # Unix timestamp for 2000-01-01; bars before this are fixtures


TF_AGE_S: dict[str, int] = {
    "M1": 120, "M5": 300, "M15": 600, "H1": 3600, "H4": 18000, "D1": 87000,
}

def bars_are_fresh_for_tf(bars: list[Bar], tf: str) -> bool:
    """Timeframe-aware freshness check."""
    if not bars:
        return False
    max_age = TF_AGE_S.get(tf, 3600)
    newest = max((b.time for b in bars if b.time), default=0.0)
    if newest < _Y2000_TS:
        return True  # fixture / synthetic data — skip staleness check
    return (time.time() - newest) < max_age


# Backward compat: alias without timeframe
_bars_are_fresh = lambda bars: bars_are_fresh_for_tf(bars, "H1")


class FixtureBarFetcher:
    """Fake fetcher used by --dry-run and tests."""
    def fetch(self, symbol: str, timeframe: str, n: int) -> list[Bar]:
        bars = _make_fixture()
        return bars[-n:] if n and n < len(bars) else bars


class MT5BarFetcher:
    """
    Thin adapter over mt5_connector. Imported lazily so that the orchestrator
    can run on machines without the MT5 terminal installed (e.g. CI).
    """
    def __init__(self):
        self._mod = None

    def _lazy(self):
        if self._mod is None:
            import importlib
            try:
                self._mod = importlib.import_module("mt5_connector")
            except Exception as e:  # pragma: no cover
                raise RuntimeError(f"mt5_connector not available: {e}") from e
        return self._mod

    def _raw_data(self) -> dict:
        """Return latest full MT5 data dict (includes amd_phase, timestamp, etc)."""
        mod = self._lazy()
        try:
            return mod.load_with_retry(max_attempts=3)
        except Exception:
            return {}

    def fetch(self, symbol: str, timeframe: str, n: int) -> list[Bar]:
        mod = self._lazy()
        raw = mod.get_bars(symbol, timeframe, n)
        bars = []
        for r in raw:
            # Prefer the broker-offset-corrected UTC timestamp set by
            # mt5_connector.get_bars(). Fall back to local parsing for
            # backward compat with older callers that don't surface it.
            t = r.get("time_utc")
            if t is None or t == 0:
                raw_t = r.get("time", 0)
                if isinstance(raw_t, str):
                    try:
                        t = datetime.strptime(raw_t, "%Y.%m.%d %H:%M:%S").replace(
                            tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        t = 0.0
                else:
                    t = float(raw_t or 0)
            bars.append(Bar(
                time=float(t),
                open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]),   close=float(r["close"]),
            ))
        return bars


# ──────────────────────────────────────────────────────────────────────────────
# Position state loader (optional)
# ──────────────────────────────────────────────────────────────────────────────

def _load_open_positions(path: Optional[Path]) -> dict[str, PositionCtx]:
    """
    Load open positions keyed by symbol. Returns {} when file missing.
    Format (JSON): [{"symbol":"EURUSD","direction":"BULL","entry":1.10,"sl":1.099,"current":1.102,"size":0.1,"add_count":0}, …]
    """
    if path is None or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("could not parse open positions %s: %s", path, e)
        return {}
    out: dict[str, PositionCtx] = {}
    for r in raw or []:
        try:
            initial_sl = float(r.get("initial_sl", r.get("sl", r["entry"])))
            out[r["symbol"]] = PositionCtx(
                symbol=r["symbol"],
                direction=r["direction"],
                entry_price=float(r["entry"]),
                current_price=float(r.get("current", r["entry"])),
                initial_sl=initial_sl,
                current_sl=float(r.get("current_sl", r.get("sl", initial_sl))),
                volume=float(r.get("volume", r.get("size", 0.01))),
                add_count=int(r.get("add_count", 0)),
            )
        except Exception as e:  # skip malformed rows
            log.warning("skipping bad position row %s: %s", r, e)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Cycle result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CycleResult:
    ts:        str
    symbols:   list[str]
    signals:   list[Signal]
    written:   list[str]
    errors:    list[str]

    def as_dict(self) -> dict:
        return {
            "ts":      self.ts,
            "symbols": self.symbols,
            "signals": [s.id for s in self.signals],
            "written": self.written,
            "errors":  self.errors,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Core: run one cycle
# ──────────────────────────────────────────────────────────────────────────────

def run_cycle(
    rules: dict,
    *,
    fetcher: BarFetcher,
    symbols: Optional[list[str]] = None,
    open_positions: Optional[dict[str, PositionCtx]] = None,
    signals_path: Optional[Path] = None,
    pine_path: Optional[Path] = None,
    max_kept: int = 20,
    n_htf: int = 200,
    n_ltf: int = 300,
) -> CycleResult:
    """
    Execute one end-to-end cycle and emit signals.json + pine overlay.

    Parameters
    ----------
    rules : dict      — parsed rules.json
    fetcher : BarFetcher — source of bars (MT5 / fixtures / mock)
    symbols : optional subset of rules.watchlist
    open_positions : dict keyed by symbol, optional
    signals_path : override output location for signals.json
    pine_path    : override output location for .pine file
    """
    open_positions = open_positions or {}
    watchlist = symbols or rules.get("watchlist", [])
    dt_cfg = rules.get("dual_tf", {}) or {}
    htf_tf  = dt_cfg.get("htf_timeframe",   "H1")
    ltf_tf  = dt_cfg.get("ltf_timeframe",   "M5")
    # Macro timeframe for D1/H4 cascade confirmation (skipped on error/fixture)
    macro_tf = dt_cfg.get("macro_timeframe", "H4")

    ts_now = datetime.now(timezone.utc)
    ts_iso = ts_now.isoformat()

    produced: list[Signal] = []
    errors:   list[str] = []
    written:  list[str] = []

    # Optional asset rotation — drop out-of-session symbols before scanning.
    try:
        from asset_rotation_manager import AssetRotationManager
        _arm = AssetRotationManager(symbols=watchlist)
        _filtered = [s for s in watchlist if not _arm.should_skip_asset(s)]
        if _filtered:
            watchlist = _filtered
    except Exception:
        pass

    # Optional regime detector — runs once per HTF series, attached to signal
    # metadata so consumers (auto_trader, dashboard) can adapt.
    try:
        from regime_detector import detect_regime as _detect_regime
        _have_regime = True
    except Exception:
        _have_regime = False
        detect_regime = None  # type: ignore

    # Pull MT5-wide metadata (amd_phase) once per cycle if using MT5BarFetcher
    mt5_amd_phase = ""
    try:
        if hasattr(fetcher, "_raw_data"):
            _raw = fetcher._raw_data()  # type: ignore
            if _raw:
                mt5_amd_phase = _raw.get("amd_phase", "")
                if mt5_amd_phase:
                    log.info("MT5 EA amd_phase for cycle: %s", mt5_amd_phase)
    except Exception:
        pass

    for symbol in watchlist:
        try:
            htf_bars   = _fetch_with_timeout(fetcher, symbol, htf_tf,   n_htf)
            ltf_bars   = _fetch_with_timeout(fetcher, symbol, ltf_tf,   n_ltf)
            # Fetch macro bars for H4/D1 cascade; silently skip on failure
            macro_bars = None
            try:
                macro_bars = _fetch_with_timeout(fetcher, symbol, macro_tf, 100)
            except Exception:
                pass

            # Freshness check — skip if bars haven't been updated recently
            if not bars_are_fresh_for_tf(htf_bars, htf_tf):
                # Diagnostic: when staleness fires, log enough info to
                # pinpoint why. (Cheap — only on the failure path.)
                if htf_bars:
                    times = [b.time for b in htf_bars if b.time]
                    if times:
                        newest = max(times)
                        oldest = min(times)
                        age_min = (time.time() - newest) / 60.0
                        log.warning(
                            "stale-debug %s: bars=%d, newest=%.0f (age=%+.1fmin), "
                            "oldest=%.0f, now=%.0f",
                            symbol, len(htf_bars), newest, age_min,
                            oldest, time.time(),
                        )
                    else:
                        log.warning("stale-debug %s: bars=%d but ALL b.time are 0/falsy",
                                    symbol, len(htf_bars))
                else:
                    log.warning("stale-debug %s: htf_bars is EMPTY", symbol)
                errors.append(f"{symbol}: stale HTF bars (oldest > 1h)")
                log.warning("stale HTF bars for %s — skipping cycle", symbol)
                continue

            # AMD scan — use MT5-wide EA phase first, then per-symbol fallback
            amd_phase = mt5_amd_phase
            if not amd_phase:
                try:
                    amd_cfg = rules.get("amd", {})
                    if amd_cfg.get("enabled", True):
                        m15_bars = _fetch_with_timeout(fetcher, symbol, "M15", 200)
                        if bars_are_fresh_for_tf(m15_bars, "M15"):
                            ps = pip_size_for(symbol)
                            from amd_engine import detect_amd
                            amd = detect_amd(
                                m15_bars,
                                pip_size=ps,
                                asian_start_h=amd_cfg.get("asian_start_h", 0),
                                asian_end_h=amd_cfg.get("asian_end_h", 7),
                                min_confidence=amd_cfg.get("min_confidence", 0.50),
                            )
                            if amd is not None:
                                amd_phase = amd.phase.value
                                log.info(
                                    "AMD %s: phase=%s dir=%s conf=%.2f",
                                    symbol, amd.phase.value, amd.direction, amd.confidence,
                                )
                except Exception as _amd_err:
                    log.debug("amd scan skipped for %s: %s", symbol, _amd_err)

            # ═══════════════════════════════════════════════════════════════
            # Phase 4: Single-path confluence engine
            # ═══════════════════════════════════════════════════════════════
            sel: TradeSelection = select_trade(
                htf_bars, ltf_bars,
                rules=dt_cfg or None,
                macro_bars=macro_bars,
                amd_phase=amd_phase,
                pip_size=pip_size_for(symbol),
            )

            # Optional proven engine advisory (does NOT emit separate signals)
            proven_boost = 0.0
            proven_meta = {}
            if _HAVE_PROVEN and _proven_generate is not None:
                try:
                    def _bars_to_raw(bb):
                        return [
                            {"time": str(b.time), "o": b.open, "h": b.high,
                             "l": b.low, "c": b.close, "v": getattr(b, "volume", 0),
                             "broker_ts": float(b.time) if isinstance(b.time, (int, float)) else 0.0}
                            for b in bb
                        ]
                    charts = {
                        symbol: {
                            htf_tf: _bars_to_raw(htf_bars),
                            ltf_tf: _bars_to_raw(ltf_bars),
                            macro_tf: _bars_to_raw(macro_bars) if macro_bars else [],
                        }
                    }
                    det_results = _proven_generate(
                        symbol, charts,
                        broker_ts=float(ts_now.timestamp()),
                        session_window="EUROPEAN",
                        max_spread_pips=50.0,
                        min_rr=2.0,
                        lookback=50,
                        sl_cap_pips=200.0,
                        stop_buffer_pips=2.0,
                        fill_window=96,
                    )
                    if det_results:
                        d = det_results[0]
                        if d.direction == sel.direction:
                            proven_boost = min(0.10, d.confidence * 0.15)
                            proven_meta = {
                                "proven_aligned": True,
                                "proven_confidence": round(d.confidence, 3),
                                "proven_grade": getattr(d, "grade", ""),
                                "proven_boost": round(proven_boost, 3),
                            }
                        else:
                            proven_meta = {
                                "proven_aligned": False,
                                "proven_direction": d.direction,
                                "sel_direction": sel.direction,
                            }
                except Exception as _det_err:
                    log.debug("proven advisory skipped for %s: %s", symbol, _det_err)

            if proven_boost > 0 and sel.is_actionable:
                sel.confidence = min(1.0, sel.confidence + proven_boost)
                sel.reasons.append(f"Proven engine alignment boost +{proven_boost:.3f}")

            # ── Compute EMA20/200/800 on ALL available timeframes ────────
            tf_emas = {}
            for tf_name, tf_n in [
                ("M5",  1800), ("M15", 600), ("M30", 400),
                ("H1",   300), ("H4",  100), ("D1",    60), ("W1",  20),
            ]:
                try:
                    tf_bars = _fetch_with_timeout(fetcher, symbol, tf_name, tf_n)
                    if tf_bars and len(tf_bars) >= 20:
                        from ict_precision import _calc_ema
                        closes = [b.close for b in tf_bars]
                        emas_tf = {"ema20": round(_calc_ema(closes, 20)[-1], 5)}
                        if len(tf_bars) >= 200:
                            emas_tf["ema200"] = round(_calc_ema(closes, 200)[-1], 5)
                        if len(tf_bars) >= 800:
                            emas_tf["ema800"] = round(_calc_ema(closes, 800)[-1], 5)
                        tf_emas[tf_name] = emas_tf
                except Exception:
                    pass
            sel.tf_emas = tf_emas

            scale_act: Optional[ScaleAction] = None
            pos = open_positions.get(symbol)
            if pos is not None:
                htf_snap = analyze(htf_bars)
                ltf_snap = analyze(ltf_bars)
                scale_act = scale_evaluate(pos, htf_snap, ltf_snap,
                                           rules=rules.get("scaling"))

            # ── Emit signal (single path) ─────────────────────────────────
            if sel.is_actionable:
                sig = build_signal(symbol, ltf_tf, sel, scale=scale_act, ts=ts_now)

                if hasattr(sig, "metadata") and isinstance(sig.metadata, dict):
                    sig.metadata["confluence"] = {
                        "count": sel.confluence_count,
                        "details": sel.confluence_details,
                        "manipulation_leg": {
                            "type": sel.manipulation_leg.leg_type if sel.manipulation_leg else "none",
                            "direction": sel.manipulation_leg.direction if sel.manipulation_leg else "none",
                            "wick_high": sel.manipulation_leg.wick_high if sel.manipulation_leg else 0,
                            "wick_low": sel.manipulation_leg.wick_low if sel.manipulation_leg else 0,
                            "quality": sel.manipulation_leg.is_high_quality if sel.manipulation_leg else False,
                        },
                        "stdv": {
                            "ce": sel.stdv_profile.ce if sel.stdv_profile else 0,
                            "stdv_unit": sel.stdv_profile.stdv if sel.stdv_profile else 0,
                            "ote_zone": [sel.stdv_profile.ote_zone_bottom, sel.stdv_profile.ote_zone_top]
                                     if sel.stdv_profile else [0, 0],
                        },
                        "proven": proven_meta,
                    }
                produced.append(sig)
                log.info(
                    "SIGNAL %s %s confluence=%d conf=%.3f entry=%s SL=%s TP=%s",
                    symbol, sel.direction, sel.confluence_count,
                    sel.confidence, sel.entry_price, sel.sl, sel.tp,
                )
            else:
                log.info(
                    "NO-SIGNAL %s: confluence=%d conf=%.3f — %s",
                    symbol, sel.confluence_count, sel.confidence,
                    sel.reasons[-1] if sel.reasons else "no confluence",
                )
        except Exception as e:
            errors.append(f"{symbol}: {type(e).__name__}: {e}")
            log.exception("cycle error for %s", symbol)

    sig_cfg = rules.get("signals", {})

    # Prune to bound file size — rules.signals.max_kept overrides caller default
    effective_max = max_kept if max_kept != 20 else int(sig_cfg.get("max_kept", 20))
    kept = prune_signals(produced, max_kept=effective_max)

    # Write outputs — rules.signals paths override module-level defaults
    s_path = (Path(signals_path) if signals_path else
              PROJECT_ROOT / sig_cfg.get("output_dir", "shared") / "signals.json")
    p_path = (Path(pine_path) if pine_path else
              PROJECT_ROOT / sig_cfg.get("pine_path", "pine/omni_pine_overlay.pine"))

    try:
        payload = build_signals_payload(kept)
        write_signals_json(payload, str(s_path))
        written.append(str(s_path))
    except Exception as e:
        errors.append(f"write_signals_json: {e}")
        log.exception("write_signals_json failed")

    try:
        write_pine(kept, str(p_path))
        written.append(str(p_path))
    except Exception as e:
        errors.append(f"write_pine: {e}")
        log.exception("write_pine failed")

    return CycleResult(
        ts=ts_iso,
        symbols=list(watchlist),
        signals=kept,
        written=written,
        errors=errors,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _load_rules(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_fetcher(dry_run: bool) -> BarFetcher:
    if dry_run:
        return FixtureBarFetcher()
    try:
        return MT5BarFetcher()
    except Exception as e:
        log.warning("MT5 not available (%s); falling back to FixtureBarFetcher", e)
        return FixtureBarFetcher()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="OMNI-ICT orchestrator cycle runner")
    p.add_argument("--dry-run", action="store_true", help="use fixtures instead of MT5")
    p.add_argument("--symbols", nargs="+", help="subset of watchlist to run")
    p.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                   help="run forever, sleep N seconds between cycles (0 = single run)")
    p.add_argument("--rules", default=str(DEFAULT_RULES_PATH))
    p.add_argument("--positions", default="", help="optional JSON file with open positions")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    rules = _load_rules(Path(args.rules))
    fetcher = _build_fetcher(args.dry_run)
    pos_path = Path(args.positions) if args.positions else None

    def _once() -> CycleResult:
        positions = _load_open_positions(pos_path)
        res = run_cycle(rules, fetcher=fetcher, symbols=args.symbols,
                        open_positions=positions)
        print(json.dumps(res.as_dict(), indent=2))
        return res

    if args.loop <= 0:
        res = _once()
        return 0 if not res.errors else 1

    # Loop mode
    while True:
        try:
            _once()
        except KeyboardInterrupt:
            log.info("orchestrator stopped by user")
            return 0
        except Exception:
            log.exception("cycle failed; continuing")
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
