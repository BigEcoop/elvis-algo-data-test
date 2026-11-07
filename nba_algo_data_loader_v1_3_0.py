#!/usr/bin/env python3
"""NBA Algo (Never Broke Again) v1.3.0 -- nflverse/nflreadpy data collector.

This is a new *data-adapter*, NOT a replacement for the preserved Elvis v1.2.1
Monte Carlo engine. Data collected after a game cannot be used to predict it.

Python 3.11+: pip install nflreadpy
    python nba_algo_data_loader_v1_3_0.py --season 2026 --week 9 --out-dir feed

Outputs: NBA_data_feed_*.json, NBA_data_audit_*.json. No guessed routes,
coverage, game-day inactives, weather, or market prices are ever inserted.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import sys
import unicodedata

VERSION = 'NBA_Algo_v1.3.0_data_loader'
DATA_URL = 'https://github.com/nflverse/nflverse-data/releases'
SNAP_URL = 'https://github.com/nflverse/nflverse-data/releases/tag/snap_counts'
CORE_STATS = ('targets','receptions','receiving_yards','carries','rushing_yards',
              'attempts','completions','passing_yards','passing_tds','rushing_tds','receiving_tds')
POSITIONS = {'QB','RB','WR','TE'}
TEAM_ALIASES = {'JAC':'JAX','LA':'LAR','STL':'LAR','SD':'LAC','OAK':'LV','WSH':'WAS'}


def team_code(value):
    return TEAM_ALIASES.get(str(value or '').strip().upper(), str(value or '').strip().upper())


def clean_name(value):
    s=unicodedata.normalize('NFKD',str(value or '')).encode('ascii','ignore').decode()
    s=s.lower().replace("'",'').replace('’','').replace('.','').replace('-',' ')
    return re.sub(r'\s+',' ',s).strip()


def numeric(value, default=0.):
    if value is None or isinstance(value, bool):return default
    try:
        x=float(value)
        return x if math.isfinite(x) else default
    except (TypeError,ValueError):return default


def ratio(value):
    if value is None:return None
    x=numeric(str(value).replace('%',''),None)
    if x is None:return None
    if x>1 and x<=100:x/=100
    return round(x,5) if 0<=x<=1 else None


def recs(df):
    if hasattr(df,'to_dicts'):return df.to_dicts()
    if isinstance(df,list):return df
    raise TypeError('Expected a Polars DataFrame or list of records')


def scalar_mean(vals):
    return round(float(statistics.fmean(vals)),5) if vals else None


def feed_from_rows(stats_rows, snap_rows, schedule_rows, injury_rows, season, week, captured_utc=None):
    """Deterministic transform, tested offline; never includes week >= target week."""
    captured_utc=captured_utc or datetime.now(timezone.utc).isoformat()
    audit={'version':VERSION,'season':season,'target_week':week,'captured_utc':captured_utc,
           'warnings':[],'unverified_inputs':['routes run/route share','position/alignment and coverage assignments',
           'offensive/defensive line indices','game-day inactive verification','weather forecasts',
           'consensus game total/spread and player prop markets'],
           'source':DATA_URL,'cutoff':'Only regular-season rows with week < target_week are eligible'}
    clean_stats=[]
    dropped=defaultdict(int)
    for r in stats_rows:
        if int(numeric(r.get('season'),-1))!=season or int(numeric(r.get('week'),-1))>=week:
            dropped['different season or current/future week']+=1;continue
        if int(numeric(r.get('week'),-1))<1 or str(r.get('season_type',r.get('game_type','REG'))).upper() not in ('REG','R'):
            dropped['not regular-season']+=1;continue
        name=r.get('player_display_name') or r.get('player_name') or r.get('player')
        team=team_code(r.get('team') or r.get('recent_team'))
        pos=str(r.get('position') or r.get('position_group') or '').upper()
        if not name or not team or pos not in POSITIONS:
            dropped['missing player, team, or unsupported position']+=1;continue
        d={'name':str(name),'name_key':clean_name(name),'team':team,'position':pos,
           'player_id':str(r.get('player_id') or r.get('gsis_id') or ''),
           'season':season,'week':int(r['week'])}
        # Missing column is not evidence of zero production; block absent
        # mandatory counts to avoid promoting missing data to actual zeros.
        if any(k not in r for k in ('targets','carries','receptions','receiving_yards','rushing_yards')):
            raise RuntimeError('Player stats feed is missing an essential opportunity/stat column')
        for k in CORE_STATS:d[k]=numeric(r.get(k))
        clean_stats.append(d)
    if not clean_stats:raise RuntimeError('No eligible pregame player stats found for requested season/week')
    latest_stats_week=max(r['week'] for r in clean_stats)
    # For each name, keep distinct weekly records, and warn on ambiguous names/teams.
    stats_by_key=defaultdict(list)
    for r in clean_stats:stats_by_key[(r['team'],r['name_key'])].append(r)
    dupkeys=[]
    for key,rs in stats_by_key.items():
        weeks=[r['week'] for r in rs]
        if len(set(weeks))!=len(weeks):dupkeys.append(f'{key[0]}:{key[1]}')
    if dupkeys:raise RuntimeError(f'Duplicate player-team-week rows, refusing merge: {dupkeys[:8]}')
    # Normalize snap-counts; player IDs are not shared across all PFR and GSIS
    # sources. Only unique (team, normalized name, week) joins are accepted.
    snap_by_key=defaultdict(list)
    for r in snap_rows:
        if int(numeric(r.get('season'),-1))!=season:continue
        w=int(numeric(r.get('week'),-1))
        if not 1<=w<week:continue
        t=team_code(r.get('team'));key=clean_name(r.get('player'))
        if not t or not key:continue
        pct=ratio(r.get('offense_pct'))
        if pct is None:continue
        snap_by_key[(t,key,w)].append({'offense_pct':pct,'offense_snaps':numeric(r.get('offense_snaps'))})
    snap_ambiguous=[k for k,v in snap_by_key.items() if len(v)>1]
    matched=0;attempts=0;players=[];snap_missing=[]
    for (t,nk),rlist in stats_by_key.items():
        rlist.sort(key=lambda x:x['week'])
        recent=rlist[-3:]
        hist={}
        for src,field in [('targets','targets'),('receptions','receptions'),('receiving_yards','receiving_yards'),
                          ('rush_attempts','carries'),('rushing_yards','rushing_yards'),
                          ('pass_attempts','attempts'),('completions','completions'),
                          ('passing_yards','passing_yards'),('passing_tds','passing_tds')]:
            if src in ('pass_attempts','completions','passing_yards','passing_tds') and rlist[-1]['position']!='QB':continue
            # Zero-padded ISO week keys sort chronologically in the unmodified v1.2.1 engine.
            hist[src]={f'W{r["week"]:02}':round(r[field],4) for r in rlist}
        observed=[];snap_recent=[];snap_season=[]
        for r in rlist:
            attempts+=1
            key=(t,nk,r['week'])
            candidates=snap_by_key.get(key,[])
            if len(candidates)==1:
                matched+=1
                val=candidates[0]['offense_pct'];snap_season.append(val)
                if r['week'] in {rr['week'] for rr in recent}:snap_recent.append(val)
                observed.append({'week':r['week'],'offense_pct':val,'offense_snaps':candidates[0]['offense_snaps']})
        if not snap_recent:snap_missing.append(f'{t}:{rlist[-1]["name"]}')
        role={'snap_share_baseline':scalar_mean(snap_season),'snap_share_recent':scalar_mean(snap_recent),
              'sample_games':len(snap_recent),'source_quality':'VERIFIED_SNAP_ONLY' if snap_recent else 'MISSING',
              'source':SNAP_URL,'important':'Routes are NOT available from this source; do not substitute snap share for route share'}
        role={k:v for k,v in role.items() if v is not None}
        current=rlist[-1]
        player={'team':t,'name':current['name'],'position':current['position'],
                'player_id':current['player_id'],'last_recorded_week':current['week'],
                'recorded_season_games':len(rlist),'recent_recorded_weeks':[r['week'] for r in recent],
                'missed_week_gap_before_target':max(0,week-1-current['week']),
                'prior_means':{},'historical_games':hist,'role_trend':role,
                'snap_observations':observed,'sources':{'stats':DATA_URL,'snap_counts':SNAP_URL},
                'recent_totals':{k:round(sum(r[k] for r in recent),4) for k in CORE_STATS},
                'season_totals':{k:round(sum(r[k] for r in rlist),4) for k in CORE_STATS}}
        for target,orig in [('targets','targets'),('receptions','receptions'),('receiving_yards','receiving_yards'),
                            ('rush_attempts','carries'),('rushing_yards','rushing_yards'),
                            ('pass_attempts','attempts'),('completions','completions'),
                            ('passing_yards','passing_yards'),('passing_tds','passing_tds')]:
            if target in hist:player['prior_means'][target]=round(sum(r[orig] for r in rlist)/len(rlist),5)
        # Official opportunities may contain rookies with little or no usage;
        # these values remain observed, not synthetic substitution.
        players.append(player)
    players.sort(key=lambda r:(r['team'],r['position'],r['name']))
    game_list=[]
    for r in schedule_rows:
        if int(numeric(r.get('season'),-1))!=season or int(numeric(r.get('week'),-1))!=week:continue
        if str(r.get('game_type','REG')).upper() not in ('REG','R'):continue
        away=team_code(r.get('away_team'));home=team_code(r.get('home_team'))
        if not away or not home:continue
        game_list.append({'id':str(r.get('game_id') or f'{season}_{week:02}_{away}_{home}'),
                          'away':away,'home':home,'date':str(r.get('gameday') or r.get('game_date') or ''),
                          'kickoff_time':str(r.get('gametime') or ''),
                          'source':DATA_URL})
    # Injury data is provided as a separate RAW feed and is not automatically
    # promoted to final inactive/play decisions (report schema varies).
    inj=[]
    for r in injury_rows:
        if int(numeric(r.get('season'),-1))!=season:continue
        w=int(numeric(r.get('week'),-1))
        if w>week or w<max(1,week-1):continue
        inj.append({k:v for k,v in r.items() if isinstance(v,(str,int,float,bool)) or v is None})
    unique_weeks=sorted(set(r['week'] for r in clean_stats))
    if max(unique_weeks)!=week-1:
        audit['warnings'].append(f'Last player-stats week is {max(unique_weeks)}; expected {week-1}. Verify feed freshness/bye-week before analysis.')
    match_rate=matched/max(1,attempts)
    if match_rate<.75:audit['warnings'].append(f'Snap/name join coverage only {match_rate:.1%}; requires reconciliation')
    if snap_ambiguous:audit['warnings'].append(f'{len(snap_ambiguous)} ambiguous PFR snap name/team/week keys excluded')
    audit['freshness_gate_passed']=bool(max(unique_weeks)==week-1)
    audit['snap_identity_gate_passed']=bool(match_rate>=.75 and not snap_ambiguous)
    audit.update({'eligible_player_stat_rows':len(clean_stats),'eligible_snap_rows':sum(len(v) for v in snap_by_key.values()),
                  'latest_eligible_stats_week':latest_stats_week,'eligible_weeks':unique_weeks,
                  'unique_players':len(players),'snap_join_matched':matched,'snap_join_attempted':attempts,
                  'snap_join_rate':round(match_rate,4),'missing_snap_examples':snap_missing[:25],
                  'ambiguous_snap_keys':len(snap_ambiguous),'discarded_rows':dict(dropped),
                  'source_status':'OFFLINE_TRANSFORM_ONLY; live fetch only attested when load_live_feed succeeds'})
    return {'feed_version':VERSION,'season':season,'week_to_predict':week,'captured_utc':captured_utc,
            'source':DATA_URL,'source_snap':SNAP_URL,'included_weeks':unique_weeks,
            'betting_ready':False,'missing_required_data':audit['unverified_inputs'],
            'players':players,'scheduled_games':game_list,'injury_report_rows_unadjudicated':inj,
            'audit':audit}


def load_live_feed(season,week):
    try:import nflreadpy as nfl
    except ImportError as ex:raise RuntimeError('nflreadpy not installed. Install with: pip install nflreadpy') from ex
    stats=recs(nfl.load_player_stats([season])); snaps=recs(nfl.load_snap_counts([season]))
    schedules=recs(nfl.load_schedules())
    try:injuries=recs(nfl.load_injuries([season]))
    except Exception as ex:
        print(f'WARNING: injuries feed unavailable ({type(ex).__name__}: {ex})',file=sys.stderr)
        injuries=[]
    out=feed_from_rows(stats,snaps,schedules,injuries,season,week)
    out['audit']['source_status']='Fetched from nflreadpy at recorded UTC time; check provider release dates'
    if not injuries:out['audit']['warnings'].append('Injury records unavailable; must independently verify')
    return out


def collect(season,week,out_dir):
    if season<2020 or not 2<=week<=22:raise ValueError('Need season >= 2020 and week 2..22')
    out_dir=Path(out_dir);out_dir.mkdir(parents=True,exist_ok=True)
    feed=load_live_feed(season,week)
    prefix=f'NBA_Algo_{season}_W{week:02}'
    main=out_dir/f'{prefix}_DATA_FEED.json';audit=out_dir/f'{prefix}_DATA_AUDIT.json'
    main.write_text(json.dumps(feed,indent=2,default=str)+'\n',encoding='utf-8')
    audit.write_text(json.dumps(feed['audit'],indent=2,default=str)+'\n',encoding='utf-8')
    print(f'NBA Algo v1.3.0: {len(feed["players"])} players; pregame eligible weeks {feed["included_weeks"]}')
    print(f'Snap join rate: {feed["audit"]["snap_join_rate"]:.1%}; {len(feed["scheduled_games"])} Week {week} games')
    print('Routes, advanced matchup, final injuries, weather, and odds are NOT filled by this loader')
    print(f'Data feed: {main}\nAudit: {audit}')
    return main, audit


def main(argv=None):
    p=argparse.ArgumentParser(description='NBA Algo v1.3.0 free NFL data collector; preserves Elvis Algo simulation')
    p.add_argument('--season',type=int,default=2026)
    p.add_argument('--week',type=int,required=True,help='Week being predicted; excludes this week and later')
    p.add_argument('--out-dir',default='nba_algo_data_output')
    args=p.parse_args(argv)
    collect(args.season,args.week,args.out_dir)
    return 0

if __name__=='__main__':sys.exit(main())