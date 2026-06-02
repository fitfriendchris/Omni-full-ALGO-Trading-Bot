import yfinance as yf
import pandas as pd
import os

def download_gold_data(symbol='GC=F', start='2020-01-01', end=None):
    """
    Downloads historical gold data.
    Default is GC=F (Gold Futures).
    """
    print(f"Downloading data for {symbol}...")
    data = yf.download(symbol, start=start, end=end)
    
    if data.empty:
        print("No data found.")
        return None
    
    # Ensure data directory exists
    os.makedirs('data', exist_ok=True)
    
    file_path = f'data/{symbol.replace("=", "_")}.csv'
    data.to_csv(file_path)
    print(f"Data saved to {file_path}")
    return data

if __name__ == "__main__":
    download_gold_data()
