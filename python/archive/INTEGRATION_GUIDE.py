"""
INTEGRATION GUIDE: Advanced Multi-Timeframe Trading System
=========================================================

This guide shows how to integrate the advanced system into your existing
auto_trader.py and ict_engine.py setup.

Three modules work together:
1. advanced_structure_analyzer.py   → Multi-timeframe order blocks, AMD, liquidity
2. advanced_risk_manager.py          → Position sizing, fees, swaps, leverage
3. integrated_trading_engine.py      → Complete signal generation and trading

USAGE FLOW:
===========

Step 1: Import the modules
Step 2: Prepare market data (multi-timeframe OHLCV)
Step 3: Analyze structures across all timeframes
Step 4: Generate trading signals with precision
Step 5: Execute trades with calculated position sizes
Step 6: Manage positions dynamically
"""

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List

# New modules
from advanced_structure_analyzer import UnifiedMultiTimeframeAnalyzer, Timeframe
from advanced_risk_manager import (
    FeeCalculator, AdvancedPositionSizer, AssetClass, TradeDuration, 
    AccountMetrics
)
from integrated_trading_engine import IntegratedSignalGenerator, SignalManager

# Your existing modules
# from ict_engine import get_ict_scanner, get_session_info
# from mt5_connector import get_symbol_prices, get_account_info
# from auto_trader import place_trade, manage_position


# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE: Complete Trading Workflow
# ─────────────────────────────────────────────────────────────────────────────

class AdvancedAutoTrader:
    """
    Production-ready auto trader combining ICT + multi-timeframe analysis
    """
    
    def __init__(self,
                 broker: str = "METATRADER",
                 symbols: List[str] = None,
                 paper_mode: bool = True):
        
        self.broker = broker
        self.symbols = symbols or ["EURUSD", "GBPUSD", "XAUUSD", "AUDUSD"]
        self.paper_mode = paper_mode
        
        # Initialize components
        self.signal_generator = IntegratedSignalGenerator(
            broker=broker,
            asset_class=AssetClass.FOREX
        )
        
        self.signal_manager = SignalManager()
        self.analyzer = UnifiedMultiTimeframeAnalyzer()
        
        # Trading parameters
        self.min_signal_confidence = 2  # Medium confidence minimum
        self.max_concurrent_trades = 3
        self.daily_loss_limit_pct = 5.0
        
        self.active_trades = {}  # Track live positions
    
    def scan_and_generate_signals(self, 
                                 market_data: Dict[str, Dict[Timeframe, pd.DataFrame]],
                                 account_info: Dict) -> Dict[str, List]:
        """
        Main scanning function - run this every minute or on tick
        
        Args:
            market_data: {
                'EURUSD': {Timeframe.H4: df, Timeframe.H1: df, ...},
                'GBPUSD': {...},
                ...
            }
            account_info: {
                'balance': 10000,
                'equity': 10500,
                'used_margin': 2000,
                ...
            }
        
        Returns:
            {
                'EURUSD': [signal1, signal2, ...],
                'GBPUSD': [signal1, ...],
                ...
            }
        """
        
        all_signals = {}
        
        # Convert account info to AccountMetrics
        account = self._create_account_metrics(account_info)
        
        # Scan each symbol
        for symbol, tf_data in market_data.items():
            if not tf_data:
                continue
            
            # Get ATR data for volatility-based stops (calculate from 1H)
            atr_data = self._calculate_atr(tf_data)
            
            # Generate signals
            signals = self.signal_generator.generate_signals(
                symbol=symbol,
                data_dict=tf_data,
                account=account,
                atr_data=atr_data
            )
            
            if signals:
                all_signals[symbol] = signals
                
                # Log the best signal
                best = signals[0]
                print(f"\n{'='*60}")
                print(f"SIGNAL: {symbol} {best.direction}")
                print(f"{'='*60}")
                print(f"Entry:        {best.entry_price:.5f}")
                print(f"Stop Loss:    {best.stop_loss:.5f}")
                print(f"Take Profit:  {best.take_profit_2:.5f}")
                print(f"Confidence:   {best.confidence_level.name}")
                print(f"Confluence:   {best.confluence_score:.1%}")
                print(f"RR Ratio:     {best.risk_reward_ratio:.2f}")
                print(f"Position:     {best.position.quantity:.2f} units")
                print(f"Leverage:     {best.position.leverage:.1f}x")
                print(f"Risk:         ${best.risk_amount:.2f}")
                print(f"Reward:       ${best.reward_amount:.2f}")
                print(f"Expected Val: ${best.expected_value:.2f}")
                print(f"Suitable for: ", end="")
                trading_styles = []
                if best.scalp_suitable:
                    trading_styles.append("Scalping")
                if best.day_trade_suitable:
                    trading_styles.append("Day Trading")
                if best.swing_trade_suitable:
                    trading_styles.append("Swing Trading")
                print(", ".join(trading_styles) or "None")
                print(f"Reasons: {'; '.join(best.key_reasons[:2])}")
                if best.risk_factors:
                    print(f"⚠️  Risks: {'; '.join(best.risk_factors[:2])}")
        
        return all_signals
    
    def execute_signal(self, symbol: str, signal, live_trade: bool = False) -> bool:
        """
        Execute a trading signal
        
        Args:
            symbol: Trading pair
            signal: TradingSignal object from signal generator
            live_trade: If False, paper trade; if True, live trade (CAREFUL!)
        
        Returns:
            True if trade executed successfully
        """
        
        if not signal.is_actionable:
            print(f"Signal not actionable for {symbol}")
            return False
        
        if len(self.active_trades) >= self.max_concurrent_trades:
            print(f"Max concurrent trades ({self.max_concurrent_trades}) reached")
            return False
        
        # Prepare order
        order = {
            'symbol': symbol,
            'direction': signal.direction,
            'quantity': signal.position.quantity,
            'entry_price': signal.entry_price,
            'stop_loss': signal.stop_loss,
            'take_profit_1': signal.take_profit_1,
            'take_profit_2': signal.take_profit_2,
            'leverage': signal.position.leverage,
            'risk_amount': signal.risk_amount,
            'reward_amount': signal.reward_amount,
            'confidence': signal.confidence_level.name,
            'timestamp': datetime.now(),
        }
        
        if self.paper_mode or not live_trade:
            print(f"\n📝 PAPER TRADE: {order['symbol']} {order['direction']}")
            print(f"   Size: {order['quantity']:.2f} @ {order['entry_price']:.5f}")
            print(f"   SL: {order['stop_loss']:.5f} | TP1: {order['take_profit_1']:.5f} | TP2: {order['take_profit_2']:.5f}")
            self.active_trades[f"{symbol}_{datetime.now().timestamp()}"] = order
            return True
        else:
            # PRODUCTION: Send actual order to broker
            # This would integrate with your MT5 connector
            print(f"\n🚀 LIVE TRADE: {order['symbol']} {order['direction']}")
            # result = place_trade(order)  # From auto_trader.py
            # return result
            return False
    
    def check_active_trades(self, current_prices: Dict[str, float]) -> List[Dict]:
        """
        Check if any active trades should be closed
        
        Args:
            current_prices: {'EURUSD': 1.0850, ...}
        
        Returns:
            List of trades to close with exit reasons
        """
        
        trades_to_close = []
        
        for trade_id, trade in self.active_trades.items():
            symbol = trade['symbol']
            current_price = current_prices.get(symbol)
            
            if not current_price:
                continue
            
            should_exit, reason = False, ""
            
            if trade['direction'] == "BUY":
                if current_price <= trade['stop_loss']:
                    should_exit, reason = True, "Stop Loss"
                elif current_price >= trade['take_profit_2']:
                    should_exit, reason = True, "Take Profit"
            else:  # SELL
                if current_price >= trade['stop_loss']:
                    should_exit, reason = True, "Stop Loss"
                elif current_price <= trade['take_profit_2']:
                    should_exit, reason = True, "Take Profit"
            
            if should_exit:
                trades_to_close.append({
                    'trade_id': trade_id,
                    'symbol': symbol,
                    'exit_price': current_price,
                    'reason': reason,
                    'profit_loss': self._calculate_pnl(trade, current_price)
                })
        
        return trades_to_close
    
    # ─────────────────────────────────────────────────────────────────────
    # HELPER METHODS
    # ─────────────────────────────────────────────────────────────────────
    
    def _create_account_metrics(self, account_info: Dict) -> AccountMetrics:
        """Convert broker account info to AccountMetrics"""
        return AccountMetrics(
            balance=account_info.get('balance', 10000),
            equity=account_info.get('equity', 10000),
            used_margin=account_info.get('used_margin', 0),
            free_margin=account_info.get('free_margin', 10000),
            margin_level=account_info.get('margin_level', 0),
            daily_profit=account_info.get('daily_profit', 0),
            daily_trades=account_info.get('daily_trades', 0),
            win_rate=account_info.get('win_rate', 0.5),
            consecutive_losses=account_info.get('consecutive_losses', 0),
            consecutive_wins=account_info.get('consecutive_wins', 0),
        )
    
    def _calculate_atr(self, tf_data: Dict[Timeframe, pd.DataFrame]) -> Dict[Timeframe, float]:
        """Calculate ATR for each timeframe"""
        atr_data = {}
        
        for tf, df in tf_data.items():
            if len(df) < 14:
                continue
            
            # Simple ATR: average of (high - low)
            tr = df['high'] - df['low']
            atr = tr.rolling(window=14).mean().iloc[-1]
            atr_data[tf] = atr
        
        return atr_data
    
    def _calculate_pnl(self, trade: Dict, exit_price: float) -> float:
        """Calculate profit/loss for a trade"""
        if trade['direction'] == "BUY":
            pnl = (exit_price - trade['entry_price']) * trade['quantity']
        else:
            pnl = (trade['entry_price'] - exit_price) * trade['quantity']
        
        # Subtract fees
        pnl -= trade['risk_amount'] * 0.05  # Rough fee estimate
        return pnl


# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE USAGE
# ─────────────────────────────────────────────────────────────────────────────

def example_full_workflow():
    """
    Complete example showing how to use the advanced system
    """
    
    # Initialize trader
    trader = AdvancedAutoTrader(
        broker="METATRADER",
        symbols=["EURUSD", "GBPUSD", "XAUUSD"],
        paper_mode=True  # Always start with paper mode!
    )
    
    # Example: Create sample market data
    # In production, this comes from MT5 or your data feed
    market_data = _create_sample_market_data()
    
    # Example: Create sample account info
    account_info = {
        'balance': 10000,
        'equity': 10500,
        'used_margin': 2000,
        'free_margin': 8500,
        'margin_level': 24,
        'daily_profit': 250,
        'daily_trades': 2,
        'win_rate': 0.55,
        'consecutive_losses': 0,
        'consecutive_wins': 1,
    }
    
    # Scan for signals
    print("🔍 Scanning for trading opportunities...")
    signals_dict = trader.scan_and_generate_signals(market_data, account_info)
    
    # Execute best signal for each symbol
    for symbol, signals in signals_dict.items():
        if signals:
            best_signal = signals[0]  # Already sorted by quality
            
            # Execute if confidence is high enough
            if best_signal.confidence_level.value >= trader.min_signal_confidence:
                trader.execute_signal(symbol, best_signal, live_trade=False)
    
    # Check for exits
    current_prices = {'EURUSD': 1.0850, 'GBPUSD': 1.2700, 'XAUUSD': 2350}
    trades_to_close = trader.check_active_trades(current_prices)
    
    print(f"\n\n📊 Status: {len(trader.active_trades)} active trades")
    if trades_to_close:
        print(f"⏹️  {len(trades_to_close)} trades ready to close:")
        for trade in trades_to_close:
            print(f"   {trade['symbol']}: {trade['reason']} @ {trade['exit_price']:.5f} | P&L: ${trade['profit_loss']:.2f}")


def _create_sample_market_data() -> Dict[str, Dict[Timeframe, pd.DataFrame]]:
    """Create sample OHLCV data for demonstration"""
    
    # Generate synthetic data for testing
    symbols = ["EURUSD", "GBPUSD", "XAUUSD"]
    timeframes = [Timeframe.DAILY, Timeframe.H4, Timeframe.H1]
    
    market_data = {}
    
    for symbol in symbols:
        market_data[symbol] = {}
        base_price = 1.0850 if symbol == "EURUSD" else 1.2700 if symbol == "GBPUSD" else 2350
        
        for tf in timeframes:
            # Generate 200 candles of data
            n = 200
            dates = pd.date_range(end=datetime.now(), periods=n, freq='h')
            
            # Random walk prices
            returns = np.random.normal(0.0001, 0.002, n)
            closes = base_price * np.exp(np.cumsum(returns))
            
            df = pd.DataFrame({
                'open': closes * (1 + np.random.uniform(-0.0005, 0.0005, n)),
                'high': closes * (1 + abs(np.random.uniform(0, 0.001, n))),
                'low': closes * (1 - abs(np.random.uniform(0, 0.001, n))),
                'close': closes,
                'volume': np.random.randint(1000000, 5000000, n),
            }, index=dates)
            
            market_data[symbol][tf] = df
    
    return market_data


# ─────────────────────────────────────────────────────────────────────────────
# INTEGRATION WITH EXISTING auto_trader.py
# ─────────────────────────────────────────────────────────────────────────────

def integrate_with_existing_trader():
    """
    Example: How to integrate into existing auto_trader.py
    
    In your auto_trader.py main loop, replace the signal detection with:
    """
    
    code_example = '''
    # At the top of auto_trader.py, add imports:
    from integrated_trading_engine import IntegratedSignalGenerator, SignalManager
    from advanced_structure_analyzer import Timeframe
    from advanced_risk_manager import AccountMetrics
    
    # In your main loop, around line 200-300:
    
    # Initialize signal generator (do this once at startup)
    signal_gen = IntegratedSignalGenerator(broker="METATRADER")
    
    # In your scan loop:
    for symbol in TRADE_SYMBOLS:
        # Fetch multi-timeframe data (replace this with your MT5 calls)
        tf_data = {
            Timeframe.DAILY: get_bars(symbol, "D1", 200),
            Timeframe.H4: get_bars(symbol, "H4", 200),
            Timeframe.H1: get_bars(symbol, "H1", 200),
            Timeframe.M15: get_bars(symbol, "M15", 200),
        }
        
        # Get current account metrics
        acct_info = get_account_info()
        account = AccountMetrics(
            balance=acct_info['balance'],
            equity=acct_info['equity'],
            used_margin=acct_info['used_margin'],
            free_margin=acct_info['free_margin'],
            win_rate=state.winning_trades / max(state.total_trades, 1),
            consecutive_losses=state.loss_streak,
            consecutive_wins=state.win_streak,
        )
        
        # Generate signals with new system
        signals = signal_gen.generate_signals(symbol, tf_data, account)
        
        if signals and signals[0].is_actionable:
            best_signal = signals[0]
            
            # Place the trade (integrate with your existing place_trade function)
            # result = place_trade(
            #     symbol=symbol,
            #     direction=best_signal.direction,
            #     quantity=best_signal.position.quantity,
            #     entry=best_signal.entry_price,
            #     sl=best_signal.stop_loss,
            #     tp1=best_signal.take_profit_1,
            #     tp2=best_signal.take_profit_2,
            #     confidence=best_signal.confidence_level.name,
            # )
    '''
    
    print(code_example)


if __name__ == "__main__":
    print("INTEGRATION GUIDE: Advanced Multi-Timeframe Trading System")
    print("=" * 60)
    print("\nRunning example workflow...")
    example_full_workflow()
