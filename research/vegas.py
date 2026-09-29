import pandas as pd, numpy as np, itertools, json
df=pd.read_pickle('bt.pkl'); df=df[(df.proj>=4)&df.implied.notna()].copy()
df['outdoor']=df.roof.isin(['outdoors','open']).astype(int)
df['windy']=((df.wind.fillna(0)>=15)&(df.outdoor==1)).astype(float)
df['imp_c']=df.implied-df.groupby(['season','week']).implied.transform('mean')
def pairs(g,col,width=3.0,cross=False):
    right=tot=0; gain=0.0
    for _,w in g.groupby(['week'] if cross else ['week','pos']):
        if cross: w=w[w.pos.isin(['RB','WR','TE'])]
        a=w[['proj','actual',col]].to_numpy()
        for i,j in itertools.combinations(range(len(a)),2):
            if abs(a[i,0]-a[j,0])>width or a[i,1]==a[j,1] or a[i,2]==a[j,2]: continue
            pick=i if a[i,2]>a[j,2] else j; other=j if pick==i else i
            tot+=1; right+=a[pick,1]>a[other,1]; gain+=a[pick,1]-a[other,1]
    return right/tot, gain/tot, tot
def adj_fit(a,feats):
    # adjustment on top of ESPN: actual - proj ~ feats (no rescaling of proj, so cross-position order is kept honest)
    X=np.c_[np.ones(len(a)),a[feats].to_numpy(float)]; return np.linalg.lstsq(X,(a.actual-a.proj).to_numpy(float),rcond=None)[0]
SPEC={'vegas':{'QB':['imp_c'],'RB':['imp_c'],'WR':['imp_c'],'TE':['imp_c']},
      'vegas+wind':{'QB':['imp_c','windy'],'RB':['imp_c'],'WR':['imp_c','windy'],'TE':['imp_c','windy']}}
for trn,tst in ((2024,2025),(2025,2024)):
    tr,te=df[df.season==trn],df[df.season==tst].copy(); te['m_espn']=te.proj
    for name,spec in SPEC.items():
        te['m_'+name]=te.proj
        for pos,feats in spec.items():
            b=adj_fit(tr[tr.pos==pos],feats); m=te.pos==pos
            te.loc[m,'m_'+name]=te.loc[m,'proj']+te.loc[m,feats].to_numpy(float)@b[1:]   # slope only: drop the constant
    print(f'\nfit {trn} -> score {tst}')
    for c in ('m_espn','m_vegas','m_vegas+wind'):
        a,g,n=pairs(te,c); a2,g2,n2=pairs(te,c,cross=True)
        print(f"  {c[2:]:12} MAE {np.abs(te.actual-te[c]).mean():.3f} | same-position pick {a*100:.2f}% {g:+.3f} pts/decision | FLEX pick {a2*100:.2f}% {g2:+.3f}")
coef={}
for pos,feats in SPEC['vegas+wind'].items():
    b=adj_fit(df[df.pos==pos],feats); coef[pos]={f:round(float(v),3) for f,v in zip(feats,b[1:])}
print('\npooled coefficients (both seasons):',json.dumps(coef))
d=df.assign(adj=0.0)
for pos,c in coef.items():
    m=d.pos==pos; d.loc[m,'adj']=sum(d.loc[m,f]*v for f,v in c.items())
print('size of adjustment (pts): ', d.groupby('pos').adj.describe()[['mean','std','min','max']].round(2).to_dict('index'))
print('implied total range', df.implied.describe()[['min','mean','max']].round(1).to_dict(), 'windy share', df.windy.mean().round(3))
