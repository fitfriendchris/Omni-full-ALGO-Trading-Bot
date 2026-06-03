# OMNI Hidden Hardcode Audit — agents / core modules
# Generated: 2026-05-28
# Scope: All .py files under ~/Omni-full-ALGO-Trading-Bot/python/ (agents + core)
# Criteria: Numeric literals, .get(..., HARDCODED_DEFAULT), equity thresholds,
#           risk %, lot caps, session hours, time constants that override rules.json
# ──────────────────────────────────────────────────────────────────────────────

SEVERITY LEGEND
  P0 — Blocks trades / major financial impact / safety gate bypass
  P1 — Wrong sizing / wrong risk / wrong thresholds (trades but incorrectly)
  P2 — Minor drift / fallback masking / cosmetic inconsistency

┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ #  │ FILE                        │ LINE(S) │ HARD-CODED VALUE              │ RULES.JSON KEY (if any)     │ SEV │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 1  │ execution_agent.py          │ 132     │ CYCLE_INTERVAL_S = 15.0       │ — (not in rules.json)       │ P2  │
│ 2  │ execution_agent.py          │ 148     │ stale_task age > 300 sec      │ —                           │ P2  │
│ 3  │ execution_agent.py          │ 171     │ cancel pending limit > 60 min │ —                           │ P2  │
│ 4  │ execution_agent.py          │ 230     │ margin > equity * 0.50 gate │ symbol_overrides.equity_…   │ P1  │
│ 5  │ execution_agent.py          │ 288-290 │ asia_min_confidence = 0.75   │ symbol_overrides.asia_min…  │ P1  │
│ 6  │ execution_agent.py          │ 313     │ max_entry_drift_pct default 0.50│ smart_trail.max_entry_…   │ P1  │
│ 7  │ execution_agent.py          │ 339-344 │ ATR multipliers tiered by equity│ — (no rules key)           │ P1  │
│     │                             │         │   <200 → 0.15, <500 → 0.25,   │                             │     │
│     │                             │         │   else → 0.50                 │                             │     │
│ 8  │ execution_agent.py          │ 352-357 │ max_risk_pct tiered by equity │ — (no rules key)            │ P0  │
│     │                             │         │   <200 → 30%, <500 → 8%,      │                             │     │
│     │                             │         │   else → 2%                   │                             │     │
│ 9  │ execution_agent.py          │ 358     │ min risk_dollars floor = 0.30 │ —                           │ P2  │
│ 10 │ execution_agent.py          │ 421     │ drawdown circuit breaker 20%  │ max_drawdown_pct (ignored)  │ P0  │
│ 11 │ execution_agent.py          │ 435     │ Kelly cap 5% risk             │ —                           │ P1  │
│ 12 │ execution_agent.py          │ 442-447 │ Kelly risk % tiered by equity │ — (no rules key)            │ P1  │
│     │                             │         │   <200 → 12%, <500 → 8%,      │                             │     │
│     │                             │         │   else → kelly                │                             │     │
│ 13 │ execution_agent.py          │ 461-466 │ Hard lot cap by equity tier   │ — (no rules key)            │ P1  │
│     │                             │         │   <1000 → 0.50, <5000 → 1.0,  │                             │     │
│     │                             │         │   else → 2.0                  │                             │     │
│ 14 │ execution_agent.py          │ 470-471 │ sub-$250 forced lot = 0.01    │ —                           │ P1  │
│ 15 │ execution_agent.py          │ 555-558 │ Limit order default expiry 60m│ —                           │ P2  │
│ 16 │ execution_agent.py          │ 313     │ .get("max_entry_drift_pct",0.50)│ smart_trail.max_entry_…   │ P1  │
│ 17 │ execution_agent.py          │ 379     │ .get("spread_atr_frac", 0.30) │ smart_trail.spread_atr_frac │ P2  │
│ 18 │ execution_agent.py          │ 84      │ .get("contract_size", 100000) │ chart.contract_size         │ P2  │
│ 19 │ execution_agent.py          │ 96,107  │ .get("tick_size", 0.01)       │ chart.tick_size             │ P2  │
│ 20 │ execution_agent.py          │ 114-116 │ proximity % math constant 100 │ legitimate math             │ —   │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 21 │ risk_agent.py               │ 46      │ CYCLE_INTERVAL_S = 30.0       │ —                           │ P2  │
│ 22 │ risk_agent.py               │ 59      │ day_start_balance fallback 100│ —                           │ P1  │
│ 23 │ risk_agent.py               │ 71      │ equity < 1 → skip check       │ —                           │ P2  │
│ 24 │ risk_agent.py               │ 77      │ day_start_balance < 1 → reset │ —                           │ P2  │
│ 25 │ risk_agent.py               │ 82      │ .get("max_daily_loss_pct",5.0)│ risk_rules.max_daily_loss_pct│ P1  │
│ 26 │ risk_agent.py               │ 93      │ daily_pnl < -ref * 0.50 sanity│ —                           │ P2  │
│ 27 │ risk_agent.py               │ 113     │ alert threshold 80% of max_dd │ —                           │ P2  │
│ 28 │ risk_agent.py               │ 168     │ .get("min_confidence",0.50)   │ risk_rules.min_confidence   │ P1  │
│ 29 │ risk_agent.py               │ 179     │ .get("max_open_positions",3)  │ risk_rules.max_open_positions│ P1  │
│ 30 │ risk_agent.py               │ 185     │ .get("equity", 10000) fallback│ account.equity              │ P1  │
│ 31 │ risk_agent.py               │ 197     │ .get("min_rr_ratio", 2.0)     │ risk_rules.min_rr_ratio     │ P1  │
│ 32 │ risk_agent.py               │ 212-214 │ equity < 10 → reject,         │ — (no rules key)            │ P0  │
│     │                             │         │   equity < 100 → cap 0.5%     │                             │     │
│ 33 │ risk_agent.py               │ 217     │ .get("base_risk_pct", 1.0)    │ risk_rules.base_risk_pct    │ P1  │
│ 34 │ risk_agent.py               │ 220     │ risk_usd = min(risk_usd, eq*0.005)│ —                       │ P1  │
│ 35 │ risk_agent.py               │ 223     │ sl_dist < 1e-8 → return 0.01  │ —                           │ P1  │
│ 36 │ risk_agent.py               │ 225-232 │ Symbol contract unit constants│ — (no rules key)            │ P1  │
│     │                             │         │   XAUUSD=100, XAGUSD=5000,    │                             │     │
│     │                             │         │   JPY=100000/entry, else=100000│                            │     │
│ 37 │ risk_agent.py               │ 235     │ round(max(min(lot,0.10),0.01),2)│ —                       │ P1  │
│     │                             │         │ hard lot floor 0.01, ceiling 0.10│                            │     │
│ 38 │ risk_agent.py               │ 265-275 │ Zero-equity → cached equity   │ —                           │ P2  │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 39 │ signal_agent.py             │ 48      │ CYCLE_INTERVAL_S = 20.0       │ —                           │ P2  │
│ 40 │ signal_agent.py             │ 86-106  │ ADX period = 14 (TA constant) │ legitimate indicator        │ —   │
│ 41 │ signal_agent.py             │ 110     │ .get("min_confidence", 0.50)  │ dual_tf.min_confidence      │ P1  │
│ 42 │ signal_agent.py             │ 128     │ .get("equity", 0)             │ account.equity              │ P2  │
│ 43 │ signal_agent.py             │ 142     │ .get("confidence", 0)         │ signal.confidence           │ P2  │
│ 44 │ signal_agent.py             │ 148     │ adx < 20 and conf < 0.85 gate │ — (no rules key)            │ P1  │
│ 45 │ signal_agent.py             │ 160     │ .get("min_equity_usd", 75)    │ symbol_overrides.equity_gate│ P2  │
│ 46 │ signal_agent.py             │ 173-174 │ equity < 500 scaling formula  │ — (no rules key)            │ P1  │
│     │                             │         │   effective_conf *= (eq/100)^0.5│                            │     │
│ 47 │ signal_agent.py             │ 178-180 │ sym_min_conf logic with /100  │ —                           │ P2  │
│ 48 │ signal_agent.py             │ 223     │ no_tradeable gap > 1800 sec   │ —                           │ P2  │
│ 49 │ signal_agent.py             │ 230-231 │ routed_ids prune > 400 → 200  │ —                           │ P2  │
│ 50 │ signal_agent.py             │ 250-251 │ conf_override clamp -0.30/+0.20│ —                          │ P2  │
│ 51 │ signal_agent.py             │ 277     │ _is_active_session 7-21 UTC   │ —                           │ P2  │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 52 │ auto_trader.py              │ 749-799 │ calculate_lot_size() ceilings │ — (no rules key)            │ P0  │
│     │                             │         │   <500 → 0.05, <2k → 0.20,    │                             │     │
│     │                             │         │   <5k → 0.50, <10k → 1.00,    │                             │     │
│     │                             │         │   10k+ → 2% equity in lots      │                             │     │
│ 53 │ auto_trader.py              │ 785-793 │ Same hard lot ceiling tiers   │ scaling_engine (different)  │ P0  │
│ 54 │ auto_trader.py              │ 803-838 │ adjust_risk() streak tiers    │ leverage_compounding (partial)│ P1│
│ 55 │ auto_trader.py              │ 815-833 │ Win streak 3/5, Loss streak 2/3│ —                        │ P1  │
│ 56 │ auto_trader.py              │ 846-866 │ recovery_protocol defaults      │ recovery_protocol (partial) │ P2  │
│ 57 │ auto_trader.py              │ 869-886 │ _compounded_risk() multipliers│ leverage_compounding (partial)│ P1│
│ 58 │ auto_trader.py              │ 880-884 │ win streak 7→1.75, 5→1.50,    │ leverage_compounding (partial)│ P1│
│     │                             │         │   3→1.20, loss 3→0.60, 2→0.80   │                             │     │
│ 59 │ auto_trader.py              │ 905     │ send_command poll 100×0.1s      │ —                           │ P2  │
│ 60 │ auto_trader.py              │ 967     │ pyramid_max_adds default 3      │ scalp_engine.pyramid_max_adds│ P2 │
│ 61 │ auto_trader.py              │ 1118-1132│ drawdown halt uses MAX_DD_FROM_PEAK (config) │ max_drawdown_pct │ P0  │
│ 62 │ auto_trader.py              │ 1145-1148│ Trading day reset at 22:00 UTC │ —                          │ P2  │
│ 63 │ auto_trader.py              │ 1184    │ Partial TPs: 50% / 30% / 20%  │ — (hard strategy)           │ P0  │
│ 64 │ auto_trader.py              │ 1266    │ Re-entry valid_until + 3600   │ —                           │ P2  │
│ 65 │ auto_trader.py              │ 1289-1300│ Session loss streak ≥3 penalty│ —                           │ P1  │
│ 66 │ auto_trader.py              │ 1383    │ _load_regime() call           │ regime_agent (indirect)     │ P2  │
│ 67 │ auto_trader.py              │ 1413    │ modify threshold 1×pip_size   │ —                           │ P2  │
│ 68 │ auto_trader.py              │ 1816    │ pip_size = point * 10         │ —                           │ P2  │
│ 69 │ auto_trader.py              │ 1822-1827│ Stale limit check pip_size*10 │ —                           │ P2  │
│ 70 │ auto_trader.py              │ 1862-1863│ scale-in max_adds=3, min_profit_r=0.75│ scaling (partial)   │ P1  │
│ 71 │ auto_trader.py              │ 1943    │ scale_vol = original * 0.50   │ scaling.add_size_frac       │ P1  │
│ 72 │ auto_trader.py              │ 1954    │ scale TP = 1.5× remaining     │ —                           │ P1  │
│ 73 │ auto_trader.py              │ 1962-1964│ scale TP fallback = 3×risk    │ —                           │ P1  │
│ 74 │ auto_trader.py              │ 2062    │ MAX_OPEN_TRADES from config   │ config.py (legitimate)      │ —   │
│ 75 │ auto_trader.py              │ 2073    │ pip_size = point * 10         │ —                           │ P2  │
│ 76 │ auto_trader.py              │ 2083    │ re-entry tolerance = 5 pips   │ —                           │ P2  │
│ 77 │ auto_trader.py              │ 2191    │ effective_min_conf + 10 per tier│ —                         │ P1  │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 78 │ scaling_engine.py           │ 96-115  │ DEFAULT_RULES dict at module  │ scaling (shadows rules.json)│ P0  │
│     │                             │         │ level — shadows rules.json if  │                             │     │
│     │                             │         │ missing or partial            │                             │     │
│ 79 │ scaling_engine.py           │ 99      │ max_adds = 2                  │ scaling.max_adds            │ P1  │
│ 80 │ scaling_engine.py           │ 100     │ min_add_profit_r = 1.0        │ scaling.min_add_profit_r    │ P1  │
│ 81 │ scaling_engine.py           │ 101     │ add_size_frac = 0.5           │ scaling.add_size_frac       │ P1  │
│ 82 │ scaling_engine.py           │ 103     │ reduce_at_r = 2.0             │ scaling.reduce_at_r         │ P1  │
│ 83 │ scaling_engine.py           │ 104     │ reduce_frac = 0.33            │ scaling.reduce_frac         │ P1  │
│ 84 │ scaling_engine.py           │ 106     │ close_at_adverse_r = -0.2     │ scaling.close_at_adverse_r  │ P1  │
│ 85 │ scaling_engine.py           │ 109-113 │ add_size_ladder hardcoded     │ scaling.add_size_ladder     │ P1  │
│ 86 │ scaling_engine.py           │ 153     │ structure[-3:] for LTF check │ —                            │ P2  │
│ 87 │ scaling_engine.py           │ 157     │ proximity_frac = 0.25         │ —                           │ P2  │
│ 88 │ scaling_engine.py           │ 186-193 │ evaluate() flat fallbacks     │ scaling (shadows)           │ P1  │
│ 89 │ scaling_engine.py           │ 220     │ _atr(ltf.bars, period=7)      │ —                           │ P2  │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 90 │ swarm.py                    │ 136     │ back-off min=2, max=120, ×2    │ —                           │ P2  │
│ 91 │ swarm.py                    │ 174     │ state writer every 30 sec     │ —                           │ P2  │
│ 92 │ swarm.py                    │ 189     │ stop-file poll every 10 sec   │ —                           │ P2  │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 93 │ smart_trail_adapter.py      │ 28      │ .get("enabled", True) default │ smart_trail.enabled         │ P2  │
│ 94 │ smart_trail_adapter.py      │ 35-36   │ profit_lock_ladder defaults   │ smart_trail.profit_lock_…   │ P2  │
│ 95 │ smart_trail_adapter.py      │ 38-55   │ _build_config() flat defaults │ smart_trail.*               │ P2  │
│ 96 │ smart_trail_adapter.py      │ 74      │ _MAX_TRAIL_BARS = 200         │ —                           │ P2  │
│ 97 │ smart_trail_adapter.py      │ 97-106  │ _pip_size_for() hardcodes     │ —                           │ P2  │
│ 98 │ smart_trail_adapter.py      │ 111-122 │ _tick_size_for() hardcodes    │ chart.tick_size             │ P2  │
│ 99 │ smart_trail_adapter.py      │ 177     │ tick_size * 20 fallback spread│ —                           │ P2  │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 100│ config.py                   │ 122-141 │ All _getfloat/_getint defaults│ config.json / env vars      │ P2  │
│    │                             │         │ These are legitimate fallbacks│ (priority: env > json > def)│   │
│ 101│ config.py                   │ 253     │ MAX_TRAIL_PIPS < 30 warning   │ —                           │ P2  │
│ 102│ config.py                   │ 255     │ BASE_RISK_PCT > 3.0 warning   │ —                           │ P2  │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 103│ dual_tf_selector.py         │ 140-147 │ MIN_CONFLUENCE, MIN_RR, TP_RR │ — (core engine constants)   │ P1  │
│    │                             │         │ KILL_ZONES_UTC hours          │                             │     │
│ 104│ dual_tf_selector.py         │ 155     │ AMD_ACTIONABLE_PHASES list    │ —                           │ P2  │
│ 105│ dual_tf_selector.py         │ 158     │ _CONFLUENCE_WEIGHT = 0.10     │ —                           │ P2  │
│ 106│ dual_tf_selector.py         │ 31-41   │ Confidence formula constants  │ — (core algorithm)          │ P1  │
│    │                             │         │ base=0.45, +0.10 per condition│                             │     │
│    │                             │         │ penalties for outside KZ etc  │                             │     │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤

SUMMARY BY SEVERITY
  P0 (Critical — blocks trades / major safety impact): 7 findings
  P1 (Wrong sizing / risk / thresholds):              43 findings
  P2 (Minor drift / fallback masking):                53 findings
  —  (Legitimate constants / math / already config-driven): 3 findings

TOP CRITICAL ISSUES (P0)
────────────────────────
1. execution_agent.py L421 — 20% drawdown circuit breaker is HARDCODED.
   It reads peak_equity from trader_state.json but the 20% threshold is NOT
   taken from rules.json max_drawdown_pct. If rules.json says 10% or 30%,
   this code ignores it.

2. auto_trader.py L749-799 — calculate_lot_size() has its own hard equity
   tier ceilings (<500→0.05, <2k→0.20, <5k→0.50, <10k→1.00) that OVERRIDE
   any broker max_lot from sym_info AND any scaling config in rules.json.
   The function also ignores rules.json scaling.add_size_ladder.

3. scaling_engine.py L96-115 — DEFAULT_RULES dict at module level SHADOWS
   rules.json. If rules.json is missing the "scaling" key, or if the key
   is partially present, the engine silently falls back to these defaults.
   This is a classic .get("key", HARDCODED_DEFAULT) masking pattern.

4. risk_agent.py L212-220 — equity < 10 → reject trade (no rules.json key).
   equity < 100 → cap risk at 0.5% (no rules.json key).
   These micro-account gates are hardcoded and can't be tuned by config.

5. risk_agent.py L235 — lot floor 0.01 / ceiling 0.10 is hardcoded.
   For a $10,000 account this forces max 0.10 lot regardless of risk rules.

6. auto_trader.py L1118-1132 — drawdown halt uses MAX_DD_FROM_PEAK from
   config.py (10% default). If rules.json sets a different value, the
   auto_trader ignores it and uses config.py's constant.

7. auto_trader.py L1184 — Partial profit taking 50%/30%/20% is a hard
   strategy split with no rules.json override.

FILES CREATED / MODIFIED
────────────────────────
  Created: /Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/HIDDEN_HARDCODE_AUDIT.md
  (This file — the complete audit table and recommendations.)

RECOMMENDED FIXES (in priority order)
─────────────────────────────────────
1. [P0] execution_agent.py L421 — Replace hardcoded 0.20 with:
      rules.get("risk_rules", {}).get("max_drawdown_pct", 10.0) / 100.0

2. [P0] auto_trader.py L749-799 — Remove duplicate lot ceiling tiers.
   Let calculate_lot_size() read a "lot_ceiling_tiers" list from rules.json
   or use the broker sym_info.max_lot as the only hard cap.

3. [P0] scaling_engine.py L96-115 — Remove DEFAULT_RULES module-level dict.
   Instead, require rules.json to be present and loudly warn (not silently
   default) if the "scaling" section is missing.  Or merge with explicit
   logging: "scaling rules missing — using emergency defaults".

4. [P0] risk_agent.py L212-220 — Move micro-account thresholds to rules.json:
      risk_rules.micro_account_equity_threshold = 100
      risk_rules.micro_account_max_risk_pct = 0.5
      risk_rules.min_trade_equity = 10

5. [P0] risk_agent.py L235 — Read lot floor/ceiling from rules.json:
      risk_rules.min_lot = 0.01
      risk_rules.max_lot = 0.10   (or sym_info override)

6. [P1] execution_agent.py L339-344 + L352-357 + L442-447 + L461-466
   — All equity-tiered risk/ATR/lot constants should be in a single
   "micro_account_tiers" block in rules.json so they are visible and tunable.

7. [P1] auto_trader.py L785-793 — Align lot ceiling with execution_agent.py
   (they currently disagree: auto_trader says <500→0.05, execution_agent says
   <1000→0.50 for the hard cap).  Single source of truth in rules.json.

8. [P2] All .get("...", DEFAULT) patterns — Add explicit "MISSING_CONFIG"
   logs when a default fallback is used, so silent masking is visible in
   the logs.  Example:
      val = rules.get("key")
      if val is None:
          log.warning("MISSING_CONFIG: key 'max_entry_drift_pct' not in rules.json; using 0.50")
          val = 0.50
