import pandas as pd, numpy as np
df=pd.read_pickle('dk.pkl'); rng=np.random.default_rng(1)
def ols(a,f):
    X=np.c_[np.ones(len(a)),a[f].to_numpy(float)]; y=a.actual.to_numpy(float)
    b=np.linalg.lstsq(X,y,rcond=None)[0]; r=y-X@b
    se=np.sqrt(np.diag(np.linalg.inv(X.T@X))*r.var(ddof=X.shape[1])); return b,se
for pos,f in (('DST',['proj','opp']),('K',['proj','dome']),('K',['proj','own'])):
    for y in (2024,2025,'both'):
        a=df[(df.pos==pos)&((df.season==y) if y!='both' else True)]
        b,se=ols(a,f); print(pos,y,' '.join(f"{n}={v:+.3f}±{s:.3f}" for n,v,s in zip(['const']+f,b,se)))
# adjustment on top of ESPN (slope on the residual), pooled, and gain in top-3 picks with bootstrap over weeks
for pos,feat in (('DST','opp'),('K','dome')):
    a=df[df.pos==pos].copy()
    for trn,tst in ((2024,2025),(2025,2024)):
        tr,te=a[a.season==trn],a[a.season==tst].copy()
        c=feat+'_c'; tr=tr.assign(**{c:tr[feat]-tr.groupby('week')[feat].transform('mean')}); te[c]=te[feat]-te.groupby('week')[feat].transform('mean')
        k=np.linalg.lstsq(tr[[c]].to_numpy(float),(tr.actual-tr.proj).to_numpy(float),rcond=None)[0][0]
        te['m']=te.proj+k*te[c]
        wk=[(w.nlargest(3,'m').actual.mean()-w.nlargest(3,'proj').actual.mean(), w.nlargest(1,'m').actual.mean()-w.nlargest(1,'proj').actual.mean(), (w.nlargest(3,'m').id.tolist()!=w.nlargest(3,'proj').id.tolist())) for _,w in te.groupby('week')]
        d3=np.array([x[0] for x in wk]); d1=np.array([x[1] for x in wk])
        bs=[rng.choice(d3,len(d3)).mean() for _ in range(4000)]
        print(f"{pos} {trn}->{tst}: slope {k:+.3f} per unit; top-3 picks gain {d3.mean():+.2f} pts (90% range {np.percentile(bs,5):+.2f} to {np.percentile(bs,95):+.2f}); top-1 gain {d1.mean():+.2f}; picks differ in {sum(x[2] for x in wk)}/{len(wk)} weeks; MAE {np.abs(te.proj-te.actual).mean():.3f}->{np.abs(te.m-te.actual).mean():.3f}")
    a[feat+'_c']=a[feat]-a.groupby(['season','week'])[feat].transform('mean')
    print(pos,'POOLED slope',round(float(np.linalg.lstsq(a[[feat+'_c']].to_numpy(float),(a.actual-a.proj).to_numpy(float),rcond=None)[0][0]),3), '| feature spread (sd)',round(a[feat+'_c'].std(),2))
# DST: how much is matchup worth in plain terms
a=df[df.pos=='DST']
a=a.assign(bucket=pd.cut(a.opp,[0,17,20,23,26,40],labels=['under 17','17-20','20-23','23-26','26+']))
print(a.groupby('bucket',observed=True).agg(n=('actual','size'),espn_proj=('proj','mean'),actual=('actual','mean')).round(2))
k=df[df.pos=='K']; print(k.groupby('dome').agg(n=('actual','size'),espn_proj=('proj','mean'),actual=('actual','mean')).round(2))
print(k.groupby(['season','dome']).agg(n=('actual','size'),espn_proj=('proj','mean'),actual=('actual','mean')).round(2))
