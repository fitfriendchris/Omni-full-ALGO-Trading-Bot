import pandas as pd
import numpy as np

class AurumCompounderStrategy:
    def __init__(self, initial_balance=10000, risk_per_trade=0.01, atr_period=14, ema_fast=20, ema_slow=50, rsi_period=14, 
                 pyramiding_max=4, pyramiding_step=0.7, trailing_atr_mult=1.8):
        self.balance = initial_balance
        self.risk_per_trade = risk_per_trade
        self.atr_period = atr_period
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.pyramiding_max = pyramiding_max
        self.pyramiding_step = pyramiding_step # ATR multiplier for adding positions
        self.trailing_atr_mult = trailing_atr_mult
        self.positions = []
        self.equity_curve = []
        self.trade_log = []

    def calculate_indicators(self, df):
        # Handle potential MultiIndex columns from yfinance
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df['EMA_fast'] = df['Close'].ewm(span=self.ema_fast, adjust=False).mean()
        df['EMA_slow'] = df['Close'].ewm(span=self.ema_slow, adjust=False).mean()
        
        # RSI
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=self.rsi_period).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # Simple ATR calculation
        high_low = df['High'] - df['Low']
        high_close = np.abs(df['High'] - df['Close'].shift())
        low_close = np.abs(df['Low'] - df['Close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        df['ATR'] = true_range.rolling(window=self.atr_period).mean()
        
        # Volume Filter
        df['Volume_SMA'] = df['Volume'].rolling(window=20).mean()
        
        return df

    def run_backtest(self, df):
        df = self.calculate_indicators(df.copy())
        df = df.dropna()
        
        self.positions = [] # List of dicts: {'entry_price', 'units', 'stop_loss', 'type'}
        self.equity_curve = []
        
        for i in range(len(df)):
            current_price = df['Close'].iloc[i]
            current_date = df.index[i]
            atr = df['ATR'].iloc[i]
            ema_f = df['EMA_fast'].iloc[i]
            ema_s = df['EMA_slow'].iloc[i]
            rsi = df['RSI'].iloc[i]
            volume = df['Volume'].iloc[i]
            v_sma = df['Volume_SMA'].iloc[i]
            
            # 1. Update Trailing Stop for all existing positions
            if self.positions:
                trail_sl = current_price - (self.trailing_atr_mult * atr)
                for pos in self.positions:
                    if trail_sl > pos['stop_loss']:
                        pos['stop_loss'] = trail_sl

            # 2. Exit Logic
            reversal_signal = ema_f < ema_s or rsi > 80
            
            exited_positions = []
            remaining_positions = []
            for pos in self.positions:
                if current_price <= pos['stop_loss'] or reversal_signal:
                    exited_positions.append(pos)
                else:
                    remaining_positions.append(pos)
            
            for pos in exited_positions:
                profit = (current_price - pos['entry_price']) * pos['units']
                self.balance += profit
                self.trade_log.append(profit)
            
            self.positions = remaining_positions

            # 3. Entry / Pyramiding Logic
            if ema_f > ema_s and rsi > 50 and volume > v_sma:
                if not self.positions:
                    # Initial Entry / Re-entry
                    entry_price = current_price
                    stop_loss = entry_price - (1.5 * atr)
                    risk_amount = self.balance * self.risk_per_trade
                    risk_per_unit = entry_price - stop_loss
                    if risk_per_unit > 0:
                        units = risk_amount / risk_per_unit
                        self.positions.append({
                            'entry_price': entry_price,
                            'units': units,
                            'stop_loss': stop_loss,
                            'type': 'initial'
                        })
                elif len(self.positions) < self.pyramiding_max:
                    # Pyramiding Logic
                    last_pos = self.positions[-1]
                    if current_price >= last_pos['entry_price'] + (self.pyramiding_step * atr):
                        entry_price = current_price
                        stop_loss = entry_price - (self.trailing_atr_mult * atr)
                        
                        risk_amount = self.balance * self.risk_per_trade
                        risk_per_unit = entry_price - stop_loss
                        if risk_per_unit > 0:
                            units = risk_amount / risk_per_unit
                            self.positions.append({
                                'entry_price': entry_price,
                                'units': units,
                                'stop_loss': stop_loss,
                                'type': 'pyramid'
                            })
                            # Sync SL for all positions
                            for p in self.positions:
                                p['stop_loss'] = max(p['stop_loss'], stop_loss)
            
            self.equity_curve.append(self.balance)
            
        return self.balance, self.equity_curve, self.trade_log

if __name__ == "__main__":
    pass
