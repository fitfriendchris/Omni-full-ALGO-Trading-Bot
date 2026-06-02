import pandas as pd
import numpy as np

class BaselineAurumStrategy:
    def __init__(self, initial_balance=10000, risk_per_trade=0.01, atr_period=14, ema_fast=20, ema_slow=50, rsi_period=14):
        self.balance = initial_balance
        self.risk_per_trade = risk_per_trade
        self.atr_period = atr_period
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.positions = []
        self.equity_curve = []
        self.trade_log = []

    def calculate_indicators(self, df):
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df['EMA_fast'] = df['Close'].ewm(span=self.ema_fast, adjust=False).mean()
        df['EMA_slow'] = df['Close'].ewm(span=self.ema_slow, adjust=False).mean()
        
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=self.rsi_period).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        high_low = df['High'] - df['Low']
        high_close = np.abs(df['High'] - df['Close'].shift())
        low_close = np.abs(df['Low'] - df['Close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        df['ATR'] = true_range.rolling(window=self.atr_period).mean()
        
        return df

    def run_backtest(self, df):
        df = self.calculate_indicators(df.copy())
        df = df.dropna()
        
        in_position = False
        entry_price = 0
        stop_loss = 0
        units = 0
        
        for i in range(len(df)):
            current_price = df['Close'].iloc[i]
            current_date = df.index[i]
            atr = df['ATR'].iloc[i]
            ema_f = df['EMA_fast'].iloc[i]
            ema_s = df['EMA_slow'].iloc[i]
            rsi = df['RSI'].iloc[i]
            
            if not in_position:
                if ema_f > ema_s and rsi > 50:
                    in_position = True
                    entry_price = current_price
                    stop_loss = entry_price - (1.5 * atr)
                    risk_amount = self.balance * self.risk_per_trade
                    risk_per_unit = entry_price - stop_loss
                    if risk_per_unit > 0:
                        units = risk_amount / risk_per_unit
                    else:
                        in_position = False
                        continue
            else:
                if current_price <= stop_loss or ema_f < ema_s or rsi > 80:
                    profit = (current_price - entry_price) * units
                    self.balance += profit
                    self.trade_log.append(profit)
                    in_position = False
            
            self.equity_curve.append(self.balance)
            
        return self.balance, self.equity_curve, self.trade_log
