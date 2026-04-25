"""
ai_dashboard_patch.py — AI Panel additions for dashboard.py
Add these imports and callbacks to your existing dashboard.py.

STEP 1: Add at the top of dashboard.py (after existing imports):
    from ai_engine import OmniAI, load_ai_state, get_regime_color, get_bias_color, get_verdict_color
    omni_ai = OmniAI()
    omni_ai.start()   # background thread — no blocking

STEP 2: Add the AI_PANEL layout block into app.layout (inside the main html.Div content area),
        after your existing stat cards section.

STEP 3: Register the callbacks below with your Dash app instance.
"""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Layout block — paste into app.layout
# ─────────────────────────────────────────────────────────────────────────────

AI_PANEL_LAYOUT = """
# Add this import at top of dashboard.py:
# from dash import html, dcc, Input, Output, callback

html.Div([
    # ── AI Header ─────────────────────────────────────────────────────────
    html.Div([
        html.Span("⬡", style={"color": "#bf5af2", "fontSize": "18px", "marginRight": "8px"}),
        html.Span("AI ENGINE", style={
            "fontFamily": FONT_MONO, "fontWeight": "600",
            "fontSize": "13px", "letterSpacing": "0.12em",
            "color": TEXT_PRI,
        }),
        html.Span(id="ai-status-badge", children="● INITIALIZING", style={
            "marginLeft": "12px", "fontSize": "10px",
            "color": "#bf5af2", "fontFamily": FONT_MONO,
        }),
    ], style={"display": "flex", "alignItems": "center", "marginBottom": "14px"}),

    # ── Regime + Bias cards ────────────────────────────────────────────────
    html.Div(id="ai-regime-cards", style={
        "display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "14px"
    }),

    # ── AI Insight ────────────────────────────────────────────────────────
    html.Div([
        html.P("AI BRIEFING", style={
            "fontSize": "10px", "letterSpacing": "0.1em",
            "color": TEXT_SEC, "fontFamily": FONT_SANS,
            "textTransform": "uppercase", "margin": "0 0 8px",
        }),
        html.Pre(id="ai-insight-text", style={
            "fontFamily": FONT_MONO, "fontSize": "11px",
            "color": TEXT_PRI, "lineHeight": "1.6",
            "whiteSpace": "pre-wrap", "margin": "0",
        }),
    ], style={
        "background": BG_CARD, "border": f"1px solid {BORDER}",
        "borderLeft": "2px solid #bf5af2",
        "borderRadius": "8px", "padding": "14px 16px", "marginBottom": "14px",
    }),

    # ── Adapted Parameters ────────────────────────────────────────────────
    html.Div([
        html.P("LIVE PARAMETERS (AI-ADAPTED)", style={
            "fontSize": "10px", "letterSpacing": "0.1em",
            "color": TEXT_SEC, "fontFamily": FONT_SANS,
            "textTransform": "uppercase", "margin": "0 0 8px",
        }),
        html.Div(id="ai-params-grid", style={
            "display": "grid",
            "gridTemplateColumns": "repeat(auto-fill, minmax(150px, 1fr))",
            "gap": "8px",
        }),
        html.P(id="ai-rationale-text", style={
            "fontSize": "11px", "color": TEXT_SEC,
            "fontFamily": FONT_MONO, "marginTop": "8px",
            "fontStyle": "italic",
        }),
    ], style={
        "background": BG_CARD, "border": f"1px solid {BORDER}",
        "borderRadius": "8px", "padding": "14px 16px", "marginBottom": "14px",
    }),

    # ── Signal log ────────────────────────────────────────────────────────
    html.Div([
        html.P("RECENT AI SIGNALS", style={
            "fontSize": "10px", "letterSpacing": "0.1em",
            "color": TEXT_SEC, "fontFamily": FONT_SANS,
            "textTransform": "uppercase", "margin": "0 0 8px",
        }),
        html.Div(id="ai-signals-list"),
    ], style={
        "background": BG_CARD, "border": f"1px solid {BORDER}",
        "borderRadius": "8px", "padding": "14px 16px",
    }),

], style={
    "background": BG_CARD2, "border": f"1px solid {BORDER}",
    "borderTop": "2px solid #bf5af2",
    "borderRadius": "8px", "padding": "20px", "marginBottom": "16px",
})
"""


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Callbacks — paste into dashboard.py
# ─────────────────────────────────────────────────────────────────────────────

CALLBACKS_CODE = '''
@callback(
    Output("ai-regime-cards",   "children"),
    Output("ai-insight-text",   "children"),
    Output("ai-params-grid",    "children"),
    Output("ai-rationale-text", "children"),
    Output("ai-signals-list",   "children"),
    Output("ai-status-badge",   "children"),
    Input("interval", "n_intervals"),
)
def update_ai_panel(_):
    state = load_ai_state()          # reads ai_state.json written by OmniAI
    if not state:
        empty = [html.P("AI engine starting...",
                        style={"color": TEXT_SEC, "fontFamily": FONT_MONO,
                               "fontSize": "12px"})]
        return empty, "Initializing...", empty, "", empty, "● STARTING"

    regime = state.get("regime", {})
    params = state.get("params", {})
    sigs   = state.get("signals", [])
    insight= state.get("insight", "")

    # Regime cards
    def mini_card(label, val, color):
        return html.Div([
            html.P(label, style={"margin": "0 0 2px", "fontSize": "9px",
                                  "color": TEXT_SEC, "fontFamily": FONT_SANS,
                                  "textTransform": "uppercase", "letterSpacing": "0.08em"}),
            html.Span(val, style={"fontFamily": FONT_MONO, "fontSize": "14px",
                                   "fontWeight": "700", "color": color}),
        ], style={
            "background": BG_CARD, "border": f"1px solid {BORDER}",
            "borderTop": f"2px solid {color}",
            "borderRadius": "6px", "padding": "10px 14px", "minWidth": "110px",
        })

    regime_cards = [
        mini_card("Phase",     regime.get("phase",     "—"), get_regime_color(regime.get("phase",""))),
        mini_card("Bias",      regime.get("bias",      "—"), get_bias_color(regime.get("bias",""))),
        mini_card("AMD Stage", regime.get("amd_stage", "—"), "#ff9500"),
        mini_card("Volatility",regime.get("volatility","—"), "#007aff"),
        mini_card("Session",   regime.get("session",   "—"), "#5a6a7e"),
        mini_card("Confidence",f"{regime.get('confidence',0)}%", "#bf5af2"),
    ]
    if regime.get("killzone_active"):
        regime_cards.append(mini_card("Killzone", "ACTIVE ⚡", "#00e5a0"))
    if regime.get("smt_divergence"):
        regime_cards.append(mini_card("SMT Div", "FORMING ⚠", "#ff9500"))

    # Param grid
    param_items = [
        ("Risk %",    f"{params.get('base_risk_pct',2):.1f}%"),
        ("Min RR",    f"{params.get('min_rr',1.5):.1f}"),
        ("Min Conf",  f"{params.get('min_confidence',50)}%"),
        ("Max Open",  str(params.get("max_open", 3))),
        ("Daily Lim", f"{params.get('daily_loss_limit',6):.1f}%"),
        ("Skip Sess", ", ".join(params.get("skip_session", [])) or "None"),
        ("Priority",  ", ".join(params.get("priority_setups", [])) or "All"),
        ("TP1 Split", f"{int(params.get('tp1_pct', 0.5)*100)}%"),
    ]
    param_grid = [
        html.Div([
            html.P(label, style={"margin": "0 0 2px", "fontSize": "9px",
                                  "textTransform": "uppercase", "letterSpacing": "0.08em",
                                  "color": TEXT_SEC, "fontFamily": FONT_SANS}),
            html.Span(val, style={"fontFamily": FONT_MONO, "fontSize": "13px",
                                   "color": ACCENT, "fontWeight": "600"}),
        ], style={"background": BG_PAGE, "borderRadius": "4px",
                  "padding": "8px 10px", "border": f"1px solid {BORDER}"})
        for label, val in param_items
    ]
    rationale = f"AI: {params.get('ai_rationale', '')[:160]}"

    # Signals list
    signal_rows = []
    for s in reversed(sigs[-8:]):
        verdict = s.get("ai_verdict", "")
        color   = get_verdict_color(verdict)
        signal_rows.append(html.Div([
            html.Span(f"{s.get('symbol','')} {s.get('direction','')}", style={
                "fontFamily": FONT_MONO, "fontSize": "12px",
                "color": GREEN if s.get("direction") == "BUY" else RED,
                "fontWeight": "600", "minWidth": "100px",
            }),
            html.Span(s.get("entry_type",""), style={
                "fontFamily": FONT_MONO, "fontSize": "11px",
                "color": TEXT_SEC, "minWidth": "70px",
            }),
            html.Span(verdict, style={
                "fontFamily": FONT_MONO, "fontSize": "11px",
                "color": color, "fontWeight": "700", "minWidth": "90px",
            }),
            html.Span(f"{s.get('ai_confidence',0)}%", style={
                "fontFamily": FONT_MONO, "fontSize": "11px",
                "color": "#bf5af2", "minWidth": "45px",
            }),
            html.Span(s.get("ai_reasoning","")[:80], style={
                "fontFamily": FONT_MONO, "fontSize": "10px",
                "color": TEXT_SEC, "flex": "1",
            }),
        ], style={
            "display": "flex", "gap": "14px", "alignItems": "center",
            "padding": "7px 0", "borderBottom": f"1px solid {BORDER}",
        }))

    if not signal_rows:
        signal_rows = [html.P("No signals evaluated yet",
                               style={"color": TEXT_SEC, "fontFamily": FONT_MONO,
                                      "fontSize": "12px"})]

    ts = state.get("timestamp", "")[:16].replace("T", " ")
    status_badge = f"● LIVE  updated {ts} UTC"

    return regime_cards, insight, param_grid, rationale, signal_rows, status_badge
'''


if __name__ == "__main__":
    print("ai_dashboard_patch.py — Integration instructions:")
    print()
    print("1. Copy ai_engine.py into your project folder (same level as dashboard.py)")
    print()
    print("2. Add to dashboard.py imports:")
    print("   from ai_engine import OmniAI, load_ai_state, get_regime_color, get_bias_color, get_verdict_color")
    print("   omni_ai = OmniAI()")
    print("   omni_ai.start()")
    print()
    print("3. Add AI_PANEL layout block into app.layout")
    print()
    print("4. Register the callback (copy from CALLBACKS_CODE above)")
    print()
    print("5. Set env var:  export ANTHROPIC_API_KEY=sk-ant-...")
    print()
    print("6. Run:  python dashboard.py")
    print("   The AI panel will appear at the top of the dashboard.")
    print("   Regime refreshes every 5 min, params every 60 min.")
