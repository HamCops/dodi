import json, pandas as pd, numpy as np
df=pd.read_pickle('bt.pkl')
rank={}
for y in (2024,2025):
    for x in json.load(open(f'espn_{y}_1.json'))['players']:
        r=((x['player'].get('draftRanksByRankType') or {}).get('STANDARD') or {}).get('rank')
        if r: rank[(y,str(x['player']['id']))]=r
df['rank']=[rank.get((y,e),999) for y,e in zip(df.season,df.espn_id)]
st=pd.concat([pd.read_csv(f'stats_player_week_{y}.csv',low_memory=False,usecols=['player_id','season','week','season_type']) for y in (2024,2025)])
st=st[st.season_type=='REG'].rename(columns={'player_id':'gsis_id'}).sort_values(['gsis_id','season','week'])
st['prev_game']=st.groupby(['gsis_id','season']).week.shift(1)
df=df.merge(st[['gsis_id','season','week','prev_game']],on=['gsis_id','season','week'],how='left')
df['missed']=df.week-df.prev_game-1          # weeks since last game, minus one (bye counts as 1)
df['first_game']=df.prev_game.isna()&(df.week>=3)   # season debut in week 3+: opened on IR/PUP/suspension
df=df[df.proj>=4]
df['prior_proj']=df.groupby(['espn_id','season']).proj.transform(lambda s:s.shift(1).expanding().mean())
def show(label,m):
    g=df[m]; 
    if len(g)<20: print(f' {label:52} n={len(g)} (too few)'); return
    r=g.actual-g.proj
    print(f" {label:52} n={len(g):5} proj {g.proj.mean():5.2f} actual {g.actual.mean():5.2f}  beat proj by {r.mean():+.2f} ± {r.std()/np.sqrt(len(g)):.2f}  ({(r>0).mean()*100:.0f}% beat it)")
for y in (2024,2025,'both'):
    s=(df.season==y) if y!='both' else (df.season>0)
    print('\nseason',y)
    show('everyone',s)
    show('played last week or bye (missed<=1)',s&(df.missed<=1))
    show('back after missing 2+ weeks',s&(df.missed>=2))
    show('back after missing 2+, top-60 preseason',s&(df.missed>=2)&(df['rank']<=60))
    show('season debut in week 3+ (Bowers case)',s&df.first_game)
    show('season debut wk3+, top-100 preseason',s&df.first_game&(df['rank']<=100))
    show('returning (either), projection cut 20%+ vs own norm',s&((df.missed>=2))&(df.proj<=0.8*df.prior_proj))
    show('top-60 preseason, anyone',s&(df['rank']<=60))
