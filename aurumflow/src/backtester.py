import pandas as pd
import matplotlib.pyplot as plt
from strategy import AurumCompounderStrategy
import os

def run_aurum_backtest(csv_path):
    # Load data
    print(f"Loading data from {csv_path}...")
    # Skip the second header row (Ticker row)
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True, skiprows=[1])
    
    if df.empty:
        print("Data is empty.")
        return
    
    # Initialize strategy
    initial_balance = 10000
    strategy = AurumCompounderStrategy(initial_balance=initial_balance, risk_per_trade=0.01)
    
    # Run backtest
    final_balance, equity_curve = strategy.run_backtest(df)
    
    # Calculate Metrics
    total_return = ((final_balance - initial_balance) / initial_balance) * 100
    
    # Max Drawdown
    equity_series = pd.Series(equity_curve)
    rolling_max = equity_series.cummax()
    drawdown = (equity_series - rolling_max) / rolling_max
    max_drawdown = drawdown.min() * 100
    
    print("\n" + "="*30)
    print(" AURUMFLOW BACKTEST RESULTS")
    print("="*30)
    print(f"Initial Balance: ${initial_balance:,.2f}")
    print(f"Final Balance:   ${final_balance:,.2f}")
    print(f"Total Return:    {total_return:.2f}%")
    print(f"Max Drawdown:    {max_drawdown:.2f}%")
    print("="*30)
    
    # Plot equity curve
    plt.figure(figsize=(12, 6))
    plt.plot(equity_curve, label='Equity Curve', color='gold')
    plt.title('AurumFlow Compounding Strategy Performance')
    plt.xlabel('Trades / Days')
    plt.ylabel('Balance ($)')
    plt.grid(True, alpha=0.3)
    plt.legend()
    
    os.makedirs('docs', exist_ok=True)
    plt.savefig('docs/backtest_results.png')
    print("Equity curve saved to docs/backtest_results.png")

if __name__ == "__main__":
    run_aurum_backtest('data/GC_F.csv')
