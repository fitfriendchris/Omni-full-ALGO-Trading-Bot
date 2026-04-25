"""
backtest_dashboard.py — OMNI ICT Backtest UI
Run: python backtest_dashboard.py  →  http://localhost:8051
"""

import json, os, threading
import dash
from dash import dcc, html, dash_table, Input, Output, State, callback
import plotly.graph_objects as go
import pandas as pd
from datetime import datetime

RESULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_results.json")

# ── Design ────────────────────────────────────────────────────────────────────
BG="#07090c"; BG_CARD="#0c0f14"; BG_CARD2="#11161e"; BORDER="#1c2333"
GOLD="#d4a843"; SILVER="#a8b8c8"; RED="#e84545"; GREEN="#2ecc71"
BLUE="#3b82f6"; PURPLE="#8b5cf6"; TEXT="#dde4ef"; MUTED="#4a5a72"
MONO="'JetBrains Mono','Courier New',monospace"; SANS="'Sora','Segoe UI',sans-serif"

SYMBOLS = ["XAUUSD","XAGUSD","EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD",
           "GBPJPY","EURJPY","BTCUSD","ETHUSD",".US30Cash"]

def card(children, style=None):
    s = {"background":BG_CARD,"border":f"1px solid {BORDER}","borderRadius":"10px","padding":"16px","marginBottom":"12px"}
    if style: s.update(style)
    return html.Div(children, style=s)

def badge(t, c=GOLD):
    return html.Span(t, style={"background":c+"22","color":c,"border":f"1px solid {c}44",
                                "borderRadius":"4px","padding":"2px 8px","fontSize":"10px","fontFamily":MONO,"fontWeight":"600"})

def stat(label, value, color=TEXT, sub=""):
    return html.Div([
        html.Div(label, style={"fontFamily":MONO,"fontSize":"9px","color":MUTED,"textTransform":"uppercase","letterSpacing":"0.12em"}),
        html.Div(value, style={"fontFamily":MONO,"fontSize":"18px","fontWeight":"700","color":color,"margin":"3px 0 1px"}),
        html.Div(sub,   style={"fontFamily":SANS,"fontSize":"10px","color":MUTED}) if sub else html.Span(),
    ], style={"textAlign":"center","flex":"1","padding":"0 8px"})

TH = {"backgroundColor":BG_CARD2,"color":GOLD,"fontWeight":"600","textTransform":"uppercase",
      "fontSize":"10px","letterSpacing":"0.08em","border":f"1px solid {BORDER}","fontFamily":MONO}
TC = {"backgroundColor":BG_CARD,"color":TEXT,"border":f"1px solid {BORDER}",
      "padding":"6px 10px","fontFamily":MONO,"fontSize":"11px"}

def chl(h=300, title=""):
    return dict(height=h, margin=dict(l=8,r=8,t=30 if title else 8,b=8),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor=BG,
                font=dict(family=MONO,color=MUTED,size=11),
                title=dict(text=title,font=dict(color=TEXT,size=12),x=0.01) if title else None,
                xaxis=dict(gridcolor=BORDER,zerolinecolor=BORDER),
                yaxis=dict(gridcolor=BORDER,zerolinecolor=BORDER),
                legend=dict(bgcolor="rgba(0,0,0,0)",font=dict(color=MUTED)))

# ── App ───────────────────────────────────────────────────────────────────────
app = dash.Dash(__name__, title="OMNI Backtester", update_title=None,
    external_stylesheets=["https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Sora:wght@400;500;600;700&display=swap"],
    suppress_callback_exceptions=True)

dropdown_style = {"background":BG_CARD2,"color":TEXT,"border":f"1px solid {BORDER}","borderRadius":"6px","fontFamily":MONO,"fontSize":"12px"}

app.layout = html.Div([
    dcc.Store(id="bt-results"),
    dcc.Store(id="running", data=False),

    # ── Header ────────────────────────────────────────────────────────
    html.Div([
        html.Div([
            html.Span("◈", style={"color":GOLD,"fontSize":"20px","marginRight":"8px"}),
            html.Span("OMNI", style={"fontFamily":MONO,"fontWeight":"700","fontSize":"16px","color":TEXT,"letterSpacing":"0.2em"}),
            html.Span(" BACKTESTER", style={"fontFamily":SANS,"fontSize":"12px","color":GOLD,"letterSpacing":"0.3em"}),
        ], style={"display":"flex","alignItems":"center"}),
        html.Div([
            badge("ICT Strategy"),
            html.Span("  Liquidity Sweep | OB Retest | FVG Fill | Compounding",
                      style={"fontFamily":SANS,"fontSize":"11px","color":MUTED,"marginLeft":"10px"}),
        ], style={"display":"flex","alignItems":"center"}),
    ], style={"display":"flex","justifyContent":"space-between","alignItems":"center",
              "background":BG_CARD,"borderBottom":f"1px solid {BORDER}",
              "padding":"14px 28px","position":"sticky","top":"0","zIndex":"100"}),

    html.Div([

        # ── Config Panel ──────────────────────────────────────────────
        html.Div([
            card([
                html.Div("⚙️  BACKTEST CONFIGURATION", style={"fontFamily":MONO,"fontSize":"11px","color":GOLD,
                                                                "letterSpacing":"0.1em","marginBottom":"16px","fontWeight":"700"}),

                # Symbol
                html.Div([
                    html.Label("Symbol", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED,"marginBottom":"4px","display":"block"}),
                    dcc.Dropdown(id="bt-symbol",
                        options=[{"label":s,"value":s} for s in SYMBOLS],
                        value="XAUUSD", clearable=False,
                        style={"background":BG_CARD2,"color":TEXT,"fontFamily":MONO,"fontSize":"12px"}),
                ], style={"marginBottom":"12px"}),

                # Initial equity
                html.Div([
                    html.Label("Initial Equity ($)", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED,"marginBottom":"4px","display":"block"}),
                    dcc.Input(id="bt-equity", type="number", value=10000, min=100, max=10000000,
                              style={**dropdown_style,"width":"100%","padding":"8px","color":TEXT}),
                ], style={"marginBottom":"12px"}),

                # Risk %
                html.Div([
                    html.Label("Base Risk %", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED,"marginBottom":"4px","display":"block"}),
                    dcc.Slider(id="bt-risk", min=0.5, max=5.0, step=0.5, value=2.0,
                               marks={i/2: f"{i/2}%" for i in range(1,11)},
                               tooltip={"placement":"bottom"}),
                ], style={"marginBottom":"16px"}),

                # Min confidence
                html.Div([
                    html.Label("Min Confidence", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED,"marginBottom":"4px","display":"block"}),
                    dcc.Slider(id="bt-conf", min=30, max=90, step=5, value=50,
                               marks={i: str(i) for i in range(30,91,10)},
                               tooltip={"placement":"bottom"}),
                ], style={"marginBottom":"16px"}),

                # Min RR
                html.Div([
                    html.Label("Min R:R Ratio", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED,"marginBottom":"4px","display":"block"}),
                    dcc.Slider(id="bt-rr", min=1.0, max=4.0, step=0.5, value=1.5,
                               marks={i/2: f"{i/2}:1" for i in range(2,9)},
                               tooltip={"placement":"bottom"}),
                ], style={"marginBottom":"16px"}),

                # Max open trades
                html.Div([
                    html.Label("Max Open Trades", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED,"marginBottom":"4px","display":"block"}),
                    dcc.Slider(id="bt-maxopen", min=1, max=5, step=1, value=3,
                               marks={i:str(i) for i in range(1,6)}),
                ], style={"marginBottom":"16px"}),

                # Daily loss limit
                html.Div([
                    html.Label("Daily Loss Limit %", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED,"marginBottom":"4px","display":"block"}),
                    dcc.Slider(id="bt-dayloss", min=2, max=15, step=1, value=6,
                               marks={i:f"{i}%" for i in [2,4,6,8,10,12,15]}),
                ], style={"marginBottom":"16px"}),

                # Compounding toggle
                html.Div([
                    html.Label("Compounding", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED,"marginBottom":"4px","display":"block"}),
                    dcc.RadioItems(id="bt-compound",
                        options=[{"label":" Enabled","value":True},{"label":" Fixed","value":False}],
                        value=True, inline=True,
                        style={"fontFamily":MONO,"fontSize":"11px","color":TEXT}),
                ], style={"marginBottom":"20px"}),

                # Run button
                html.Button("▶  RUN BACKTEST", id="bt-run", n_clicks=0,
                    style={"width":"100%","padding":"12px","background":f"linear-gradient(135deg,{GOLD},{GOLD}88)",
                           "color":"#000","fontFamily":MONO,"fontWeight":"700","fontSize":"13px",
                           "border":"none","borderRadius":"8px","cursor":"pointer","letterSpacing":"0.1em"}),

                # Status
                html.Div(id="bt-status", style={"marginTop":"10px","fontFamily":MONO,"fontSize":"11px",
                                                  "color":MUTED,"textAlign":"center"}),
            ]),
        ], style={"width":"260px","minWidth":"260px","flexShrink":"0"}),

        # ── Results Panel ─────────────────────────────────────────────
        html.Div([
            html.Div(id="bt-results-panel", children=[
                # Placeholder
                html.Div([
                    html.Div("◈", style={"fontSize":"48px","color":GOLD+"44","marginBottom":"16px"}),
                    html.Div("Configure & Run Backtest", style={"fontFamily":MONO,"fontSize":"14px","color":MUTED}),
                    html.Div("Results will appear here", style={"fontFamily":SANS,"fontSize":"12px","color":MUTED+"88","marginTop":"8px"}),
                ], style={"textAlign":"center","padding":"80px 0"}),
            ]),
        ], style={"flex":"1","minWidth":"0"}),

    ], style={"display":"flex","gap":"16px","padding":"20px 28px","maxWidth":"1800px","margin":"0 auto"}),

], style={"backgroundColor":BG,"minHeight":"100vh","fontFamily":SANS,"color":TEXT})


# ── Callbacks ─────────────────────────────────────────────────────────────────

@callback(
    Output("bt-results","data"),
    Output("bt-status","children"),
    Input("bt-run","n_clicks"),
    State("bt-symbol","value"),
    State("bt-equity","value"),
    State("bt-risk","value"),
    State("bt-conf","value"),
    State("bt-rr","value"),
    State("bt-maxopen","value"),
    State("bt-dayloss","value"),
    State("bt-compound","value"),
    prevent_initial_call=True,
)
def run_bt(n, symbol, equity, risk, conf, rr, maxopen, dayloss, compound):
    if not n:
        return None, ""

    try:
        from backtester import run_backtest, BacktestConfig
        cfg = BacktestConfig(
            symbol=symbol,
            initial_equity=float(equity or 10000),
            base_risk_pct=float(risk),
            min_confidence=int(conf),
            min_rr=float(rr),
            max_open=int(maxopen),
            daily_loss_limit=float(dayloss),
            compound=bool(compound),
        )
        result = run_backtest(cfg)

        # Serialize
        data = {
            "symbol":           result.symbol,
            "initial_equity":   result.initial_equity,
            "final_equity":     result.final_equity,
            "total_pnl":        result.total_pnl,
            "total_return_pct": result.total_return_pct,
            "total_trades":     result.total_trades,
            "winning_trades":   result.winning_trades,
            "losing_trades":    result.losing_trades,
            "win_rate":         result.win_rate,
            "profit_factor":    result.profit_factor,
            "max_drawdown_pct": result.max_drawdown_pct,
            "max_drawdown_usd": result.max_drawdown_usd,
            "avg_win_r":        result.avg_win_r,
            "avg_loss_r":       result.avg_loss_r,
            "expectancy_r":     result.expectancy_r,
            "sharpe_ratio":     result.sharpe_ratio,
            "gross_profit":     result.gross_profit,
            "gross_loss":       result.gross_loss,
            "longest_win_streak":  result.longest_win_streak,
            "longest_loss_streak": result.longest_loss_streak,
            "setups_found":     result.setups_found,
            "bars_processed":   result.bars_processed,
            "start_date":       result.start_date,
            "end_date":         result.end_date,
            "run_time_ms":      result.run_time_ms,
            "monthly_returns":  result.monthly_returns,
            "equity_curve": result.equity_curve,
            "trades": [{
                "id":t.id,"symbol":t.symbol,"direction":t.direction,
                "entry_type":t.entry_type,"entry_time":t.entry_time,
                "entry_price":t.entry_price,"exit_time":t.exit_time,
                "exit_price":t.exit_price,"exit_reason":t.exit_reason,
                "sl":t.sl,"tp1":t.tp1,"lot_size":t.lot_size,
                "pnl":t.pnl,"pnl_r":t.pnl_r,"confidence":t.confidence,
                "session":t.session,"amd_phase":t.amd_phase,
            } for t in result.trades],
        }
        status = f"✅ Done in {result.run_time_ms}ms — {result.total_trades} trades"
        return data, status

    except Exception as e:
        return None, f"❌ Error: {str(e)[:80]}"


@callback(
    Output("bt-results-panel","children"),
    Input("bt-results","data"),
)
def show_results(data):
    if not data:
        return html.Div([
            html.Div("◈", style={"fontSize":"48px","color":GOLD+"44","marginBottom":"16px"}),
            html.Div("Configure & Run Backtest", style={"fontFamily":MONO,"fontSize":"14px","color":MUTED}),
        ], style={"textAlign":"center","padding":"80px 0"})

    r = data
    ret_pct = r["total_return_pct"]
    ret_col = GREEN if ret_pct >= 0 else RED
    dd_col  = RED if r["max_drawdown_pct"] > 10 else GOLD if r["max_drawdown_pct"] > 5 else GREEN
    pf_col  = GREEN if r["profit_factor"] > 1.5 else GOLD if r["profit_factor"] > 1.0 else RED
    wr_col  = GREEN if r["win_rate"] > 55 else GOLD if r["win_rate"] > 45 else RED

    # Grade
    grade_score = 0
    if ret_pct > 0:            grade_score += 20
    if r["win_rate"] > 50:     grade_score += 20
    if r["profit_factor"] > 1.3: grade_score += 20
    if r["max_drawdown_pct"] < 15: grade_score += 20
    if r["expectancy_r"] > 0.2:  grade_score += 20
    grade = "A+" if grade_score>=90 else "A" if grade_score>=80 else "B" if grade_score>=60 else "C" if grade_score>=40 else "D"
    grade_col = GREEN if grade in("A+","A") else GOLD if grade=="B" else RED

    # ── Equity Curve ──────────────────────────────────────────────────
    eq_curve = r.get("equity_curve", [])
    fig_eq = go.Figure()
    if eq_curve:
        times = [e["time"] for e in eq_curve]
        equities = [e["equity"] for e in eq_curve]
        dds = [e["drawdown"] for e in eq_curve]

        fig_eq.add_trace(go.Scatter(
            x=times, y=equities, name="Equity",
            line=dict(color=GOLD, width=2), fill="tozeroy", fillcolor=GOLD+"15",
            hovertemplate="<b>%{x}</b><br>Equity: $%{y:,.2f}<extra></extra>",
        ))
        layout = chl(280, "Equity Curve")
        layout["yaxis"]["tickprefix"] = "$"
        fig_eq.update_layout(**layout)

    # ── Drawdown ──────────────────────────────────────────────────────
    fig_dd = go.Figure()
    if eq_curve:
        fig_dd.add_trace(go.Scatter(
            x=times, y=[-d for d in dds], name="Drawdown",
            line=dict(color=RED, width=1.5), fill="tozeroy", fillcolor=RED+"18",
            hovertemplate="<b>%{x}</b><br>DD: %{y:.2f}%<extra></extra>",
        ))
        fig_dd.update_layout(**chl(180, "Drawdown %"))

    # ── Monthly bar chart ─────────────────────────────────────────────
    monthly = r.get("monthly_returns", {})
    fig_mo = go.Figure()
    if monthly:
        months = sorted(monthly.keys())
        values = [monthly[m] for m in months]
        colors = [GREEN if v >= 0 else RED for v in values]
        fig_mo.add_trace(go.Bar(x=months, y=values, marker_color=colors,
                                hovertemplate="<b>%{x}</b><br>P&L: $%{y:,.2f}<extra></extra>"))
        fig_mo.update_layout(**chl(200, "Monthly P&L"))

    # ── Trade scatter ──────────────────────────────────────────────────
    trades = r.get("trades", [])
    fig_tr = go.Figure()
    if trades:
        wins  = [t for t in trades if t["pnl"] >= 0]
        losses= [t for t in trades if t["pnl"] <  0]
        for group, col, name in [(wins, GREEN, "Win"), (losses, RED, "Loss")]:
            if group:
                fig_tr.add_trace(go.Scatter(
                    x=[t["entry_time"] for t in group],
                    y=[t["pnl_r"] for t in group],
                    mode="markers",
                    marker=dict(color=col, size=8, opacity=0.8,
                                symbol="triangle-up" if name=="Win" else "triangle-down"),
                    name=name,
                    hovertemplate="<b>%{x}</b><br>%{y:.2f}R<extra></extra>",
                ))
        fig_tr.add_hline(y=0, line_dash="dot", line_color=MUTED, line_width=1)
        fig_tr.update_layout(**chl(200, "Trades (R)"))

    # ── Session breakdown ─────────────────────────────────────────────
    session_pnl = {}
    for t in trades:
        s = t.get("session", "UNKNOWN")
        session_pnl[s] = session_pnl.get(s, 0) + t["pnl"]
    fig_sess = go.Figure()
    if session_pnl:
        sc = {k: (GREEN if v >= 0 else RED) for k, v in session_pnl.items()}
        fig_sess.add_trace(go.Bar(
            x=list(session_pnl.keys()),
            y=list(session_pnl.values()),
            marker_color=[sc[k] for k in session_pnl.keys()],
        ))
        fig_sess.update_layout(**chl(200, "P&L by Session"))

    # ── Win/loss by setup type ────────────────────────────────────────
    type_pnl = {}
    for t in trades:
        tp = t.get("entry_type", "UNKNOWN")
        if tp not in type_pnl:
            type_pnl[tp] = {"pnl":0,"count":0,"wins":0}
        type_pnl[tp]["pnl"]   += t["pnl"]
        type_pnl[tp]["count"] += 1
        if t["pnl"] >= 0:
            type_pnl[tp]["wins"] += 1

    # ── Trades table ──────────────────────────────────────────────────
    trade_rows = []
    for t in sorted(trades, key=lambda x: x["entry_time"], reverse=True):
        trade_rows.append({
            "#":         t["id"],
            "Time":      t["entry_time"][:16],
            "Dir":       t["direction"],
            "Type":      t["entry_type"].replace("_"," "),
            "Entry":     f"{t['entry_price']:.5g}",
            "Exit":      f"{t['exit_price']:.5g}",
            "Reason":    t["exit_reason"],
            "Lot":       t["lot_size"],
            "P&L":       f"${t['pnl']:+,.2f}",
            "R":         f"{t['pnl_r']:+.2f}R",
            "Conf":      t["confidence"],
            "Session":   t["session"],
            "AMD":       t["amd_phase"],
        })
    df_trades = pd.DataFrame(trade_rows) if trade_rows else pd.DataFrame()

    # ── Verdict box ───────────────────────────────────────────────────
    if grade in ("A+","A"):
        verdict_msg = "🚀 Excellent results! This strategy is ready for live trading."
        verdict_col = GREEN
    elif grade == "B":
        verdict_msg = "✅ Solid results. Consider testing on more symbols before going live."
        verdict_col = GOLD
    else:
        verdict_msg = "⚠️  Strategy needs improvement. Adjust parameters and re-run."
        verdict_col = RED

    return html.Div([

        # Grade + summary
        html.Div([
            html.Div([
                html.Div(grade, style={"fontFamily":MONO,"fontSize":"52px","fontWeight":"700",
                                       "color":grade_col,"lineHeight":"1"}),
                html.Div("GRADE", style={"fontFamily":MONO,"fontSize":"9px","color":MUTED,"letterSpacing":"0.15em"}),
            ], style={"textAlign":"center","borderRight":f"1px solid {BORDER}","paddingRight":"20px","marginRight":"20px"}),
            html.Div(verdict_msg, style={"fontFamily":SANS,"fontSize":"13px","color":verdict_col,"flex":"1"}),
        ], style={"display":"flex","alignItems":"center","background":verdict_col+"11",
                  "border":f"1px solid {verdict_col}44","borderRadius":"10px","padding":"16px 20px","marginBottom":"14px"}),

        # Key stats grid
        card([
            html.Div([
                stat("Return", f"{ret_pct:+.1f}%", ret_col, f"${r['total_pnl']:+,.2f}"),
                html.Div(style={"width":"1px","background":BORDER}),
                stat("Win Rate", f"{r['win_rate']:.1f}%", wr_col,
                     f"{r['winning_trades']}W / {r['losing_trades']}L"),
                html.Div(style={"width":"1px","background":BORDER}),
                stat("Profit Factor", f"{r['profit_factor']:.2f}", pf_col),
                html.Div(style={"width":"1px","background":BORDER}),
                stat("Max DD", f"{r['max_drawdown_pct']:.1f}%", dd_col, f"${r['max_drawdown_usd']:,.2f}"),
                html.Div(style={"width":"1px","background":BORDER}),
                stat("Expectancy", f"{r['expectancy_r']:+.3f}R",
                     GREEN if r['expectancy_r'] > 0 else RED),
                html.Div(style={"width":"1px","background":BORDER}),
                stat("Sharpe", f"{r['sharpe_ratio']:.2f}",
                     GREEN if r['sharpe_ratio'] > 1 else GOLD if r['sharpe_ratio'] > 0 else RED),
                html.Div(style={"width":"1px","background":BORDER}),
                stat("Total Trades", str(r["total_trades"]), TEXT,
                     f"{r['setups_found']} setups found"),
                html.Div(style={"width":"1px","background":BORDER}),
                stat("Avg Win", f"{r['avg_win_r']:.2f}R", GREEN),
                html.Div(style={"width":"1px","background":BORDER}),
                stat("Avg Loss", f"{r['avg_loss_r']:.2f}R", RED),
            ], style={"display":"flex","alignItems":"center","gap":"0","flexWrap":"wrap"}),
        ]),

        # Charts row 1
        html.Div([
            html.Div([card([dcc.Graph(figure=fig_eq, config={"displayModeBar":False})])], style={"flex":"2"}),
            html.Div([card([dcc.Graph(figure=fig_dd, config={"displayModeBar":False})])], style={"flex":"1"}),
        ], style={"display":"flex","gap":"12px"}),

        # Charts row 2
        html.Div([
            html.Div([card([dcc.Graph(figure=fig_mo, config={"displayModeBar":False})])], style={"flex":"1"}),
            html.Div([card([dcc.Graph(figure=fig_tr, config={"displayModeBar":False})])], style={"flex":"1"}),
            html.Div([card([dcc.Graph(figure=fig_sess, config={"displayModeBar":False})])], style={"flex":"1"}),
        ], style={"display":"flex","gap":"12px"}),

        # Setup type breakdown
        card([
            html.Div("Setup Type Performance", style={"fontFamily":MONO,"fontSize":"10px","color":GOLD,
                                                       "textTransform":"uppercase","letterSpacing":"0.1em","marginBottom":"10px"}),
            html.Div([
                html.Div([
                    html.Div(tp.replace("_"," "), style={"fontFamily":MONO,"fontSize":"11px","color":TEXT,"marginBottom":"4px"}),
                    html.Div([
                        html.Div(style={
                            "height":"4px","borderRadius":"2px","marginBottom":"4px",
                            "width":f"{min(info['wins']/info['count']*100,100):.0f}%",
                            "background":f"linear-gradient(90deg,{GREEN},{GOLD})",
                        }),
                    ], style={"background":BORDER,"borderRadius":"2px","height":"4px","marginBottom":"4px"}),
                    html.Div([
                        html.Span(f"{info['wins']}/{info['count']} trades  ", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED}),
                        html.Span(f"${info['pnl']:+,.2f}", style={"fontFamily":MONO,"fontSize":"10px",
                                                                    "color":GREEN if info['pnl']>=0 else RED}),
                    ]),
                ], style={"flex":"1","padding":"10px","background":BG_CARD2,"borderRadius":"8px","border":f"1px solid {BORDER}"})
            for tp, info in type_pnl.items()],
            style={"display":"flex","gap":"10px","flexWrap":"wrap"}),
        ]),

        # Trade list
        card([
            html.Div("All Trades", style={"fontFamily":MONO,"fontSize":"10px","color":GOLD,
                                           "textTransform":"uppercase","letterSpacing":"0.1em","marginBottom":"10px"}),
            dash_table.DataTable(
                data=df_trades.to_dict("records") if not df_trades.empty else [],
                columns=[{"name":c,"id":c} for c in df_trades.columns] if not df_trades.empty else [],
                style_table={"overflowX":"auto"},
                style_header=TH, style_cell=TC,
                style_data_conditional=[
                    {"if":{"filter_query":'{Dir} = "BUY"', "column_id":"Dir"},"color":GREEN,"fontWeight":"700"},
                    {"if":{"filter_query":'{Dir} = "SELL"',"column_id":"Dir"},"color":RED,"fontWeight":"700"},
                    {"if":{"filter_query":'{Reason} = "SL"', "column_id":"Reason"},"color":RED},
                    {"if":{"filter_query":'{Reason} = "TP3"',"column_id":"Reason"},"color":GREEN,"fontWeight":"700"},
                    {"if":{"filter_query":'{Reason} = "TP2"',"column_id":"Reason"},"color":GREEN},
                    {"if":{"filter_query":'{Reason} = "TP1"',"column_id":"Reason"},"color":GOLD},
                ],
                sort_action="native", filter_action="native", page_size=15,
            ) if not df_trades.empty else html.P("No trades", style={"color":MUTED,"fontFamily":MONO}),
        ]),

        # Info bar
        html.Div([
            html.Span(f"📊 {r['symbol']}  ", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED}),
            html.Span(f"{r['bars_processed']} bars  ", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED}),
            html.Span(f"{r['start_date'][:10]} → {r['end_date'][:10]}  ", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED}),
            html.Span(f"Ran in {r['run_time_ms']}ms", style={"fontFamily":MONO,"fontSize":"10px","color":MUTED}),
        ], style={"padding":"8px 0","textAlign":"right"}),
    ])


if __name__ == "__main__":
    print("\n  ◈ OMNI Backtest Dashboard")
    print("  → http://localhost:8051\n")
    app.run(debug=True, host="0.0.0.0", port=8051)
