import json, re, os, glob, pandas as pd
from datetime import datetime

try:
    from config import cfg
    JSON_PATH = cfg.JSON_PATH
except ImportError:
    JSON_PATH = "/Users/owner/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"

def _load():
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        # Fix trailing commas before ] or }
        raw = re.sub(r',\s*([\]}])', r'\1', raw)
        return json.loads(raw)
    except Exception as e:
        print(f"mt5_connector error: {e}")
        return {}

def get_json_path(): return JSON_PATH
def get_account_info(): return _load().get("account", {})
def get_symbol_prices(symbols):
    prices = _load().get("prices", [])
    return [p for p in prices if p["symbol"] in set(symbols)]
def get_last_update(): return _load().get("timestamp", "—")
def is_connected():
    try:
        age = datetime.now().timestamp() - os.path.getmtime(JSON_PATH)
        return age < 30
    except: return False

def get_open_positions():
    data = _load().get("positions", [])
    if not data:
        return pd.DataFrame(columns=["ticket","symbol","type","volume","open_price","current_price","sl","tp","profit","swap","time"])
    return pd.DataFrame(data)

def get_trade_history(days=30):
    data = _load().get("history", [])
    if not data: return pd.DataFrame()
    df = pd.DataFrame(data)
    df["time"] = pd.to_datetime(df["time"])
    df = df[df["time"] >= pd.Timestamp.now() - pd.Timedelta(days=days)].sort_values("time")
    df["cumulative_profit"] = df["profit"].cumsum()
    return df
