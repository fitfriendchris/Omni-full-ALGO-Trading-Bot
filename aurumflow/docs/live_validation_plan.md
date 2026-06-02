# AurumFlow: Live Deployment Validation & Pre-Flight Checklist

This document details the audit of risk parameters, critical code paths, and the required steps before enabling live trading.

## 1. Conservative Risk Profile (Initial Phase)

The initial live deployment will use the `config/live_conservative.yaml` profile, which prioritizes capital preservation over growth.

### Key Parameter Overrides:
| Parameter | Default | Conservative | Purpose |
|-----------|---------|--------------|---------|
| `risk_per_trade` | 0.01 (1%) | **0.0025 (0.25%)** | 4x lower exposure per trade. |
| `max_positions` | 5 | **2** | Limits total account exposure. |
| `pyramiding_max` | 4 | **2** | Limits compounding risk in single trends. |
| `max_drawdown_limit`| 0.15 (15%) | **0.10 (10%)** | Tighter exit on poor performance. |
| `max_daily_loss` | 0.05 (5%) | **0.03 (3%)** | Tighter daily loss guard. |
| `min_margin_level` | 100.0 | **150.0** | Ensures account is never over-leveraged. |
| `trading.enabled` | false | **false** | Must be explicitly toggled in file. |

## 2. Code Path Audit

### Risk Guards (`can_open_trade`)
- **Status**: **PASS**. 
- **Verification**: The method correctly gates entries based on margin level, position counts, daily loss, and drawdown. 
- **Finding**: Added margin level check to ensure we don't enter trades if the account is already heavily utilized.

### Position Sizing (`compute_position_size`)
- **Status**: **PASS**.
- **Verification**: Correctly accounts for cent accounts (converting cents to dollars for units calc). Rounds to broker step (0.01 lots).
- **Finding**: `skip_below_min_lot: true` is crucial for small accounts ($100-$500) to avoid over-risking if the 0.25% risk is less than 0.01 lots.

### Execution Edge Cases (`_execute_signal`)
- **Status**: **PASS WITH RECOMMENDATION**.
- **Observation**: The bot relies on the strategy to signal `close` to record results in `RiskManager`.
- **Recommendation**: The bot needs a **Reconciliation Loop** to detect positions closed by SL/TP in MT5 to correctly update `daily_pnl` and `consecutive_losses`.

### Shutdown Handling
- **Status**: **PASS WITH MODIFICATION**.
- **Verification**: Current `shutdown()` only disconnects. 
- **Recommendation**: Add a `close_all_on_exit` config flag (default `false`) for panic shutdowns.

## 3. Pre-Flight Checklist

Before setting `trading.enabled: true`:

1.  [ ] **MT5 Credentials**: Verify `AURUM_MT5_LOGIN` and `AURUM_MT5_PASSWORD` are exported in the shell.
2.  [ ] **Connectivity**: Run `python src/bot/main.py --config config/live_conservative.yaml --dry-run`. Verify log shows "Connected to ICMarkets-Demo".
3.  [ ] **Account Info**: Check logs to verify Balance, Equity, and Leverage match your MT5 terminal.
4.  [ ] **Symbol Info**: Verify "XAUUSD" is visible and has a valid spread in the logs.
5.  [ ] **Warm-up**: Run for at least 1 hour in `trading.enabled: false` mode to ensure indicators (EMA, RSI) have enough bars to stabilize.
6.  [ ] **Backup**: Have MT5 mobile app logged in and ready to "Close All" if the bot encounters unexpected behavior.
7.  [ ] **Logging**: Verify `logs/aurumflow_live.log` is being written to.

## 4. Kill Switch Recommendation

**Highly Recommended.**

We should implement an **External Monitor** (separate script) that:
1.  Connects to MT5 independently.
2.  Monitors Equity every 10 seconds.
3.  If Equity < (Peak * 0.88) [12% drawdown vs 10% bot limit]:
    - Sends `kill -9` to the bot process.
    - Calls `positions_get()` and `order_send(TRADE_ACTION_DEAL, type_filling=IOC)` to close everything.
    - Sends an alert (Discord/Telegram).

This protects against bot "hangs" where the Python process is stuck but positions are still open and moving against us.

---
*Prepared by Quant Researcher*
*AurumFlow Team*
