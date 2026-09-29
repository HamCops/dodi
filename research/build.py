import json, pandas as pd, numpy as np
POS={1:'QB',2:'RB',3:'WR',4:'TE'}
rows=[]
for y in (2024,2025):
    for w in range(1,18):
        for x in json.load(open(f'espn_{y}_{w}.json')).get('players',[]):
            p=x['player']; pos=POS.get(p.get('defaultPositionId'))
            if not pos: continue
            for s in p.get('stats',[]):
                if s['seasonId']==y and s['scoringPeriodId']==w and s['statSourceId']==1 and s['statSplitTypeId']==1:
                    rows.append(dict(season=y,week=w,espn_id=str(p['id']),name=p['fullName'],pos=pos,proj=s.get('appliedTotal',0.0)))
proj=pd.DataFrame(rows); print('proj rows',len(proj))
pl=pd.read_csv('players.csv',usecols=['gsis_id','espn_id','pfr_id'],dtype=str).dropna(subset=['espn_id'])
pl['espn_id']=pl['espn_id'].str.replace(r'\.0$','',regex=True)
st=pd.concat([pd.read_csv(f'stats_player_week_{y}.csv',low_memory=False) for y in (2024,2025)])
st=st[st.season_type=='REG'][['player_id','season','week','team','opponent_team','fantasy_points','targets','carries','target_share']].rename(columns={'player_id':'gsis_id','fantasy_points':'actual'})
sn=pd.concat([pd.read_csv(f'snap_counts_{y}.csv') for y in (2024,2025)])
sn=sn[sn.game_type=='REG'][['season','week','pfr_player_id','offense_pct']].rename(columns={'pfr_player_id':'pfr_id'})
df=proj.merge(pl,on='espn_id',how='left')
print('matched gsis',df.gsis_id.notna().mean())
df=df.merge(st,on=['gsis_id','season','week'],how='left').merge(sn,on=['pfr_id','season','week'],how='left')
# played = has a stat row or snaps; did-not-play => actual 0
df['played']=df.actual.notna()|(df.offense_pct.fillna(0)>0)
df['actual']=df.actual.fillna(0.0)
g=pd.read_csv('games.csv'); g=g[(g.season.isin([2024,2025]))&(g.game_type=='REG')]
h=g.assign(team=g.home_team,implied=(g.total_line+g.spread_line)/2,home=1)
a=g.assign(team=g.away_team,implied=(g.total_line-g.spread_line)/2,home=0)
gg=pd.concat([h,a])[['season','week','team','implied','total_line','spread_line','roof','temp','wind','home']]
gg['team']=gg.team.replace({'LA':'LA'})
# team for a projected player who did not play: take his most recent team
df=df.sort_values(['espn_id','season','week'])
df['team']=df.groupby(['espn_id','season']).team.transform(lambda s:s.ffill().bfill())
df=df.merge(gg,on=['season','week','team'],how='left')
print('with game',df.implied.notna().mean())
# usage history strictly before the week
hist=st.merge(sn.merge(pl[['gsis_id','pfr_id']],on='pfr_id'),on=['gsis_id','season','week'],how='outer').sort_values(['gsis_id','season','week'])
for c in ('actual','target_share','carries','offense_pct','targets'):
    grp=hist.groupby(['gsis_id','season'])[c]
    hist[c+'_l3']=grp.transform(lambda s:s.shift(1).rolling(3,min_periods=1).mean())
    hist[c+'_last']=grp.transform(lambda s:s.shift(1))
    hist[c+'_szn']=grp.transform(lambda s:s.shift(1).expanding().mean())
keep=['gsis_id','season','week']+[c for c in hist.columns if c.endswith(('_l3','_last','_szn'))]
df=df.merge(hist[keep].drop_duplicates(['gsis_id','season','week']),on=['gsis_id','season','week'],how='left')
# previous weeks' projection residual (does ESPN under-react?)
df['resid']=df.actual-df.proj
grp=df[df.played].groupby(['espn_id','season']).resid
df.loc[df.played,'resid_l3']=grp.transform(lambda s:s.shift(1).rolling(3,min_periods=1).mean())
df['resid_l3']=df.groupby(['espn_id','season']).resid_l3.transform(lambda s:s.ffill())
df.to_pickle('bt.pkl'); print(df.shape); print(df[df.proj>=4].groupby(['season','pos']).agg(n=('proj','size'),played=('played','mean'),proj=('proj','mean'),actual=('actual','mean')).round(2))
