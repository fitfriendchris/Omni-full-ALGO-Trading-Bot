import pandas as pd
import matplotlib.pyplot as plt
from baseline_strategy import BaselineAurumStrategy
import os
import numpy as np

def run_baseline_backtest(csv_path):
    print(f"Loading data from {csv_path}...")
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True, skiprows=[1])
    
    if df.empty:
        print("Data is empty.")
        return
    
    initial_balance = 10000
    strategy = BaselineAurumStrategy(initial_balance=initial_balance, risk_per_trade=0.01)
    
    final_balance, equity_curve, trade_log = strategy.run_backtest(df)
    
    # Metrics calculation
    total_return = ((final_balance - initial_balance) / initial_balance) * 100
    
    equity_series = pd.Series(equity_curve)
    rolling_max = equity_series.cummax()
    drawdown = (equity_series - rolling_max) / rolling_max
    max_drawdown = drawdown.min() * 100
    
    # Profit Factor
    trades = np.array(trade_log)
    gross_profit = trades[trades > 0].sum()
    gross_loss = np.abs(trades[trades < 0].sum())
    profit_factor = gross_profit / gross_loss if gross_loss != 0 else float('inf')
    
    # Monthly ROI (Geometric Mean)
    # Assuming daily data
    n_days = len(df)
    n_months = n_days / 21 # Approx 21 trading days per month
    monthly_roi = (pow(final_balance / initial_balance, 1/n_months) - 1) * 100
    
    print("\n" + "="*30)
    print(" AURUMFLOW BASELINE RESULTS")
    print("="*30)
    print(f"Initial Balance: ${initial_balance:,.2f}")
    print(f"Final Balance:   ${final_balance:,.2f}")
    print(f"Total Return:    {total_return:.2f}%")
    print(f"Monthly ROI:     {monthly_roi:.2f}%")
    print(f"Max Drawdown:    {max_drawdown:.2f}%")
    print(f"Profit Factor:   {profit_factor:.2f}")
    print("="*30)
    
    # Plot equity curve
    plt.figure(figsize=(12, 6))
    plt.plot(equity_curve, label='Baseline Equity Curve', color='blue')
    plt.title('AurumFlow Baseline Strategy Performance')
    plt.xlabel('Trades / Days')
    plt.ylabel('Balance ($)')
    plt.grid(True, alpha=0.3)
    plt.legend()
    
    os.makedirs('docs', exist_ok=True)
    plt.savefig('docs/baseline_backtest.png')
    print("Baseline equity curve saved to docs/baseline_backtest.png")

if __name__ == "__main__":
    run_baseline_backtest('data/GC_F.csv')
