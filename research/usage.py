"""Which number, known at week w, best predicts what a player scores from then on?"""
import pandas as pd, numpy as np
from scipy.stats import spearmanr
COLS=['player_id','player_display_name','position','season','week','season_type','team','fantasy_points','carries','targets','receptions','rushing_yards','receiving_yards','receiving_air_yards','target_share','air_yards_share','rushing_tds','receiving_tds','rushing_first_downs','receiving_first_downs','attempts','passing_yards','passing_tds','passing_air_yards']
st=pd.concat([pd.read_csv(f'stats_player_week_{y}.csv',low_memory=False,usecols=COLS) for y in (2024,2025)])
st=st[(st.season_type=='REG')&st.position.isin(['QB','RB','WR','TE'])&(st.week<=17)].fillna(0)
pl=pd.read_csv('players.csv',usecols=['gsis_id','pfr_id'],dtype=str)
sn=pd.concat([pd.read_csv(f'snap_counts_{y}.csv') for y in (2024,2025)])
sn=sn[sn.game_type=='REG'][['season','week','pfr_player_id','offense_pct']].merge(pl,left_on='pfr_player_id',right_on='pfr_id')
st=st.merge(sn[['gsis_id','season','week','offense_pct']],left_on=['player_id','season','week'],right_on=['gsis_id','season','week'],how='left')
st['offense_pct']=st.offense_pct.fillna(0)
st['td']=st.rushing_tds+st.receiving_tds
st['yards']=st.rushing_yards+st.receiving_yards
# expected points from opportunity, fit per position at the game level (no TDs, no yards: volume only)
OPP={'RB':['carries','targets','receiving_air_yards'],'WR':['targets','receiving_air_yards'],'TE':['targets','receiving_air_yards'],'QB':['attempts','carries','passing_air_yards']}
def fit_xfp(train):
    out={}
    for pos,f in OPP.items():
        g=train[train.position==pos]; X=np.c_[np.ones(len(g)),g[f].to_numpy(float)]
        out[pos]=np.linalg.lstsq(X,g.fantasy_points.to_numpy(float),rcond=None)[0]
    return out
def add_xfp(df,coef):
    df=df.copy(); df['xfp']=0.0
    for pos,f in OPP.items():
        m=df.position==pos; df.loc[m,'xfp']=np.c_[np.ones(m.sum()),df.loc[m,f].to_numpy(float)]@coef[pos]
    return df
def table(df,cut):
    a=df[df.week<=cut]; b=df[df.week>cut]
    L3=a[a.week>cut-3]
    f=a.groupby(['player_id','position']).agg(g=('week','size'),ppg=('fantasy_points','mean'),xppg=('xfp','mean'),snap=('offense_pct','mean'),tgt=('targets','mean'),car=('carries','mean'),tshare=('target_share','mean'),td=('td','mean')).reset_index()
    l=L3.groupby('player_id').agg(ppg3=('fantasy_points','mean'),xppg3=('xfp','mean'),snap3=('offense_pct','mean'),g3=('week','size')).reset_index()
    last=a[a.week==a.groupby('player_id').week.transform('max')][['player_id','offense_pct','xfp']].rename(columns={'offense_pct':'snap_last','xfp':'xfp_last'})
    r=b.groupby('player_id').agg(ros=('fantasy_points','mean'),rg=('week','size')).reset_index()
    t=f.merge(l,on='player_id',how='left').merge(last,on='player_id',how='left').merge(r,on='player_id')
    t=t[(t.g>=2)&(t.rg>=4)].fillna(0); t['cut']=cut
    t['snap_jump']=t.snap3-t.snap; t['luck']=t.ppg-t.xppg
    return t
res={}
for trn,tst in ((2024,2025),(2025,2024)):
    coef=fit_xfp(st[st.season==trn])
    T=pd.concat([table(add_xfp(st[st.season==tst],coef),c) for c in (3,4,5,6,7,8)])
    Tr=pd.concat([table(add_xfp(st[st.season==trn],coef),c) for c in (3,4,5,6,7,8)])
    print(f'\n===== fit {trn}, score {tst}:  correlation with rest-of-season points per game =====')
    print(f" {'pos':3} {'n':>5} | {'PPG so far':>10} {'last-3 PPG':>10} {'usage xPPG':>10} {'xPPG last3':>10} {'snap %':>7} | blend(PPG+usage) MAE vs PPG-only MAE")
    for pos in ('RB','WR','TE','QB'):
        g=T[T.position==pos]; h=Tr[Tr.position==pos]
        c=lambda col: np.corrcoef(g[col],g.ros)[0,1]
        X=lambda d,cols: np.c_[np.ones(len(d)),d[cols].to_numpy(float)]
        b1=np.linalg.lstsq(X(h,['ppg']),h.ros,rcond=None)[0]; b2=np.linalg.lstsq(X(h,['ppg','xppg','xppg3','snap3']),h.ros,rcond=None)[0]
        m1=np.abs(X(g,['ppg'])@b1-g.ros).mean(); m2=np.abs(X(g,['ppg','xppg','xppg3','snap3'])@b2-g.ros).mean()
        print(f" {pos:3} {len(g):5} | {c('ppg'):10.3f} {c('ppg3'):10.3f} {c('xppg'):10.3f} {c('xppg3'):10.3f} {c('snap'):7.3f} | {m2:.3f} vs {m1:.3f}  ({(m1-m2)/m1*100:+.1f}%)   coef {np.round(b2,2)}")
    # the waiver question: among players scoring little so far, who breaks out?
    print('  WAIVER POOL (PPG so far under 7, RB/WR/TE): pick the top 10% by each signal; what do they score afterwards?')
    w=T[(T.ppg<7)&T.position.isin(['RB','WR','TE'])]
    print(f"   pool n={len(w)}, average afterwards {w.ros.mean():.2f}")
    for col in ('ppg','ppg3','xppg','xppg3','snap3','snap_last','snap_jump','tgt','xfp_last'):
        top=w[w[col]>=w.groupby('cut')[col].transform(lambda s:s.quantile(.9))]
        print(f"   top by {col:10}: afterwards {top.ros.mean():5.2f}  (n={len(top)}, {100*(top.ros>=8).mean():.0f}% became startable 8+ PPG)")
    # luck: does over/under-performing usage regress?
    print('  LUCK (PPG minus usage xPPG), players with 4+ games so far:')
    q=T[(T.g>=4)&T.position.isin(['RB','WR','TE'])&(T.xppg>=6)].copy()
    q['chg']=q.ros-q.ppg
    for lab,m in (('unlucky: scoring 3+ below usage',q.luck<=-3),('in line',q.luck.abs()<1.5),('lucky: scoring 3+ above usage',q.luck>=3)):
        s=q[m]; print(f"   {lab:32} n={len(s):4}  PPG so far {s.ppg.mean():5.2f} -> afterwards {s.ros.mean():5.2f}  (change {s.chg.mean():+.2f})   usage said {s.xppg.mean():.2f}")
