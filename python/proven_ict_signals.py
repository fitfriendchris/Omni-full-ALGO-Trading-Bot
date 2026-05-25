"""
proven_ict_signals.py — Live signal generator for OMNI orchestrator
Based on deterministic_ict_proven_backtest.py v5 with progressive profit lock.

Exposes generate_signals_for_symbol() matching deterministic_ict_engine.py API.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Tuple
from pathlib import Path

try:
    from deterministic_ict_proven_backtest import (
        EngineConfig, Engine, Bar, Sig,
        PIP_VALUES, SESSIONS
    )
except ImportError:
    raise RuntimeError("proven_ict_signals requires deterministic_ict_proven_backtest.py")

import yfinance as yf
import pandas as pd

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "proven_ict_config_final_v4.json"


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ── Public API ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Signal:
    """Signal envelope matching deterministic_ict_engine.Signal."""
    symbol: str
    direction: str
    entry: float
    sl: float
    tp: float
    confidence: float
    timestamp: str
    reason: str
    capped: bool = False
    strength: float = 0.0

    # ── orchestrator compatibility properties ─────────────────────────
    @property
    def id(self) -> str:          return f"PROVEN-{self.symbol}-{self.direction}"
    @property
    def ts(self) -> str:           return self.timestamp
    @property
    def entry_price(self) -> float:return self.entry
    @property
    def entry_type(self) -> str:   return "LIMIT"
    @property
    def timeframe(self) -> str:    return "H1"
    @property
    def htf_bias(self) -> str:     return self.direction
    @property
    def reasons(self) -> List[str]:return [self.reason]
    @property
    def grade(self) -> str:
        return "A" if self.strength >= 0.7 else ("B" if self.strength >= 0.5 else "C")
    @property
    def phase(self) -> str:        return "DISTRIBUTION"
    @property
    def invalidation(self) -> float:return self.sl
    @property
    def rr_ratio(self) -> float:
        return abs(self.tp - self.entry) / abs(self.entry - self.sl) if self.entry != self.sl else 0.0
    @property
    def session(self) -> str:      return "LONDON"

    def __repr__(self) -> str:
        return f"Signal({self.symbol} {self.direction} E={self.entry:.2f} SL={self.sl:.2f} TP={self.tp:.2f} S={self.strength:.2f})"


DetSignal = Signal  # legacy alias


# ── helpers ──────────────────────────────────────────────────────────
def _charts_to_data(symbol: str, charts: dict,
                    htf_tf: str = "D1", ltf_tf: str = "H1") -> Tuple[List[Bar], List[Bar]]:
    """Convert {symbol: {D1: [...], H1: [...]}} → (ltf, htf)."""
    sym_charts = charts.get(symbol, {})
    raw_ltf = sym_charts.get(ltf_tf, sym_charts.get("H1", []))
    raw_htf = sym_charts.get(htf_tf, sym_charts.get("D1", []))
    if not raw_ltf or not raw_htf:
        return [], []

    def _to_bar(d):
        t = d.get("time", 0)
        if isinstance(t, (int, float)):
            t = float(t)
        elif isinstance(t, str):
            # Orchestrator passes str(b.time); if it looks like an epoch,
            # convert to float so _to_ts() can apply unit="s".
            import re
            if re.match(r'^\d+(?:\.\d+)?$', t):
                try:
                    f = float(t)
                    if f > 1_000_000_000:
                        t = f
                except ValueError:
                    pass
        else:
            t = 0.0
        return Bar(
            t,
            float(d.get("o", d.get("open", 0))),
            float(d.get("h", d.get("high", 0))),
            float(d.get("l", d.get("low", 0))),
            float(d.get("c", d.get("close", 0))),
            float(d.get("v", d.get("volume", 0))),
        )
    return [_to_bar(b) for b in raw_ltf], [_to_bar(b) for b in raw_htf]


def _get_session_label(broker_ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(broker_ts, tz=timezone.utc)
        h = dt.hour
        if 7 <= h < 10:   return "LONDON"
        if 13 <= h < 17:  return "NY"
        if 0 <= h < 6:    return "TOKYO"
    except Exception:
        pass
    return "LONDON"


def fetch_live_data(symbol: str, period: str = "1mo", interval: str = "1h") -> Tuple[List[Bar], List[Bar]]:
    tickers = {"XAUUSD": "GC=F", "EURUSD": "EURUSD=X", "NAS100": "^NDX", "XAGUSD": "SI=F"}
    ticker = tickers.get(symbol, symbol)

    df = yf.Ticker(ticker).history(period=period, interval=interval)
    if df.empty:
        return [], []
    df.reset_index(inplace=True)
    if "Datetime" in df.columns:
        df.rename(columns={"Datetime": "Date"}, inplace=True)
    df["Date"] = pd.to_datetime(df["Date"], utc=True)
    ltf = [Bar(r["Date"], float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]), float(r.get("Volume", 0))) for _, r in df.iterrows()]

    df_d = yf.Ticker(ticker).history(period=period, interval="1d")
    df_d.reset_index(inplace=True)
    if "Datetime" in df_d.columns:
        df_d.rename(columns={"Datetime": "Date"}, inplace=True)
    df_d["Date"] = pd.to_datetime(df_d["Date"], utc=True)
    htf = [Bar(r["Date"], float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]), float(r.get("Volume", 0))) for _, r in df_d.iterrows()]
    return ltf, htf


def align_htf(ltf: List[Bar], htf: List[Bar]) -> List[Bar]:
    """Date-align HTF bars to LTF using nearest-timestamp lookup."""
    if not htf:
        return []
    from bisect import bisect_right

    def _to_ts(val):
        if isinstance(val, (int, float)):
            return pd.Timestamp(val, unit="s", tz="UTC")
        return pd.Timestamp(val)

    htf_times = [_to_ts(b.time) for b in htf]
    result = []
    for bar in ltf:
        idx = bisect_right(htf_times, _to_ts(bar.time)) - 1
        if idx < 0:
            idx = 0
        result.append(htf[idx])
    return result


# ── main API ─────────────────────────────────────────────────────────
def generate_signals_for_symbol(
    symbol: str = "XAUUSD",
    charts: Optional[dict] = None,
    broker_ts: float = 0.0,
    session_window: str = "EUROPEAN",
    max_spread_pips: float = 50.0,
    min_rr: float = 2.0,
    lookback: int = 50,
    sl_cap_pips: float = 200.0,
    stop_buffer_pips: float = 2.0,
    fill_window: int = 96,
    execution_mode: str = "MARKET_ONLY",
    signal_strength_threshold: float = 0.5,
    stricter_slippage: bool = True,
    data: Optional[Tuple[List[Bar], List[Bar]]] = None,
    **_ignore,
) -> List[Signal]:
    """
    Orchestrator-compatible API.
      generate_signals_for_symbol(symbol, charts, broker_ts, ...)
    Back-compat: data=(ltf, htf) tuple still supported.
    """
    if data:
        ltf, htf = data
    elif charts:
        ltf, htf = _charts_to_data(symbol, charts)
    else:
        ltf, htf = fetch_live_data(symbol, period="3mo")

    if not ltf or not htf:
        return []

    session = session_window or _get_session_label(broker_ts)
    cfg = EngineConfig(
        execution_mode=execution_mode,
        sl_cap_pips=sl_cap_pips,
        fill_window=fill_window,
        session=session,
        min_rr=min_rr,
        lookback=lookback,
        stop_buffer_pips=stop_buffer_pips,
        max_spread_pips=max_spread_pips,
        stricter_slippage=stricter_slippage,
        signal_strength_threshold=signal_strength_threshold,
    )

    eng = Engine(symbol)
    raw = eng.generate(ltf, align_htf(ltf, htf), cfg)

    out = []
    for s in raw:
        ts_val = ltf[s.bar_idx].time if s.bar_idx is not None and 0 <= s.bar_idx < len(ltf) else ""
        ss = getattr(s, "signal_strength", 0.5)
        out.append(Signal(
            symbol=symbol, direction=s.direction, entry=s.entry,
            sl=s.sl, tp=s.tp2, confidence=ss,
            timestamp=str(ts_val),
            reason=f"strength={ss:.2f}_disp={getattr(s, 'bar_range_pips', 0):.0f}p_" + ("CAPPED" if s.sl_capped else "RAW"),
            capped=s.sl_capped, strength=ss,
        ))
    return out


generate_signals_for_symbol_with_config = generate_signals_for_symbol


# ── quick self-test ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("proven_ict_signals.py — quick self-test")
    ltf, htf = fetch_live_data("XAUUSD", period="3mo")
    print(f"  Bars LTF={len(ltf)} HTF={len(htf)}")
    sigs = generate_signals_for_symbol("XAUUSD")
    print(f"  Signals: {len(sigs)}")
    for s in sigs[:3]:
        print(f"    {s}")
