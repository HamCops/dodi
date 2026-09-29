import json, pandas as pd, numpy as np, itertools
df=pd.read_pickle('bt.pkl'); df=df[(df.proj>=4)&df.implied.notna()].copy()
adp={}
for y in (2024,2025):
    for x in json.load(open(f'espn_{y}_1.json'))['players']:
        o={'averageDraftPosition':((x['player'].get('draftRanksByRankType') or {}).get('STANDARD') or {}).get('rank')}
        if o.get('averageDraftPosition'): adp[(y,str(x['player']['id']))]=o['averageDraftPosition']
df['adp']=[adp.get((y,e),np.nan) for y,e in zip(df.season,df.espn_id)]
df['adp']=df.adp.fillna(300).clip(upper=300)
df['szn_avg']=df.actual_szn
df['imp_c']=df.implied-df.groupby(['season','week']).implied.transform('mean')
df['outdoor']=df.roof.isin(['outdoors','open']); df['windy']=((df.wind.fillna(0)>=15)&df.outdoor)
print('adp coverage',(df.adp<300).mean())
def test(g, width, cond, label, cross=False):
    n=win=0; gain=[]; 
    keys=['season','week'] if cross else ['season','week','pos']
    for _,w in g.groupby(keys):
        if cross: w=w[w.pos.isin(['RB','WR','TE'])]
        a=w[['proj','actual','adp','szn_avg','imp_c','windy']].to_numpy(float)
        for i,j in itertools.combinations(range(len(a)),2):
            lo,hi=(i,j) if a[i,0]<a[j,0] else (j,i)   # lo = lower projection = the benched one
            if a[hi,0]-a[lo,0]>width: continue
            if not cond(a[lo],a[hi]): continue
            n+=1; win+=a[lo,1]>a[hi,1]; gain.append(a[lo,1]-a[hi,1])
    gain=np.array(gain)
    if n==0: print(label,'no cases'); return
    # cluster-ish SE: treat as independent (optimistic)
    print(f" {label:58} n={n:6} lower-proj player wins {100*win/n:5.1f}%  avg swing {gain.mean():+.2f} ± {gain.std()/np.sqrt(n):.2f}")
for y in (2024,2025,None):
    g=df if y is None else df[df.season==y]
    print('\nseason',y or 'both','— start the LOWER-projected player when...')
    test(g,1.5,lambda l,h:True,'(baseline: any pair within 1.5)')
    test(g,1.5,lambda l,h:l[2]<=h[2]-40,'he was ranked 40+ spots higher preseason')
    test(g,1.5,lambda l,h:l[2]<=h[2]/2,'he was preseason rank is half or better')
    test(g,1.5,lambda l,h:l[2]<=h[2]/2 and l[2]<=50,'...and is a top-50 preseason')
    test(g,0.5,lambda l,h:l[2]<=h[2]/2,'half ADP, projections within 0.5')
    test(g,3.0,lambda l,h:l[2]<=h[2]/2,'half ADP, projections within 3.0')
    test(g,1.5,lambda l,h:l[3]>=h[3]+3 ,'his season avg is 3+ pts higher')
    test(g,1.5,lambda l,h:l[4]>=h[4]+4 ,'his team implied total is 4+ higher')
    test(g,1.5,lambda l,h:h[5]==1 and l[5]==0 ,'the starter plays in 15+ mph wind (QB/WR/TE pairs incl RB)')
    test(g,1.5,lambda l,h:l[2]<=h[2]/2,'FLEX (RB/WR/TE mixed): half ADP',cross=True)
