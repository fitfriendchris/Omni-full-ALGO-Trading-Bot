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

log = logging.getLogger("orchestrator")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
DEFAULT_RULES_PATH = HERE / "rules.json"
DEFAULT_SIGNALS_DIR = PROJECT_ROOT / "shared"
DEFAULT_PINE_PATH = PROJECT_ROOT / "pine" / "omni_pine_overlay.pine"


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


def _bars_are_fresh(bars: list[Bar], max_age_s: int = 3600) -> bool:
    """Return False if the newest bar is older than max_age_s seconds.
    Bars with timestamps near the Unix epoch (e.g. fixture data) are always considered fresh."""
    if not bars:
        return False
    newest = max((b.time for b in bars if b.time), default=0.0)
    if newest < _Y2000_TS:
        return True  # fixture / synthetic data — skip staleness check
    return (time.time() - newest) < max_age_s


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

    def fetch(self, symbol: str, timeframe: str, n: int) -> list[Bar]:
        mod = self._lazy()
        raw = mod.get_bars(symbol, timeframe, n)
        bars = []
        for r in raw:
            t = r.get("time", 0)
            if isinstance(t, str):
                try:
                    # MT5 format: "2026.04.19 00:00:00"
                    t = datetime.strptime(t, "%Y.%m.%d %H:%M:%S").replace(
                        tzinfo=timezone.utc).timestamp()
                except ValueError:
                    t = 0.0
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
            out[r["symbol"]] = PositionCtx(
                direction=r["direction"],
                entry_price=float(r["entry"]),
                current_price=float(r.get("current", r["entry"])),
                sl=float(r["sl"]),
                size=float(r.get("size", 0.0)),
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
    htf_tf = dt_cfg.get("htf_timeframe", "H1")
    ltf_tf = dt_cfg.get("ltf_timeframe", "M5")

    ts_now = datetime.now(timezone.utc)
    ts_iso = ts_now.isoformat()

    produced: list[Signal] = []
    errors:   list[str] = []
    written:  list[str] = []

    for symbol in watchlist:
        try:
            htf_bars = _fetch_with_timeout(fetcher, symbol, htf_tf, n_htf)
            ltf_bars = _fetch_with_timeout(fetcher, symbol, ltf_tf, n_ltf)

            # Freshness check — skip if bars haven't been updated recently
            if not _bars_are_fresh(htf_bars):
                errors.append(f"{symbol}: stale HTF bars (oldest > 1h)")
                log.warning("stale HTF bars for %s — skipping cycle", symbol)
                continue

            # select_trade runs analyze() internally on the raw bars, so pass
            # bars in directly. We also compute snapshots for the scaling engine.
            htf_snap = analyze(htf_bars)
            ltf_snap = analyze(ltf_bars)

            sel: TradeSelection = select_trade(htf_bars, ltf_bars, rules=dt_cfg or None)

            scale_act: Optional[ScaleAction] = None
            pos = open_positions.get(symbol)
            if pos is not None:
                scale_act = scale_evaluate(pos, htf_snap, ltf_snap,
                                           rules=rules.get("scaling"))

            sig = build_signal(symbol, ltf_tf, sel, scale=scale_act, ts=ts_now)
            produced.append(sig)
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
