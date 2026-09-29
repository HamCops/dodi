import pandas as pd, numpy as np, json
COLS=['player_id','position','season','week','season_type','fantasy_points','fantasy_points_ppr','carries','targets','receiving_air_yards','attempts','passing_air_yards']
st=pd.concat([pd.read_csv(f'stats_player_week_{y}.csv',low_memory=False,usecols=COLS) for y in (2024,2025)])
st=st[(st.season_type=='REG')&st.position.isin(['RB','WR','TE'])&(st.week<=17)].fillna(0)
st['half']=(st.fantasy_points+st.fantasy_points_ppr)/2
OPP={'RB':['carries','targets','receiving_air_yards'],'WR':['targets','receiving_air_yards'],'TE':['targets','receiving_air_yards']}
def fit_xfp(train,y):
    return {pos:np.linalg.lstsq(np.c_[np.ones((train.position==pos).sum()),train[train.position==pos][f].to_numpy(float)],train[train.position==pos][y].to_numpy(float),rcond=None)[0] for pos,f in OPP.items()}
def add(df,coef):
    df=df.copy(); df['xfp']=0.0
    for pos,f in OPP.items():
        m=df.position==pos; df.loc[m,'xfp']=np.c_[np.ones(m.sum()),df.loc[m,f].to_numpy(float)]@coef[pos]
    return df
def table(df,cut,y):
    a=df[df.week<=cut]; b=df[df.week>cut]
    f=a.groupby(['player_id','position']).agg(g=('week','size'),ppg=(y,'mean'),xppg=('xfp','mean')).reset_index()
    r=b.groupby('player_id').agg(ros=(y,'mean'),rg=('week','size')).reset_index()
    t=f.merge(r,on='player_id'); t=t[(t.g>=2)&(t.rg>=4)]; t['cut']=cut; t['luck']=t.ppg-t.xppg; return t
y='fantasy_points'
print('simple blend (PPG + usage xPPG only), and luck by games played')
for trn,tst in ((2024,2025),(2025,2024)):
    coef=fit_xfp(st[st.season==trn],y)
    T=pd.concat([table(add(st[st.season==tst],coef),c,y) for c in range(3,11)]); Tr=pd.concat([table(add(st[st.season==trn],coef),c,y) for c in range(3,11)])
    for pos in OPP:
        g,h=T[T.position==pos],Tr[Tr.position==pos]; X=lambda d,c: np.c_[np.ones(len(d)),d[c].to_numpy(float)]
        b1=np.linalg.lstsq(X(h,['ppg']),h.ros,rcond=None)[0]; b2=np.linalg.lstsq(X(h,['ppg','xppg']),h.ros,rcond=None)[0]
        m1=np.abs(X(g,['ppg'])@b1-g.ros).mean(); m2=np.abs(X(g,['ppg','xppg'])@b2-g.ros).mean()
        print(f"  {trn}->{tst} {pos} MAE ppg-only {m1:.3f}  blend {m2:.3f} ({(m1-m2)/m1*100:+.1f}%)  weights ppg {b2[1]:.2f} xppg {b2[2]:.2f}")
    for games in (3,4,5):
        q=T[(T.g>=games)&(T.cut==T.g.clip(upper=10))&(T.xppg>=6)] if False else T[(T.g>=games)&(T.xppg>=6)]
        for thr in (2,3):
            u,l=q[q.luck<=-thr],q[q.luck>=thr]
            print(f"  {trn}->{tst} games>={games} threshold {thr}: unlucky n={len(u):3} {u.ppg.mean():.2f}->{u.ros.mean():.2f} ({(u.ros-u.ppg).mean():+.2f}) | lucky n={len(l):3} {l.ppg.mean():.2f}->{l.ros.mean():.2f} ({(l.ros-l.ppg).mean():+.2f})")
    # the trade question: swap a lucky player for an unlucky one with the SAME points so far
    q=T[(T.g>=3)&(T.xppg>=6)]
    lucky=q[q.luck>=2]; unl=q[q.luck<=-2]
    d=[]
    for _,a in lucky.iterrows():
        m=unl[(unl.cut==a.cut)&(unl.position==a.position)&((unl.ppg-a.ppg).abs()<=1.5)]
        d+=list(m.ros-a.ros)
    if d: print(f"  {trn}->{tst} TRADE a lucky player for an unlucky one scoring about the same so far: n={len(d)} pairs, gain afterwards {np.mean(d):+.2f} PPG, right {100*np.mean(np.array(d)>0):.0f}%")
print('\nPOOLED COEFFICIENTS (both seasons)')
out={}
for name,y in (('standard','fantasy_points'),('half','half'),('ppr','fantasy_points_ppr')):
    coef=fit_xfp(st,y); T=pd.concat([table(add(st,coef),c,y) for c in range(3,11)])
    out[name]={'xfp':{p:[round(float(v),4) for v in c] for p,c in coef.items()},'blend':{}}
    for pos in OPP:
        h=T[T.position==pos]; b=np.linalg.lstsq(np.c_[np.ones(len(h)),h[['ppg','xppg']].to_numpy(float)],h.ros,rcond=None)[0]
        out[name]['blend'][pos]=[round(float(v),3) for v in b]
print(json.dumps(out))
json.dump(out,open('usage_coef.json','w'),indent=1)
