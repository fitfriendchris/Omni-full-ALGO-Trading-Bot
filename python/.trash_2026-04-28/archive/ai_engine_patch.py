"""
ai_engine_patch.py
==================
Apply these MINIMAL changes to ai_engine.py to wire in data_router.py.
Only 3 edits needed — everything else stays the same.

──────────────────────────────────────────────────────────────────────────────
EDIT 1 of 3 — Top of file, replace the _load_mt5 import block (~line 26-31)
──────────────────────────────────────────────────────────────────────────────

REMOVE:
    JSON_PATH = (
        "/Users/owner/Library/Application Support/"
        ...
    )

ADD:
    from data_router import load_data as _load_from_router, get_source_status

──────────────────────────────────────────────────────────────────────────────
EDIT 2 of 3 — Replace _load_mt5() function (~line 136-142)
──────────────────────────────────────────────────────────────────────────────

REMOVE:
    def _load_mt5() -> dict:
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                raw = re.sub(r',\s*([\]}])', r'\1', f.read())
            return json.loads(raw)
        except Exception:
            return {}

ADD:
    def _load_mt5() -> dict:
        \"\"\"
        Now uses data_router — transparently returns MT5 or TradingView data.
        Falls back gracefully: MT5 → TV → empty dict.
        \"\"\"
        return _load_from_router()

──────────────────────────────────────────────────────────────────────────────
EDIT 3 of 3 — In OmniAI._refresh_regime(), after regime is built (~line 380)
Add source metadata to the regime dict so the dashboard can display it.
──────────────────────────────────────────────────────────────────────────────

FIND (in _refresh_regime or wherever regime is assembled):
    regime = _detect_regime(data)

ADD AFTER:
    # Inject data source info into regime
    src = get_source_status()
    regime["data_source"]  = src["active_source"]
    regime["mt5_live"]     = src["mt5_connected"]
    regime["tv_live"]      = src["tv_connected"]

──────────────────────────────────────────────────────────────────────────────
DASHBOARD UPDATE — dashboard.py source badge (optional but nice)
──────────────────────────────────────────────────────────────────────────────

In dashboard.py wherever you show the connection status badge, add:

    from data_router import get_source_status

    def _source_badge():
        status = get_source_status()
        src    = status["active_source"]
        color  = {"MT5": "#2ecc71", "TRADINGVIEW": "#3b82f6", "NONE": "#e84545"}[src]
        label  = {
            "MT5":         f"● MT5 LIVE  ({status['mt5_age_s']}s)",
            "TRADINGVIEW": f"● TV LIVE   ({status['tv_age_s']}s)",
            "NONE":        "○ NO DATA",
        }[src]
        return html.Span(label, style={"color": color, "fontFamily": "JetBrains Mono", "fontSize": "11px"})

──────────────────────────────────────────────────────────────────────────────
MORNING BRIEF — add to dashboard or run from terminal
──────────────────────────────────────────────────────────────────────────────

    from tradingview_connector import morning_brief
    print(morning_brief())           # or wire to a dashboard button

──────────────────────────────────────────────────────────────────────────────
"""

# ── Quick self-test ────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        from data_router import load_data, get_source_status, load_rules
        status = get_source_status()
        print("\n[OMNI Data Router Status]")
        print(f"  Active: {status['active_source']}")
        print(f"  MT5:    {'✓' if status['mt5_connected'] else '✗'} ({status['mt5_age_s']}s)")
        print(f"  TV:     {'✓' if status['tv_connected']  else '✗'} ({status['tv_age_s']}s)")

        data = load_data()
        print(f"\n  Source:  {data.get('_source', '?')}")
        print(f"  Prices:  {len(data.get('prices', []))} symbols")
        print(f"  Session: {data.get('session', '—')}")
        print(f"  AMD:     {data.get('amd_phase', '—')}")

        rules = load_rules()
        print(f"\n  Watchlist: {rules['watchlist']}")
        print(f"  Risk:      {rules['risk_rules']['max_risk_per_trade_pct']}% per trade, "
              f"min RR {rules['risk_rules']['min_rr_ratio']}")
    except ImportError:
        print("Place data_router.py in the same folder and re-run.")
