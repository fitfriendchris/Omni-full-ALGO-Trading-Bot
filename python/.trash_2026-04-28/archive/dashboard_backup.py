"""
OMNI Trading Dashboard — Dash + Plotly
Connect your MT5 credentials below and run:  python dashboard.py
"""

import os
import dash
from dash import dcc, html, dash_table, Input, Output, callback
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from datetime import datetime

# ── Optional: load from .env or set directly here ────────────────────────────
MT5_LOGIN    = int(os.getenv("MT5_LOGIN",    "0"))      # ← your login
MT5_PASSWORD = os.getenv("MT5_PASSWORD",     "")        # ← your password
MT5_SERVER   = os.getenv("MT5_SERVER",       "")        # ← e.g. "ICMarkets-Demo"

WATCHLIST    = ["EURUSD", "GBPUSD", "XAUUSD", "US30", "BTCUSD", "NAS100"]
REFRESH_MS   = 5_000   # live refresh every 5 seconds

# ── Try to import MT5 connector (skip gracefully if MT5 not installed) ────────
try:
    import mt5_connector as mt5c
    MT5_AVAILABLE = mt5c.is_connected()
except ImportError:
    MT5_AVAILABLE = False
    mt5c = None
else:
    MT5_AVAILABLE = mt5c.is_connected()

# ─────────────────────────────────────────────────────────────────────────────
# Colour palette / design tokens
# ─────────────────────────────────────────────────────────────────────────────
BG_PAGE   = "#0a0c0f"
BG_CARD   = "#0f1217"
BG_CARD2  = "#141920"
BORDER    = "#1e2530"
ACCENT    = "#00e5a0"       # neon green
ACCENT2   = "#007aff"       # electric blue
RED       = "#ff3d5a"
GREEN     = "#00e5a0"
TEXT_PRI  = "#e8ecf1"
TEXT_SEC  = "#5a6a7e"
FONT_MONO = "'JetBrains Mono', 'Fira Code', monospace"
FONT_SANS = "'DM Sans', 'Sora', sans-serif"

TABLE_STYLE = {
    "backgroundColor": BG_CARD,
    "color": TEXT_PRI,
    "fontFamily": FONT_MONO,
    "fontSize": "12px",
    "border": "none",
}
TABLE_HEADER = {
    "backgroundColor": BG_CARD2,
    "color": ACCENT,
    "fontWeight": "600",
    "textTransform": "uppercase",
    "fontSize": "11px",
    "letterSpacing": "0.08em",
    "border": f"1px solid {BORDER}",
}
TABLE_CELL = {
    "backgroundColor": BG_CARD,
    "color": TEXT_PRI,
    "border": f"1px solid {BORDER}",
    "padding": "8px 12px",
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sign_color(val):
    try:
        return GREEN if float(val) >= 0 else RED
    except Exception:
        return TEXT_PRI


def stat_card(label: str, value, sub: str = "", accent=ACCENT):
    return html.Div([
        html.P(label, style={
            "margin": "0 0 4px", "fontSize": "11px", "letterSpacing": "0.1em",
            "textTransform": "uppercase", "color": TEXT_SEC, "fontFamily": FONT_SANS,
        }),
        html.H2(value, style={
            "margin": "0", "fontSize": "26px", "fontWeight": "700",
            "color": accent, "fontFamily": FONT_MONO, "lineHeight": "1.1",
        }),
        html.P(sub, style={
            "margin": "4px 0 0", "fontSize": "11px", "color": TEXT_SEC, "fontFamily": FONT_SANS,
        }),
    ], style={
        "background": BG_CARD,
        "border": f"1px solid {BORDER}",
        "borderTop": f"2px solid {accent}",
        "borderRadius": "8px",
        "padding": "18px 20px",
        "flex": "1",
        "minWidth": "140px",
    })


def section_header(title: str, badge: str = ""):
    return html.Div([
        html.Span(title, style={
            "fontFamily": FONT_SANS, "fontWeight": "600",
            "fontSize": "13px", "letterSpacing": "0.06em",
            "textTransform": "uppercase", "color": TEXT_PRI,
        }),
        html.Span(badge, style={
            "marginLeft": "10px", "fontSize": "10px",
            "background": ACCENT + "22", "color": ACCENT,
            "borderRadius": "4px", "padding": "2px 7px",
            "fontFamily": FONT_MONO,
        }) if badge else html.Span(),
    ], style={"marginBottom": "12px", "display": "flex", "alignItems": "center"})


def card(children, style_extra=None):
    base = {
        "background": BG_CARD,
        "border": f"1px solid {BORDER}",
        "borderRadius": "8px",
        "padding": "20px",
        "marginBottom": "16px",
    }
    if style_extra:
        base.update(style_extra)
    return html.Div(children, style=base)


# ─────────────────────────────────────────────────────────────────────────────
# Empty chart placeholders
# ─────────────────────────────────────────────────────────────────────────────

def empty_fig(msg="No data"):
    fig = go.Figure()
    fig.add_annotation(text=msg, x=0.5, y=0.5, xref="paper", yref="paper",
                       font=dict(color=TEXT_SEC, size=13, family=FONT_MONO), showarrow=False)
    fig.update_layout(**chart_layout())
    return fig


def chart_layout(height=280):
    return dict(
        height=height,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor=BG_PAGE,
        font=dict(family=FONT_MONO, color=TEXT_SEC, size=11),
        xaxis=dict(gridcolor=BORDER, zerolinecolor=BORDER, showgrid=True),
        yaxis=dict(gridcolor=BORDER, zerolinecolor=BORDER, showgrid=True),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT_SEC)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# App layout
# ─────────────────────────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    title="OMNI | Trading Dashboard",
    update_title=None,
    external_stylesheets=[
        "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=DM+Sans:wght@400;500;600;700&display=swap"
    ],
    suppress_callback_exceptions=True,
)

app.layout = html.Div([

    # ── Live refresh interval ──────────────────────────────────────────────
    dcc.Interval(id="interval", interval=REFRESH_MS, n_intervals=0),

    # ── Top nav bar ────────────────────────────────────────────────────────
    html.Div([
        html.Div([
            html.Span("◈", style={"color": ACCENT, "fontSize": "20px", "marginRight": "10px"}),
            html.Span("OMNI", style={
                "fontFamily": FONT_MONO, "fontWeight": "600",
                "fontSize": "18px", "color": TEXT_PRI, "letterSpacing": "0.15em",
            }),
            html.Span("TRADING", style={
                "fontFamily": FONT_SANS, "fontWeight": "400",
                "fontSize": "13px", "color": TEXT_SEC,
                "marginLeft": "8px", "letterSpacing": "0.2em",
            }),
        ], style={"display": "flex", "alignItems": "center"}),

        html.Div([
            html.Span(id="mt5-status", children="● CONNECTING", style={
                "fontFamily": FONT_MONO, "fontSize": "11px",
                "color": ACCENT if MT5_AVAILABLE else RED,
                "letterSpacing": "0.08em",
            }),
            html.Span(id="clock", style={
                "fontFamily": FONT_MONO, "fontSize": "11px",
                "color": TEXT_SEC, "marginLeft": "20px",
            }),
        ], style={"display": "flex", "alignItems": "center"}),
    ], style={
        "display": "flex", "justifyContent": "space-between", "alignItems": "center",
        "background": BG_CARD, "borderBottom": f"1px solid {BORDER}",
        "padding": "14px 28px",
        "position": "sticky", "top": "0", "zIndex": "100",
    }),

    # ── Main content ───────────────────────────────────────────────────────
    html.Div([

        # ── Account stat cards ─────────────────────────────────────────────
        html.Div(id="account-stats", style={
            "display": "flex", "gap": "12px", "flexWrap": "wrap",
            "marginBottom": "16px",
        }),

        # ── Row 1: P&L curve + Positions ──────────────────────────────────
        html.Div([

            # P&L equity curve
            html.Div([
                card([
                    section_header("Equity Curve / P&L", "30d"),
                    dcc.Graph(id="pnl-chart", figure=empty_fig("Loading…"),
                              config={"displayModeBar": False}),
                ])
            ], style={"flex": "1.6", "minWidth": "300px"}),

            # Drawdown
            html.Div([
                card([
                    section_header("Drawdown"),
                    dcc.Graph(id="dd-chart", figure=empty_fig("Loading…"),
                              config={"displayModeBar": False}),
                ])
            ], style={"flex": "1", "minWidth": "260px"}),

        ], style={"display": "flex", "gap": "16px", "flexWrap": "wrap"}),

        # ── Row 2: Price chart + watchlist ─────────────────────────────────
        html.Div([

            html.Div([
                card([
                    html.Div([
                        section_header("Price Chart"),
                        dcc.Dropdown(
                            id="symbol-select",
                            options=[{"label": s, "value": s} for s in WATCHLIST],
                            value=WATCHLIST[0],
                            clearable=False,
                            style={
                                "width": "160px",
                                "background": BG_CARD2,
                                "color": TEXT_PRI,
                                "border": f"1px solid {BORDER}",
                                "borderRadius": "6px",
                                "fontSize": "12px",
                                "fontFamily": FONT_MONO,
                            },
                        ),
                    ], style={"display": "flex", "justifyContent": "space-between",
                               "alignItems": "center", "marginBottom": "12px"}),
                    dcc.Graph(id="price-chart", figure=empty_fig("Select a symbol"),
                              config={"displayModeBar": False}),
                ])
            ], style={"flex": "2", "minWidth": "320px"}),

            # Watchlist
            html.Div([
                card([
                    section_header("Watchlist"),
                    html.Div(id="watchlist-panel"),
                ])
            ], style={"flex": "0.8", "minWidth": "200px"}),

        ], style={"display": "flex", "gap": "16px", "flexWrap": "wrap"}),

        # ── Row 3: Open positions table ────────────────────────────────────
        card([
            section_header("Open Positions"),
            html.Div(id="positions-table"),
        ]),

        # ── Row 4: Trade history table ─────────────────────────────────────
        card([
            html.Div([
                section_header("Trade History"),
                html.Div([
                    html.Label("Days:", style={"color": TEXT_SEC, "fontSize": "12px",
                                               "fontFamily": FONT_SANS, "marginRight": "8px"}),
                    dcc.Slider(id="history-days", min=7, max=90, step=7, value=30,
                               marks={7: "7d", 30: "30d", 60: "60d", 90: "90d"},
                               tooltip={"placement": "top"},
                               className="history-slider"),
                ], style={"display": "flex", "alignItems": "center", "gap": "10px",
                           "flex": "1", "maxWidth": "300px"}),
            ], style={"display": "flex", "justifyContent": "space-between",
                       "alignItems": "center", "flexWrap": "wrap", "gap": "10px",
                       "marginBottom": "12px"}),
            html.Div(id="history-table"),
        ]),

    ], style={
        "padding": "20px 28px",
        "maxWidth": "1600px",
        "margin": "0 auto",
    }),

], style={
    "backgroundColor": BG_PAGE,
    "minHeight": "100vh",
    "fontFamily": FONT_SANS,
    "color": TEXT_PRI,
})


# ─────────────────────────────────────────────────────────────────────────────
# Callbacks
# ─────────────────────────────────────────────────────────────────────────────

@callback(Output("clock", "children"), Input("interval", "n_intervals"))
def update_clock(_):
    return datetime.now().strftime("%Y-%m-%d  %H:%M:%S")


@callback(
    Output("account-stats", "children"),
    Output("mt5-status", "children"),
    Output("mt5-status", "style"),
    Input("interval", "n_intervals"),
)
def update_account_stats(_):
    if not MT5_AVAILABLE or mt5c is None:
        cards = [
            stat_card("Balance",     "—",   "Not connected", RED),
            stat_card("Equity",      "—",   "",              RED),
            stat_card("Margin",      "—",   "",              RED),
            stat_card("Free Margin", "—",   "",              RED),
            stat_card("Open P&L",    "—",   "",              RED),
            stat_card("Leverage",    "—",   "",              TEXT_SEC),
        ]
        return cards, "● DISCONNECTED", {"fontFamily": FONT_MONO, "fontSize": "11px",
                                          "color": RED, "letterSpacing": "0.08em"}

    info = mt5c.get_account_info()
    if not info:
        return [], "● ERROR", {}

    cur = info.get("currency", "USD")
    profit = info.get("profit", 0)
    p_col  = GREEN if profit >= 0 else RED

    cards = [
        stat_card("Balance",     f"{info['balance']:,.2f} {cur}",   info.get("name", ""), ACCENT2),
        stat_card("Equity",      f"{info['equity']:,.2f} {cur}",    f"Server: {info.get('server','')}", ACCENT2),
        stat_card("Margin",      f"{info['margin']:,.2f} {cur}",    f"Level: {info.get('margin_level',0):.1f}%"),
        stat_card("Free Margin", f"{info['free_margin']:,.2f} {cur}", "Available"),
        stat_card("Open P&L",    f"{profit:+,.2f} {cur}",          "Floating", p_col),
        stat_card("Leverage",    f"1:{info.get('leverage',1)}",     "Account leverage", TEXT_SEC),
    ]
    status_style = {"fontFamily": FONT_MONO, "fontSize": "11px",
                    "color": ACCENT, "letterSpacing": "0.08em", "marginLeft": "20px"}
    return cards, f"● LIVE  #{info.get('login','')}", status_style


@callback(
    Output("pnl-chart", "figure"),
    Output("dd-chart", "figure"),
    Input("interval", "n_intervals"),
    Input("history-days", "value"),
)
def update_pnl_chart(_, days):
    if not MT5_AVAILABLE or mt5c is None:
        return empty_fig("Connect MT5 to see P&L"), empty_fig("Connect MT5 to see drawdown")

    df = mt5c.get_trade_history(days=days)
    if df.empty:
        return empty_fig("No closed trades in range"), empty_fig("No data")

    # Equity curve
    fig_pnl = go.Figure()
    fig_pnl.add_trace(go.Scatter(
        x=df["time"], y=df["cumulative_profit"],
        fill="tozeroy",
        line=dict(color=ACCENT, width=2),
        fillcolor=ACCENT + "22",
        name="Cumulative P&L",
        hovertemplate="%{x}<br>P&L: %{y:+,.2f}<extra></extra>",
    ))
    fig_pnl.update_layout(**chart_layout(260))

    # Drawdown
    df["running_max"]  = df["cumulative_profit"].cummax()
    df["drawdown"]     = df["cumulative_profit"] - df["running_max"]
    fig_dd = go.Figure()
    fig_dd.add_trace(go.Scatter(
        x=df["time"], y=df["drawdown"],
        fill="tozeroy",
        line=dict(color=RED, width=1.5),
        fillcolor=RED + "22",
        name="Drawdown",
        hovertemplate="%{x}<br>DD: %{y:+,.2f}<extra></extra>",
    ))
    fig_dd.update_layout(**chart_layout(260))

    return fig_pnl, fig_dd


@callback(
    Output("price-chart", "figure"),
    Input("symbol-select", "value"),
    Input("interval", "n_intervals"),
)
def update_price_chart(symbol, _):
    if not MT5_AVAILABLE or mt5c is None:
        return empty_fig("Connect MT5")

    return empty_fig(f"{symbol} — candlestick requires direct MT5 connection")


@callback(
    Output("watchlist-panel", "children"),
    Input("interval", "n_intervals"),
)
def update_watchlist(_):
    if not MT5_AVAILABLE or mt5c is None:
        return html.P("Not connected", style={"color": TEXT_SEC, "fontFamily": FONT_MONO,
                                               "fontSize": "12px"})
    prices = mt5c.get_symbol_prices(WATCHLIST)
    rows = []
    for p in prices:
        rows.append(html.Div([
            html.Span(p["symbol"], style={
                "fontFamily": FONT_MONO, "fontSize": "12px",
                "fontWeight": "600", "color": TEXT_PRI,
            }),
            html.Div([
                html.Span(f"{p['bid']:.5g}", style={"color": RED,   "fontSize": "12px",
                                                     "fontFamily": FONT_MONO}),
                html.Span(" / ", style={"color": TEXT_SEC, "fontSize": "11px"}),
                html.Span(f"{p['ask']:.5g}", style={"color": GREEN, "fontSize": "12px",
                                                     "fontFamily": FONT_MONO}),
            ]),
            html.Span(f"spd {p['spread']}", style={
                "color": TEXT_SEC, "fontSize": "10px", "fontFamily": FONT_MONO,
            }),
        ], style={
            "display": "flex", "justifyContent": "space-between", "alignItems": "center",
            "padding": "8px 10px",
            "borderBottom": f"1px solid {BORDER}",
            "transition": "background 0.15s",
        }))
    return rows


@callback(
    Output("positions-table", "children"),
    Input("interval", "n_intervals"),
)
def update_positions(_):
    if not MT5_AVAILABLE or mt5c is None:
        return html.P("Not connected", style={"color": TEXT_SEC, "fontFamily": FONT_MONO,
                                               "fontSize": "12px"})
    df = mt5c.get_open_positions()
    if df.empty:
        return html.P("No open positions", style={"color": TEXT_SEC, "fontFamily": FONT_MONO,
                                                   "fontSize": "12px"})
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=[{"name": c.upper(), "id": c} for c in df.columns],
        style_table={"overflowX": "auto"},
        style_header=TABLE_HEADER,
        style_cell=TABLE_CELL,
        style_data_conditional=[
            {"if": {"filter_query": "{type} = BUY",    "column_id": "type"},
             "color": GREEN},
            {"if": {"filter_query": "{type} = SELL",   "column_id": "type"},
             "color": RED},
            {"if": {"filter_query": "{profit} > 0",    "column_id": "profit"},
             "color": GREEN},
            {"if": {"filter_query": "{profit} < 0",    "column_id": "profit"},
             "color": RED},
        ],
        page_size=10,
        sort_action="native",
    )


@callback(
    Output("history-table", "children"),
    Input("history-days", "value"),
    Input("interval", "n_intervals"),
)
def update_history(days, _):
    if not MT5_AVAILABLE or mt5c is None:
        return html.P("Not connected", style={"color": TEXT_SEC, "fontFamily": FONT_MONO,
                                               "fontSize": "12px"})
    df = mt5c.get_trade_history(days=days)
    if df.empty:
        return html.P("No trade history in this range", style={"color": TEXT_SEC,
                                                                "fontFamily": FONT_MONO,
                                                                "fontSize": "12px"})
    display_cols = ["time", "symbol", "type", "volume", "price", "profit",
                    "swap", "commission", "cumulative_profit"]
    df_show = df[[c for c in display_cols if c in df.columns]].copy()
    df_show["time"] = df_show["time"].astype(str)

    return dash_table.DataTable(
        data=df_show.to_dict("records"),
        columns=[{"name": c.upper().replace("_", " "), "id": c} for c in df_show.columns],
        style_table={"overflowX": "auto"},
        style_header=TABLE_HEADER,
        style_cell=TABLE_CELL,
        style_data_conditional=[
            {"if": {"filter_query": "{type} = BUY",  "column_id": "type"}, "color": GREEN},
            {"if": {"filter_query": "{type} = SELL", "column_id": "type"}, "color": RED},
            {"if": {"filter_query": "{profit} > 0",  "column_id": "profit"}, "color": GREEN},
            {"if": {"filter_query": "{profit} < 0",  "column_id": "profit"}, "color": RED},
            {"if": {"filter_query": "{cumulative_profit} > 0", "column_id": "cumulative_profit"},
             "color": GREEN},
            {"if": {"filter_query": "{cumulative_profit} < 0", "column_id": "cumulative_profit"},
             "color": RED},
        ],
        page_size=15,
        sort_action="native",
        filter_action="native",
    )


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  ◈ OMNI Trading Dashboard")
    print(f"  MT5 Connected: {MT5_AVAILABLE}")
    print("  → http://localhost:8050\n")
    app.run(debug=True, host="0.0.0.0", port=8050)
