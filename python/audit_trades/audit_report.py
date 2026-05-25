import pandas as pd, json, glob, os
print('=== AUDIT: TOP 5 WINNING SEEDS (TRADE-BY-TRADE) ===\n')
for f in sorted(glob.glob('seed_*_trades.csv')):
    df = pd.read_csv(f)
    seed = f.split('_')[1]
    ret = round(df['pnl_net'].sum() / 100 * 100, 1)
    wins = df[df.pnl_net > 0]
    loss = df[df.pnl_net <= 0]
    pf = abs(wins.pnl_net.sum()/loss.pnl_net.sum()) if len(loss) > 0 else 999
    print(f'--- SEED {seed} | {len(df)} trades | +{df.pnl_net.sum():.2f} net | WR {len(wins)/len(df)*100:.1f}% | PF {pf:.2f} ---')
    
    print('Confluence count distribution (wins vs losses):')
    for cc in sorted(df.confluence_count.unique()):
        w = len(df[(df.confluence_count==cc) & (df.pnl_net>0)])
        l = len(df[(df.confluence_count==cc) & (df.pnl_net<=0)])
        if w+l > 0:
            print(f'  {int(cc)} confluences: {w}W / {l}L = {w/(w+l)*100:.0f}% WR')
    
    print('Manipulation type (wins vs losses):')
    for mt in df.manipulation_type.unique():
        if not mt or pd.isna(mt): continue
        w = len(df[(df.manipulation_type==mt) & (df.pnl_net>0)])
        l = len(df[(df.manipulation_type==mt) & (df.pnl_net<=0)])
        if w+l > 0:
            print(f'  {mt}: {w}W / {l}L = {w/(w+l)*100:.0f}% WR')
    
    print('First 15 trade sequence [entry_price -> exit_price | reason | net_pnl]:')
    for _, r in df.head(15).iterrows():
        arrow = 'UP' if r.pnl_net > 0 else 'DN'
        print(f'  {arrow} {r.direction} {r.entry_price:.2f} -> {r.exit_price:.2f} | {r.exit_reason} | ${r.pnl_net:.2f} | conf={r.confidence:.2f} | confl={int(r.confluence_count)} | manip={r.manipulation_type}')
    
    print('Equity at trade closes (compounding path):')
    equity = 100.0
    eq_pts = [100.0]
    for _, r in df.iterrows():
        equity += r.pnl_net
        eq_pts.append(equity)
    for i, eq in enumerate(eq_pts):
        if i == 0 or eq >= max(eq_pts[:i+1]) * 0.95 or eq <= min(eq_pts[:i+1]) * 1.05 or i == len(eq_pts)-1:
            if i > 0:
                t = df.iloc[i-1]
                print(f'  Trade {i}: equity={eq:.2f} ({"+" if t.pnl_net>0 else ""}{t.pnl_net:.2f} {t.exit_reason})')
    print()
