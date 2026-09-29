import pandas as pd, numpy as np, itertools
df=pd.read_pickle('bt.pkl'); df=df[(df.proj>=4)&df.implied.notna()].copy()
df['outdoor']=df.roof.isin(['outdoors','open']).astype(int)
df['wind']=df.wind.fillna(0)*df.outdoor
df['windy']=(df.wind>=15).astype(int)
df['cold']=((df.temp.fillna(60)<=32)&(df.outdoor==1)).astype(int)
df['imp_c']=df.implied-df.groupby(['season','week']).implied.transform('mean')
df['snap_trend']=(df.offense_pct_last-df.offense_pct_szn).fillna(0)
df['tgt_trend']=(df.target_share_last-df.target_share_szn).fillna(0)
df['car_trend']=(df.carries_last-df.carries_szn).fillna(0)
df['resid_l3']=df.resid_l3.fillna(0)
df['form']=(df.actual_l3-df.proj).fillna(0)
tr,te=df[df.season==2024],df[df.season==2025]
print('ESPN baseline: MAE, bias, corr by position (2025)')
for pos,g in te.groupby('pos'): print(f" {pos} n={len(g)} MAE={np.abs(g.resid).mean():.2f} bias={g.resid.mean():+.2f} corr={np.corrcoef(g.proj,g.actual)[0,1]:.3f}")
FEATS=['imp_c','windy','cold','snap_trend','tgt_trend','car_trend','resid_l3','home']
print('\nDoes each signal predict ESPN\'s error? corr(feature, actual-proj), both seasons shown')
for f in FEATS+['form']:
    print(f' {f:11}', ' '.join(f"{pos}:{np.corrcoef(tr[tr.pos==pos][f],tr[tr.pos==pos].resid)[0,1]:+.3f}/{np.corrcoef(te[te.pos==pos][f],te[te.pos==pos].resid)[0,1]:+.3f}" for pos in ('QB','RB','WR','TE')))
def fit(X,y):
    A=np.c_[np.ones(len(X)),X]; return np.linalg.lstsq(A,y,rcond=None)[0]
def pred(b,X): return np.c_[np.ones(len(X)),X]@b
def pairs(g,col,width=3.0):
    """start/sit decisions: same pos, same week, ESPN projections within `width`."""
    right=tot=0; gain=0.0
    for _,w in g.groupby(['week','pos']):
        a=w[['proj','actual',col]].to_numpy()
        for i,j in itertools.combinations(range(len(a)),2):
            if abs(a[i,0]-a[j,0])>width or a[i,1]==a[j,1] or a[i,2]==a[j,2]: continue
            pick=i if a[i,2]>a[j,2] else j; other=j if pick==i else i
            tot+=1; right+=a[pick,1]>a[other,1]; gain+=a[pick,1]-a[other,1]
    return right/tot, gain/tot, tot
print('\nModels fit on 2024, scored on 2025')
res={}
te=te.copy(); te['m_espn']=te.proj
for name,feats in {'espn+vegas':['imp_c'],'espn+weather':['windy','cold'],'espn+usage':['snap_trend','tgt_trend','car_trend'],'espn+recent_error':['resid_l3'],'espn+all':FEATS,'recalibrate_only':[]}.items():
    te['m_'+name]=np.nan
    for pos in ('QB','RB','WR','TE'):
        a,b=tr[tr.pos==pos],te[te.pos==pos]
        beta=fit(a[['proj']+feats].to_numpy(float),a.actual.to_numpy(float))
        te.loc[te.pos==pos,'m_'+name]=pred(beta,b[['proj']+feats].to_numpy(float))
        if name=='espn+all': print('  coef',pos,dict(zip(['const','proj']+feats,np.round(beta,3))))
print(f"\n {'model':20} {'MAE':>6} {'pick%':>7} {'pts/decision':>13}  (decisions = same-position pairs within 3 proj pts)")
for c in [c for c in te.columns if c.startswith('m_')]:
    acc,gain,n=pairs(te,c)
    print(f" {c[2:]:20} {np.abs(te.actual-te[c]).mean():6.3f} {acc*100:6.2f}% {gain:+13.3f}  n={n}")
# reverse split for stability
tr2,te2=df[df.season==2025],df[df.season==2024].copy()
te2['m_espn']=te2.proj; te2['m_all']=np.nan; te2['m_vegas']=np.nan
for pos in ('QB','RB','WR','TE'):
    a=tr2[tr2.pos==pos]
    for nm,feats in (('m_all',FEATS),('m_vegas',['imp_c'])):
        te2.loc[te2.pos==pos,nm]=pred(fit(a[['proj']+feats].to_numpy(float),a.actual.to_numpy(float)),te2[te2.pos==pos][['proj']+feats].to_numpy(float))
print('\nReverse (fit 2025, score 2024):')
for c in ('m_espn','m_vegas','m_all'):
    acc,gain,n=pairs(te2,c); print(f" {c[2:]:20} {np.abs(te2.actual-te2[c]).mean():6.3f} {acc*100:6.2f}% {gain:+13.3f}  n={n}")
# noise floor for pick%: binomial SE
print('\npick% standard error ~', round(100*0.5/np.sqrt(pairs(te,'m_espn')[2]),2),'pts (pairs are not independent, so true error is larger)')
