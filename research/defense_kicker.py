import json, pandas as pd, numpy as np
TEAM={1:'ATL',2:'BUF',3:'CHI',4:'CIN',5:'CLE',6:'DAL',7:'DEN',8:'DET',9:'GB',10:'TEN',11:'IND',12:'KC',13:'LV',14:'LA',15:'MIA',16:'MIN',17:'NE',18:'NO',19:'NYG',20:'NYJ',21:'PHI',22:'ARI',23:'PIT',24:'LAC',25:'SF',26:'SEA',27:'TB',28:'WAS',29:'CAR',30:'JAX',33:'BAL',34:'HOU'}
rows=[]
for y in (2024,2025):
    act={}
    for x in json.load(open(f'espn_actuals_{y}.json'))['players']:
        p=x['player']
        for s in p.get('stats',[]):
            if s['seasonId']==y and s['statSourceId']==0 and s['statSplitTypeId']==1: act[(p['id'],s['scoringPeriodId'])]=s.get('appliedTotal',0.0)
    for w in range(1,18):
        for x in json.load(open(f'dk_{y}_{w}.json')).get('players',[]):
            p=x['player']; pos={5:'K',16:'DST'}.get(p.get('defaultPositionId'))
            if not pos: continue
            for s in p.get('stats',[]):
                if s['seasonId']==y and s['scoringPeriodId']==w and s['statSourceId']==1 and s['statSplitTypeId']==1 and (p['id'],w) in act:
                    rows.append(dict(season=y,week=w,id=p['id'],name=p['fullName'],pos=pos,team=TEAM.get(p.get('proTeamId')),proj=s.get('appliedTotal',0.0),actual=act[(p['id'],w)]))
df=pd.DataFrame(rows); df=df[df.proj>0]
g=pd.read_csv('games.csv'); g=g[g.season.isin([2024,2025])&(g.game_type=='REG')]
h=g.assign(team=g.home_team,own=(g.total_line+g.spread_line)/2,opp=(g.total_line-g.spread_line)/2,fav=g.spread_line,home=1)
a=g.assign(team=g.away_team,own=(g.total_line-g.spread_line)/2,opp=(g.total_line+g.spread_line)/2,fav=-g.spread_line,home=0)
gg=pd.concat([h,a])[['season','week','team','own','opp','fav','home','roof','wind','temp','total_line']]
df=df.merge(gg,on=['season','week','team'],how='inner')
df['outdoor']=df.roof.isin(['outdoors','open']).astype(int); df['windy']=((df.wind.fillna(0)>=15)&(df.outdoor==1)).astype(int); df['dome']=1-df.outdoor
df.to_pickle('dk.pkl')
print(df.groupby(['season','pos']).agg(n=('proj','size'),teams=('id','nunique'),proj=('proj','mean'),actual=('actual','mean')).round(2))
def fit(a,f): return np.linalg.lstsq(np.c_[np.ones(len(a)),a[f].to_numpy(float)],a.actual.to_numpy(float),rcond=None)[0]
def pr(b,d,f): return np.c_[np.ones(len(d)),d[f].to_numpy(float)]@b
def picks(d,col,n):
    """each week take the top n by col; what do they actually score"""
    return np.mean([w.nlargest(n,col).actual.mean() for _,w in d.groupby('week')])
def stream(d,col,skip,n=1):
    """the streaming case: the best `skip` by ESPN projection are owned by someone; pick from the rest"""
    out=[]
    for _,w in d.groupby('week'):
        rest=w.sort_values('proj',ascending=False).iloc[skip:]
        out.append(rest.nlargest(n,col).actual.mean())
    return np.mean(out)
MODELS={'DST':{'espn':['proj'],'opp total only':['opp'],'opp total + favored':['opp','fav'],'espn + opp total':['proj','opp'],'espn + opp + favored':['proj','opp','fav'],'espn + opp + fav + home':['proj','opp','fav','home']},
        'K':{'espn':['proj'],'own total only':['own'],'espn + own total':['proj','own'],'espn + own + dome':['proj','own','dome'],'espn + own + dome + windy':['proj','own','dome','windy'],'own + dome + fav':['own','dome','fav']}}
for pos in ('DST','K'):
    for trn,tst in ((2024,2025),(2025,2024)):
        tr,te=df[(df.pos==pos)&(df.season==trn)],df[(df.pos==pos)&(df.season==tst)].copy()
        print(f'\n{pos}: fit {trn}, score {tst}   (n={len(te)}, corr proj/actual {np.corrcoef(te.proj,te.actual)[0,1]:.3f}; random pick scores {te.actual.mean():.2f})')
        print(f"  {'model':26} {'corr':>6} {'MAE':>6} | {'best 1':>7} {'best 3':>7} | streaming (top 10 owned gone): {'best 1':>7} {'best 2':>7}")
        for name,f in MODELS[pos].items():
            b=fit(tr,f); te['m']=pr(b,te,f)
            print(f"  {name:26} {np.corrcoef(te.m,te.actual)[0,1]:6.3f} {np.abs(te.m-te.actual).mean():6.2f} | {picks(te,'m',1):7.2f} {picks(te,'m',3):7.2f} | {'':31} {stream(te,'m',10,1):7.2f} {stream(te,'m',10,2):7.2f}")
    print(pos,'pooled coefficients', {n:np.round(fit(df[df.pos==pos],f),3).tolist() for n,f in MODELS[pos].items() if n!='espn'})
