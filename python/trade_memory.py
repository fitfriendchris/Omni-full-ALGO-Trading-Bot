"""
trade_memory.py — OMNI AI Trade Journal & Adaptive Learning Engine

Every trade taken is logged in full detail (entry reasons, confluence, patterns,
session, AMD phase, quarter position).  On close, the outcome is recorded.
The engine then:
  - Calculates win rates by setup type / session / pattern / quarter
  - Adjusts confidence scores for future setups based on historical performance
  - Generates plain-English learned rules from what is actually working
  - Scales confidence bonuses / penalties automatically

Persists to: /Users/owner/Desktop/omni-ict/python/trade_memory.json

Usage:
    from trade_memory import TradeMemory
    memory = TradeMemory()
    trade_id = memory.log_entry(setup, reasons, pattern_info, quarter_info)
    memory.record_close(trade_id, profit_usd, r_multiple, close_reason)
    adj = memory.get_confidence_adjustment(entry_type, session, pattern)
    rules = memory.generate_learned_rules()
"""

from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

MEMORY_PATH = "/Users/owner/Desktop/omni-ict/python/trade_memory.json"
LOG_PATH    = "/Users/owner/Desktop/omni-ict/python/trade_journal.log"
MIN_SAMPLE  = 8   # Minimum trades before adjusting confidence for a category

log = logging.getLogger("TradeMemory")


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """Full record of one trade from signal detection through close."""

    # Identity
    trade_id:        str   = ""          # Unique ID: symbol_timestamp
    symbol:          str   = ""
    direction:       str   = ""          # BUY / SELL
    entry_type:      str   = ""          # OB_RETEST, FVG_FILL, SWEEP_HIGH_OB, etc.

    # Prices
    entry_price:     float = 0.0
    sl_price:        float = 0.0
    tp1_price:       float = 0.0
    tp2_price:       float = 0.0
    tp3_price:       float = 0.0
    close_price:     float = 0.0

    # Context
    session:         str   = ""          # ASIA / LONDON / NEW_YORK / NY_CLOSE
    amd_phase:       str   = ""          # ACCUMULATION / MANIPULATION / DISTRIBUTION
    d1_bias:         str   = ""          # BULLISH / BEARISH / NEUTRAL
    h4_structure:    str   = ""          # BOS_BULLISH / BOS_BEARISH / RANGING
    quarter:         str   = ""          # Q1 / Q2 / Q3 / Q4 (where entry sits in daily range)
    quarter_pct:     float = 0.0         # 0-100% exact position in daily range

    # Detected patterns (list of str)
    patterns:        list  = field(default_factory=list)   # ["DOUBLE_BOTTOM", "FALLING_WEDGE"]
    push_exhaustion: str   = ""          # PUSH / EXHAUSTION / NEUTRAL

    # Confluence reasons (text list)
    reasons:         list  = field(default_factory=list)
    confluence_count: int  = 0           # Number of independent factors

    # Scores
    confidence:      int   = 0           # Raw confidence score 0-100
    adj_confidence:  int   = 0           # Memory-adjusted confidence
    rr_ratio:        float = 0.0

    # Position sizing
    lot_size:        float = 0.0
    risk_pct:        float = 0.0
    risk_usd:        float = 0.0

    # Scale-in tracking
    is_scale_in:     bool  = False
    parent_trade_id: str   = ""

    # Outcome
    status:          str   = "OPEN"      # OPEN / WIN / LOSS / BREAKEVEN / MANUAL_CLOSE
    profit_usd:      float = 0.0
    r_multiple:      float = 0.0         # Actual R achieved (negative if loss)
    tp_level_hit:    str   = ""          # TP1 / TP2 / TP3 / SL / MANUAL
    close_reason:    str   = ""          # Short description

    # Timestamps
    open_time:       str   = ""
    close_time:      str   = ""
    duration_mins:   float = 0.0

    # Invalidation tracking
    invalidation:    float = 0.0
    was_invalidated: bool  = False


@dataclass
class PerformanceBucket:
    """Win-rate stats for a single grouping key (session, type, pattern, etc.)"""
    wins:         int   = 0
    losses:       int   = 0
    breakevens:   int   = 0
    total_r:      float = 0.0    # Sum of R multiples
    best_r:       float = 0.0
    worst_r:      float = 0.0
    total_profit: float = 0.0

    @property
    def total(self) -> int:
        return self.wins + self.losses + self.breakevens

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total * 100) if self.total > 0 else 0.0

    @property
    def avg_r(self) -> float:
        return (self.total_r / self.total) if self.total > 0 else 0.0

    @property
    def expectancy(self) -> float:
        """(win_rate × avg_win_R) - (loss_rate × avg_loss_R)"""
        return self.avg_r  # Simplified: total_r / total captures both


# ── Main Memory Engine ─────────────────────────────────────────────────────────

class TradeMemory:
    """
    Persistent trade journal + adaptive confidence engine.

    Performance tracking keys:
      by_entry_type  — "OB_RETEST", "FVG_FILL", "SWEEP_HIGH_OB", etc.
      by_session     — "ASIA", "LONDON", "NEW_YORK", "NY_CLOSE"
      by_amd         — "ACCUMULATION", "MANIPULATION", "DISTRIBUTION"
      by_pattern     — "DOUBLE_BOTTOM", "HEAD_SHOULDERS", etc.
      by_quarter     — "Q1", "Q2", "Q3", "Q4"
      by_direction   — "BUY", "SELL"
    """

    def __init__(self, path: str = MEMORY_PATH):
        self.path = path
        self.trades: dict[str, TradeRecord] = {}
        self.by_entry_type: dict[str, PerformanceBucket] = {}
        self.by_session:    dict[str, PerformanceBucket] = {}
        self.by_amd:        dict[str, PerformanceBucket] = {}
        self.by_pattern:    dict[str, PerformanceBucket] = {}
        self.by_quarter:    dict[str, PerformanceBucket] = {}
        self.by_direction:  dict[str, PerformanceBucket] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            for tid, td in data.get("trades", {}).items():
                r = TradeRecord()
                for k, v in td.items():
                    if hasattr(r, k):
                        setattr(r, k, v)
                self.trades[tid] = r
            self._rebuild_buckets()
            log.info(f"TradeMemory loaded: {len(self.trades)} trades")
        except Exception as e:
            log.error(f"TradeMemory load error: {e}")

    def save(self):
        try:
            data = {
                "trades": {tid: asdict(t) for tid, t in self.trades.items()},
                "last_save": datetime.now(timezone.utc).isoformat(),
                "summary": self._summary_dict(),
            }
            with open(self.path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error(f"TradeMemory save error: {e}")

    def _rebuild_buckets(self):
        """Rebuild all performance buckets from raw trade records."""
        self.by_entry_type.clear()
        self.by_session.clear()
        self.by_amd.clear()
        self.by_pattern.clear()
        self.by_quarter.clear()
        self.by_direction.clear()
        for t in self.trades.values():
            if t.status in ("WIN", "LOSS", "BREAKEVEN"):
                self._update_buckets(t)

    def _update_buckets(self, t: TradeRecord):
        self._update_bucket(self.by_entry_type, t.entry_type, t)
        self._update_bucket(self.by_session,    t.session,     t)
        self._update_bucket(self.by_amd,        t.amd_phase,   t)
        self._update_bucket(self.by_direction,  t.direction,   t)
        if t.quarter:
            self._update_bucket(self.by_quarter, t.quarter, t)
        for p in t.patterns:
            self._update_bucket(self.by_pattern, p, t)

    def _update_bucket(self, buckets: dict, key: str, t: TradeRecord):
        if not key:
            return
        if key not in buckets:
            buckets[key] = PerformanceBucket()
        b = buckets[key]
        if t.status == "WIN":
            b.wins += 1
        elif t.status == "LOSS":
            b.losses += 1
        else:
            b.breakevens += 1
        b.total_r      += t.r_multiple
        b.best_r        = max(b.best_r,  t.r_multiple)
        b.worst_r       = min(b.worst_r, t.r_multiple)
        b.total_profit += t.profit_usd

    # ── Logging ───────────────────────────────────────────────────────────────

    def log_entry(self,
                  symbol: str,
                  direction: str,
                  entry_type: str,
                  entry_price: float,
                  sl_price: float,
                  tp1_price: float,
                  tp2_price: float,
                  tp3_price: float,
                  confidence: int,
                  reasons: list,
                  session: str = "",
                  amd_phase: str = "",
                  d1_bias: str = "",
                  h4_structure: str = "",
                  quarter: str = "",
                  quarter_pct: float = 0.0,
                  patterns: list = None,
                  push_exhaustion: str = "",
                  rr_ratio: float = 0.0,
                  lot_size: float = 0.0,
                  risk_pct: float = 0.0,
                  risk_usd: float = 0.0,
                  invalidation: float = 0.0,
                  is_scale_in: bool = False,
                  parent_trade_id: str = "",
                  adj_confidence: int = 0) -> str:
        """
        Record a new trade entry.  Returns the trade_id.
        """
        ts = datetime.now(timezone.utc)
        trade_id = f"{symbol}_{ts.strftime('%Y%m%d_%H%M%S')}"
        if is_scale_in:
            trade_id += "_SCALE"

        adj_conf = adj_confidence if adj_confidence > 0 else confidence

        t = TradeRecord(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            entry_type=entry_type,
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            tp3_price=tp3_price,
            session=session,
            amd_phase=amd_phase,
            d1_bias=d1_bias,
            h4_structure=h4_structure,
            quarter=quarter,
            quarter_pct=quarter_pct,
            patterns=patterns or [],
            push_exhaustion=push_exhaustion,
            reasons=reasons,
            confluence_count=len(reasons),
            confidence=confidence,
            adj_confidence=adj_conf,
            rr_ratio=rr_ratio,
            lot_size=lot_size,
            risk_pct=risk_pct,
            risk_usd=risk_usd,
            invalidation=invalidation,
            is_scale_in=is_scale_in,
            parent_trade_id=parent_trade_id,
            status="OPEN",
            open_time=ts.isoformat(),
        )

        self.trades[trade_id] = t
        self.save()

        # Write detailed journal entry
        self._write_journal(t, "ENTRY")
        log.info(f"[MEMORY] Trade logged: {trade_id} | {direction} {symbol} @ {entry_price}")
        return trade_id

    def record_close(self,
                     trade_id: str,
                     close_price: float,
                     profit_usd: float,
                     r_multiple: float,
                     tp_level_hit: str = "",
                     close_reason: str = "",
                     was_invalidated: bool = False):
        """
        Record trade outcome.  Updates performance buckets and saves.
        """
        t = self.trades.get(trade_id)
        if not t:
            log.warning(f"[MEMORY] Trade {trade_id} not found — cannot record close")
            return

        now = datetime.now(timezone.utc)
        t.close_price    = close_price
        t.profit_usd     = profit_usd
        t.r_multiple     = r_multiple
        t.tp_level_hit   = tp_level_hit
        t.close_reason   = close_reason
        t.was_invalidated = was_invalidated
        t.close_time     = now.isoformat()

        # Duration in minutes
        try:
            open_dt = datetime.fromisoformat(t.open_time)
            t.duration_mins = (now - open_dt).total_seconds() / 60
        except Exception:
            pass

        # Classify outcome
        if r_multiple > 0.1:
            t.status = "WIN"
        elif r_multiple < -0.1:
            t.status = "LOSS"
        else:
            t.status = "BREAKEVEN"

        self._update_buckets(t)
        self.save()
        self._write_journal(t, "CLOSE")

        win_emoji = "WIN" if t.status == "WIN" else ("BE" if t.status == "BREAKEVEN" else "LOSS")
        log.info(
            f"[MEMORY] Trade closed: {trade_id} | {win_emoji} | "
            f"R={r_multiple:+.2f} | P&L=${profit_usd:+.2f}"
        )

    # ── Adaptive Confidence ───────────────────────────────────────────────────

    def get_confidence_adjustment(self,
                                  entry_type: str,
                                  session: str = "",
                                  patterns: list = None,
                                  quarter: str = "",
                                  amd_phase: str = "") -> int:
        """
        Returns an integer to add/subtract from raw confidence based on
        historical performance.  Requires MIN_SAMPLE trades per bucket.

        Rules:
          ≥ 70% win rate → +10 bonus
          ≥ 60% win rate → +5 bonus
          ≤ 35% win rate → -15 penalty
          ≤ 45% win rate → -8 penalty
          avg R > 2.0   → +5 bonus (good trade quality)
          avg R < 0.8   → -5 penalty
        """
        total_adj = 0
        applied = []

        def _adj_from_bucket(b: PerformanceBucket, label: str) -> int:
            if b.total < MIN_SAMPLE:
                return 0
            adj = 0
            wr = b.win_rate
            if wr >= 70:    adj += 10
            elif wr >= 60:  adj += 5
            elif wr <= 35:  adj -= 15
            elif wr <= 45:  adj -= 8
            if b.avg_r >= 2.0: adj += 5
            elif b.avg_r < 0.8: adj -= 5
            if adj != 0:
                applied.append(f"{label}: WR={wr:.0f}% avgR={b.avg_r:.1f} → {adj:+d}")
            return adj

        if entry_type and entry_type in self.by_entry_type:
            total_adj += _adj_from_bucket(self.by_entry_type[entry_type], f"type:{entry_type}")

        if session and session in self.by_session:
            total_adj += _adj_from_bucket(self.by_session[session], f"session:{session}")

        if amd_phase and amd_phase in self.by_amd:
            total_adj += _adj_from_bucket(self.by_amd[amd_phase], f"amd:{amd_phase}")

        if quarter and quarter in self.by_quarter:
            total_adj += _adj_from_bucket(self.by_quarter[quarter], f"quarter:{quarter}")

        for p in (patterns or []):
            if p in self.by_pattern:
                total_adj += _adj_from_bucket(self.by_pattern[p], f"pattern:{p}")

        if applied:
            log.debug(f"[MEMORY] Confidence adj {total_adj:+d}: {' | '.join(applied)}")

        return max(-30, min(25, total_adj))  # Hard cap: -30 to +25

    # ── Learned Rules Generator ───────────────────────────────────────────────

    def generate_learned_rules(self) -> list[dict]:
        """
        Analyzes all performance buckets and generates plain-English rule
        suggestions.  Returns a list of dicts with 'rule', 'evidence', 'action'.
        """
        rules = []

        def _check_bucket(buckets: dict, category: str):
            for key, b in buckets.items():
                if b.total < MIN_SAMPLE:
                    continue
                wr = b.win_rate
                avg_r = b.avg_r

                if wr >= 65 and avg_r >= 1.5:
                    rules.append({
                        "rule": f"HIGH PROBABILITY: {category} = {key}",
                        "evidence": f"{b.wins}W/{b.losses}L ({wr:.0f}% WR), avg R={avg_r:.2f}, {b.total} trades",
                        "action": f"Increase confidence by +10 for {category}={key} setups",
                        "priority": "HIGH"
                    })
                elif wr <= 40 and b.total >= MIN_SAMPLE:
                    rules.append({
                        "rule": f"AVOID / REDUCE SIZE: {category} = {key}",
                        "evidence": f"{b.wins}W/{b.losses}L ({wr:.0f}% WR), avg R={avg_r:.2f}, {b.total} trades",
                        "action": f"Reduce confidence by -15 or skip {category}={key} setups",
                        "priority": "HIGH"
                    })
                elif wr >= 55 and b.total >= MIN_SAMPLE * 2:
                    rules.append({
                        "rule": f"ABOVE AVERAGE: {category} = {key}",
                        "evidence": f"{b.wins}W/{b.losses}L ({wr:.0f}% WR), avg R={avg_r:.2f}, {b.total} trades",
                        "action": f"Slight confidence boost +5 for {category}={key}",
                        "priority": "MEDIUM"
                    })

        _check_bucket(self.by_entry_type, "setup_type")
        _check_bucket(self.by_session,    "session")
        _check_bucket(self.by_amd,        "amd_phase")
        _check_bucket(self.by_pattern,    "pattern")
        _check_bucket(self.by_quarter,    "quarter")

        # Cross-analysis: best combo
        combos = self._find_best_combinations()
        rules.extend(combos)

        # Sort: HIGH first, then by win rate implied from rule text
        rules.sort(key=lambda r: 0 if r["priority"] == "HIGH" else 1)
        return rules

    def _find_best_combinations(self) -> list[dict]:
        """Find which entry_type + session combinations perform best."""
        combo_stats: dict[str, list] = {}
        for t in self.trades.values():
            if t.status not in ("WIN", "LOSS", "BREAKEVEN"):
                continue
            key = f"{t.entry_type}+{t.session}"
            if key not in combo_stats:
                combo_stats[key] = []
            combo_stats[key].append(t.r_multiple)

        results = []
        for key, r_list in combo_stats.items():
            if len(r_list) < MIN_SAMPLE:
                continue
            wins = sum(1 for r in r_list if r > 0.1)
            wr = wins / len(r_list) * 100
            avg_r = sum(r_list) / len(r_list)
            if wr >= 60 and avg_r >= 1.2:
                results.append({
                    "rule": f"WINNING COMBO: {key}",
                    "evidence": f"{wins}W/{len(r_list)-wins}L ({wr:.0f}% WR), avg R={avg_r:.2f}",
                    "action": f"Prioritize {key} setups",
                    "priority": "HIGH"
                })
        return results

    # ── Reporting ─────────────────────────────────────────────────────────────

    def get_performance_report(self) -> str:
        """Returns a formatted performance report string."""
        closed = [t for t in self.trades.values() if t.status in ("WIN","LOSS","BREAKEVEN")]
        if not closed:
            return "No completed trades yet."

        wins   = sum(1 for t in closed if t.status == "WIN")
        losses = sum(1 for t in closed if t.status == "LOSS")
        total  = len(closed)
        wr     = wins / total * 100 if total > 0 else 0
        total_r = sum(t.r_multiple for t in closed)
        avg_r  = total_r / total if total > 0 else 0
        total_pnl = sum(t.profit_usd for t in closed)

        lines = [
            "═" * 52,
            "  OMNI TRADE MEMORY — PERFORMANCE REPORT",
            "═" * 52,
            f"  Total Trades:  {total}  (W:{wins} L:{losses} BE:{total-wins-losses})",
            f"  Win Rate:      {wr:.1f}%",
            f"  Avg R:         {avg_r:+.2f}R",
            f"  Total R:       {total_r:+.2f}R",
            f"  Total P&L:     ${total_pnl:+,.2f}",
            "",
            "  BY SETUP TYPE:",
        ]
        for k, b in sorted(self.by_entry_type.items(), key=lambda x: -x[1].win_rate):
            if b.total >= 3:
                lines.append(f"    {k:<25} {b.wins}W/{b.losses}L  WR:{b.win_rate:.0f}%  avgR:{b.avg_r:.2f}")

        lines += ["", "  BY SESSION:"]
        for k, b in sorted(self.by_session.items(), key=lambda x: -x[1].win_rate):
            if b.total >= 3:
                lines.append(f"    {k:<15} {b.wins}W/{b.losses}L  WR:{b.win_rate:.0f}%  avgR:{b.avg_r:.2f}")

        lines += ["", "  BY QUARTER (ICT Quarter Theory):"]
        for k, b in sorted(self.by_quarter.items()):
            if b.total >= 3:
                lines.append(f"    {k:<6} {b.wins}W/{b.losses}L  WR:{b.win_rate:.0f}%  avgR:{b.avg_r:.2f}")

        lines += ["", "  BY PATTERN:"]
        for k, b in sorted(self.by_pattern.items(), key=lambda x: -x[1].win_rate):
            if b.total >= 3:
                lines.append(f"    {k:<25} {b.wins}W/{b.losses}L  WR:{b.win_rate:.0f}%  avgR:{b.avg_r:.2f}")

        lines += ["", "  LEARNED RULES:"]
        rules = self.generate_learned_rules()
        if rules:
            for r in rules[:8]:
                lines.append(f"    [{r['priority']}] {r['rule']}")
                lines.append(f"          {r['evidence']}")
        else:
            lines.append("    Not enough data yet — need at least 8 trades per category")

        lines.append("═" * 52)
        return "\n".join(lines)

    def _summary_dict(self) -> dict:
        closed = [t for t in self.trades.values() if t.status in ("WIN","LOSS","BREAKEVEN")]
        total = len(closed)
        wins  = sum(1 for t in closed if t.status == "WIN")
        return {
            "total_trades":  total,
            "wins":          wins,
            "losses":        sum(1 for t in closed if t.status == "LOSS"),
            "win_rate_pct":  round(wins / total * 100, 1) if total > 0 else 0,
            "total_r":       round(sum(t.r_multiple for t in closed), 2),
            "total_pnl_usd": round(sum(t.profit_usd for t in closed), 2),
        }

    # ── Journal Writer ────────────────────────────────────────────────────────

    def _write_journal(self, t: TradeRecord, event: str):
        """Write a detailed human-readable journal entry to file."""
        try:
            lines = []
            sep = "━" * 64
            ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            if event == "ENTRY":
                lines += [
                    sep,
                    f"  TRADE ENTRY  [{ts}]",
                    sep,
                    f"  ID:          {t.trade_id}",
                    f"  Symbol:      {t.symbol}  {t.direction}",
                    f"  Setup Type:  {t.entry_type}",
                    f"  Confidence:  {t.confidence}/100 (adj: {t.adj_confidence}/100)",
                    f"  RR Ratio:    {t.rr_ratio:.2f}:1",
                    "  ─────────────────────────────────────────────────────────────",
                    f"  Entry:       {t.entry_price:.5g}",
                    f"  Stop Loss:   {t.sl_price:.5g}  (invalidation @ {t.invalidation:.5g})",
                    f"  TP1 (50%):   {t.tp1_price:.5g}",
                    f"  TP2 (30%):   {t.tp2_price:.5g}",
                    f"  TP3 (20%):   {t.tp3_price:.5g}",
                    "  ─────────────────────────────────────────────────────────────",
                    f"  Session:     {t.session}  |  AMD Phase: {t.amd_phase}",
                    f"  D1 Bias:     {t.d1_bias}  |  H4 Structure: {t.h4_structure}",
                    f"  Quarter:     {t.quarter} ({t.quarter_pct:.1f}% of daily range)",
                    f"  Patterns:    {', '.join(t.patterns) if t.patterns else 'None detected'}",
                    f"  Push/Exh:    {t.push_exhaustion or 'N/A'}",
                    "  ─────────────────────────────────────────────────────────────",
                    f"  Risk:        {t.risk_pct:.2f}%  |  ${t.risk_usd:.2f}  |  {t.lot_size} lots",
                    "  CONFLUENCE REASONS:",
                ]
                for i, reason in enumerate(t.reasons, 1):
                    lines.append(f"    {i:2d}. {reason}")
                if t.is_scale_in:
                    lines.append(f"  [SCALE-IN] Parent trade: {t.parent_trade_id}")

            elif event == "CLOSE":
                outcome_str = f"{'WIN' if t.status=='WIN' else ('LOSS' if t.status=='LOSS' else 'BREAKEVEN')}"
                lines += [
                    sep,
                    f"  TRADE CLOSE  [{ts}]  ── {outcome_str}",
                    sep,
                    f"  ID:          {t.trade_id}",
                    f"  Symbol:      {t.symbol}  {t.direction}",
                    f"  Entry Type:  {t.entry_type}",
                    f"  Open:        {t.entry_price:.5g}  →  Close: {t.close_price:.5g}",
                    f"  Result:      {t.r_multiple:+.2f}R  |  ${t.profit_usd:+.2f}",
                    f"  Exit level:  {t.tp_level_hit or 'Unknown'}",
                    f"  Reason:      {t.close_reason}",
                    f"  Duration:    {t.duration_mins:.0f} minutes",
                    f"  Invalidated: {'YES' if t.was_invalidated else 'No'}",
                ]

            lines.append("")

            with open(LOG_PATH, "a") as f:
                f.write("\n".join(lines) + "\n")

        except Exception as e:
            log.error(f"Journal write error: {e}")

    # ── Utility ───────────────────────────────────────────────────────────────

    def find_open_trade(self, symbol: str, direction: str) -> Optional[TradeRecord]:
        """Find the most recent open trade for a symbol/direction."""
        candidates = [
            t for t in self.trades.values()
            if t.symbol == symbol and t.direction == direction and t.status == "OPEN"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda t: t.open_time)

    def get_recent_trades(self, n: int = 20) -> list[TradeRecord]:
        closed = [t for t in self.trades.values() if t.status != "OPEN"]
        return sorted(closed, key=lambda t: t.open_time, reverse=True)[:n]

    def print_report(self):
        print(self.get_performance_report())


# ── Singleton accessor ─────────────────────────────────────────────────────────

_instance: Optional[TradeMemory] = None

def get_memory() -> TradeMemory:
    global _instance
    if _instance is None:
        _instance = TradeMemory()
    return _instance


if __name__ == "__main__":
    mem = TradeMemory()
    mem.print_report()
