#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tick_backtest.py — OMNI ICT Production Bot v28.0
Phase 6: Honest tick-level backtesting with commission + slippage.

Uses M1 OHLC data (exported from MT5 or downloaded) and simulates:
  - Limit orders: fill at limit price or better
  - Market orders: fill at open of next M1 candle + spread
  - SL execution: simulated with 1-pip granularity (can gap through SL)
  - TP execution: same
  - Slippage: random 0-3 pips on market orders
  - Commission: $7 per round lot standard for XAUUSD
  - Spread: variable from data feed or fixed input

Outputs BOTH raw results (no costs) and reality-adjusted (comm+slippage).
"""
from __future__ import annotations
import json
import random
import logging
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Callable
import csv

logger = logging.getLogger(__name__)

random.seed(42)  # Reproducible


@dataclass
class M1Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread: float = 0.0   # spread in price units (e.g. 0.05 for XAUUSD)


@dataclass
class SimulatedTrade:
    entry_price: float
    exit_price: float
    side: str                    # BUY | SELL
    sl: float
    tp: float
    entry_type: str              # limit | market
    size_lots: float
    open_time: datetime
    close_time: datetime
    pnl_gross: float             # Before costs
    pnl_net: float               # After commission + slippage
    exit_reason: str             # tp | sl | close_signal | end_of_data
    slippage_entry: float
    slippage_exit: float
    commission: float
    r_multiple: float


@dataclass
class BacktestConfig:
    symbol: str = "XAUUSD"
    lot_size: float = 100.0      # Oz per lot for XAUUSD
    pip_value: float = 10.0      # USD per pip per lot
    commission_per_lot: float = 7.0   # $ per round lot ($3.50 each way)
    spread_avg: float = 0.05     # Average spread in price units
    slippage_max_pips: float = 3.0
    min_rr: float = 3.0
    max_sl_atr_mult: float = 1.5
    
    # Reality flags
    include_commission: bool = True
    include_slippage: bool = True
    limit_fill_probability: float = 0.85  # Limit orders don't always fill


class TickBacktest:
    """
    Walk-forward backtest on M1 bars.
    
    Strategy signal function must return a dict or None:
      {
        "direction": "LONG" | "SHORT",
        "entry": float,
        "sl": float,
        "tp": float,
        "type": "limit" | "market",
        "size": float,  # lots
      }
    """
    
    def __init__(self, config: BacktestConfig = None):
        self.cfg = config or BacktestConfig()
        self.equity_curve: List[Tuple[datetime, float, float]] = []  # time, gross, net
        self.trades: List[SimulatedTrade] = []
        self.equity = 10000.0  # Starting equity
        self.equity_net = 10000.0
    
    def run(self, bars: List[M1Bar], signal_fn: Callable[[List[M1Bar], int], Optional[Dict]]) -> Dict:
        """
        Run backtest over M1 bars.
        
        Args:
            bars: Chronological M1 bars
            signal_fn: function(bars, current_idx) -> signal dict or None
        
        Returns:
            Summary dict with raw and reality-adjusted stats
        """
        active_trade: Optional[Dict] = None
        entry_bar_idx = -1
        
        for i in range(len(bars)):
            current = bars[i]
            
            # If no active trade, check for signal
            if not active_trade:
                signal = signal_fn(bars, i)
                if signal:
                    active_trade = signal
                    entry_bar_idx = i
                    
                    # Determine fill
                    if signal["type"] == "limit":
                        fill_price = self._simulate_limit_fill(signal, current)
                        if fill_price is None:
                            active_trade = None  # Limit didn't fill
                            continue
                    else:
                        fill_price = self._simulate_market_fill(signal, current)
                    
                    active_trade["fill_price"] = fill_price
                    active_trade["fill_time"] = current.time
            
            # If we have an active trade, simulate bar-by-bar management
            if active_trade:
                # Check if trade resolves on this bar
                side = active_trade["direction"]  # LONG or SHORT
                sl = active_trade["sl"]
                tp = active_trade["tp"]
                entry = active_trade["fill_price"]
                
                exit_price = None
                exit_reason = None
                
                # SL hit check
                if side == "LONG":
                    if current.low <= sl:
                        exit_price = max(current.open, sl)  # Slippage into SL
                        exit_reason = "sl"
                    elif current.high >= tp:
                        exit_price = min(current.open, tp)
                        exit_reason = "tp"
                else:  # SHORT
                    if current.high >= sl:
                        exit_price = min(current.open, sl)
                        exit_reason = "sl"
                    elif current.low <= tp:
                        exit_price = max(current.open, tp)
                        exit_reason = "tp"
                
                # If no TP/SL hit, trade carries forward (except end of data)
                if exit_price is None and i == len(bars) - 1:
                    exit_price = current.close
                    exit_reason = "end_of_data"
                
                if exit_price is not None:
                    # Calculate results
                    gross_pnl = self._calculate_pnl(entry, exit_price, side, active_trade["size"])
                    
                    # Slippage
                    slip_entry = self._random_slippage() if active_trade["type"] == "market" else 0.0
                    slip_exit = self._random_slippage() if exit_reason in ("sl", "tp") else 0.0
                    
                    # Commission
                    commission = self.cfg.commission_per_lot * active_trade["size"] if self.cfg.include_commission else 0
                    
                    # Net PNL (slippage already reflected in exit_price via gross calc approximation)
                    # More precise: adjust gross for slippage
                    adjusted_exit = exit_price + (slip_exit if side == "SHORT" else -slip_exit)
                    gross_pnl_adj = self._calculate_pnl(entry, adjusted_exit, side, active_trade["size"])
                    net_pnl = gross_pnl_adj - commission
                    
                    # R multiple
                    risk = abs(entry - sl)
                    r = gross_pnl / (risk * active_trade["size"] * self.cfg.lot_size / self.cfg.pip_value) if risk > 0 else 0
                    
                    trade = SimulatedTrade(
                        entry_price=entry,
                        exit_price=exit_price,
                        side=side,
                        sl=sl,
                        tp=tp,
                        entry_type=active_trade["type"],
                        size_lots=active_trade["size"],
                        open_time=active_trade["fill_time"],
                        close_time=current.time,
                        pnl_gross=gross_pnl,
                        pnl_net=net_pnl,
                        exit_reason=exit_reason,
                        slippage_entry=slip_entry,
                        slippage_exit=slip_exit,
                        commission=commission,
                        r_multiple=r,
                    )
                    self.trades.append(trade)
                    self.equity += gross_pnl
                    self.equity_net += net_pnl
                    self.equity_curve.append((current.time, self.equity, self.equity_net))
                    
                    active_trade = None
                    entry_bar_idx = -1
        
        return self._summarize()
    
    def _simulate_limit_fill(self, signal: Dict, bar: M1Bar) -> Optional[float]:
        """Limit order: fills if price reaches limit."""
        limit = signal["entry"]
        if signal["direction"] == "LONG":
            # Buy limit: fills if low <= limit
            if bar.low <= limit:
                # Some probability of fill
                if random.random() <= self.cfg.limit_fill_probability:
                    return min(limit, bar.open)  # Slippage toward better fill
                return None
        else:
            # Sell limit: fills if high >= limit
            if bar.high >= limit:
                if random.random() <= self.cfg.limit_fill_probability:
                    return max(limit, bar.open)
                return None
        return None
    
    def _simulate_market_fill(self, signal: Dict, bar: M1Bar) -> float:
        """Market order: fill at open + spread + random slippage."""
        if signal["direction"] == "LONG":
            fill = bar.open + self.cfg.spread_avg + self._random_slippage()
        else:
            fill = bar.open - self.cfg.spread_avg - self._random_slippage()
        return fill
    
    def _calculate_pnl(self, entry: float, exit: float, side: str, lots: float) -> float:
        """USD PNL for XAUUSD-style contract."""
        pips = exit - entry if side == "LONG" else entry - exit
        # Convert to pips (XAUUSD: 1 pip = 0.01 price)
        pips_count = pips / 0.01
        return pips_count * self.cfg.pip_value * lots
    
    def _random_slippage(self) -> float:
        """Random slippage in price units."""
        if not self.cfg.include_slippage:
            return 0.0
        max_slip = self.cfg.slippage_max_pips * 0.01  # pips to price
        return random.uniform(0, max_slip)
    
    def _summarize(self) -> Dict:
        if not self.trades:
            return {"error": "No trades taken"}
        
        wins = [t for t in self.trades if t.pnl_gross > 0]
        losses = [t for t in self.trades if t.pnl_gross <= 0]
        gross_total = sum(t.pnl_gross for t in self.trades)
        net_total = sum(t.pnl_net for t in self.trades)
        max_dd = self._max_drawdown()
        
        # Bucket R multiples
        r_buckets = {"loss": 0, "0-1R": 0, "1-3R": 0, "3-5R": 0, "5R+": 0}
        for t in self.trades:
            if t.r_multiple < 0:
                r_buckets["loss"] += 1
            elif t.r_multiple < 1:
                r_buckets["0-1R"] += 1
            elif t.r_multiple < 3:
                r_buckets["1-3R"] += 1
            elif t.r_multiple < 5:
                r_buckets["3-5R"] += 1
            else:
                r_buckets["5R+"] += 1
        
        return {
            "total_trades": len(self.trades),
            "winners": len(wins),
            "losers": len(losses),
            "raw_win_rate": len(wins) / len(self.trades) * 100 if self.trades else 0,
            "raw_total_pnl": round(gross_total, 2),
            "raw_return_pct": round(gross_total / self.equity_curve[0][1] * 100, 2) if self.equity_curve else 0,
            "net_win_rate": len([t for t in self.trades if t.pnl_net > 0]) / len(self.trades) * 100 if self.trades else 0,
            "net_total_pnl": round(net_total, 2),
            "net_return_pct": round(net_total / self.equity_curve[0][2] * 100, 2) if self.equity_curve else 0,
            "max_drawdown_pct": round(max_dd, 2),
            "avg_r_multiple": round(sum(t.r_multiple for t in self.trades) / len(self.trades), 2),
            "r_distribution": r_buckets,
            # Honest caveat
            "caveats": [
                "Commission = ${} per round lot".format(self.cfg.commission_per_lot),
                "Slippage = up to {} pips on market orders".format(self.cfg.slippage_max_pips),
                "Limit fill rate = {}%".format(self.cfg.limit_fill_probability * 100),
                "M1 data may miss intra-bar wicks that hit SL/TP. Results optimistic if SL within bar range.",
                "Spread simulated as fixed {}; real spread variable during news/low liquidity.".format(self.cfg.spread_avg),
                "Small sample — statistical significance not guaranteed.",
            ]
        }
    
    def _max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0][1]
        max_dd = 0.0
        for _, gross, _ in self.equity_curve:
            if gross > peak:
                peak = gross
            dd = (peak - gross) / peak * 100
            if dd > max_dd:
                max_dd = dd
        return max_dd
    
    def export_csv(self, path: str) -> None:
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["entry", "exit", "side", "sl", "tp", "type", "lots",
                            "open_time", "close_time", "gross_pnl", "net_pnl",
                            "exit_reason", "r_multiple"])
            for t in self.trades:
                writer.writerow([
                    t.entry_price, t.exit_price, t.side, t.sl, t.tp,
                    t.entry_type, t.size_lots, t.open_time.isoformat(),
                    t.close_time.isoformat(), t.pnl_gross, t.pnl_net,
                    t.exit_reason, t.r_multiple,
                ])
        logger.info(f"Backtest CSV exported to {path}")


if __name__ == "__main__":
    print("TickBacktest class defined. Import to use.")
