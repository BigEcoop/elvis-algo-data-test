"""CASH touchdown research: export official nflverse play-level events without results leakage.
The output is a historical SOURCE dataset; downstream code must filter week < target week.
"""
import csv, gzip, json
from collections import Counter
from pathlib import Path
import nflreadpy as nfl

OUT=Path("cash_touchdown_pbp_export")
OUT.mkdir(exist_ok=True)
fields=[
"season","week","season_type","game_id","game_date","play_id","drive",
"posteam","defteam","play_type","no_play","yardline_100","goal_to_go",
"rush_attempt","pass_attempt","qb_dropback","sack","qb_scramble",
"touchdown","rush_touchdown","pass_touchdown","return_touchdown",
"field_goal_attempt","field_goal_result","extra_point_attempt","two_point_attempt",
"receiving_touchdown","rushing_touchdown",
"receiver_player_id","receiver_player_name","rusher_player_id","rusher_player_name",
"passer_player_id","passer_player_name","air_yards","td_team",
"drive_start_yard_line","drive_end_yard_line",
]
totals={}
for season,last_week in ((2025,18),(2026,8)):
    df=nfl.load_pbp(seasons=season)
    cols=[c for c in fields if c in df.columns]
    missing=sorted(set(fields)-set(cols))
    required={"season","week","game_id","drive","posteam","play_type","yardline_100",
              "rush_touchdown","pass_touchdown","receiver_player_id","rusher_player_id"}
    if required-set(cols):
        raise RuntimeError(f"{season}: required play-by-play fields absent: {sorted(required-set(cols))}")
    import polars as pl
    df=df.filter((pl.col("season_type")=="REG")&(pl.col("week")<=last_week)).select(cols)
    fname=OUT/f"pbp_{season}_to_w{last_week}.csv.gz"
    with gzip.open(fname,"wt",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=cols)
        writer.writeheader()
        for row in df.iter_rows(named=True):
            writer.writerow(row)
    totals[str(season)]={"rows":df.height,"last_week":last_week,
                         "columns":cols,"missing_optional_columns":missing,
                         "export":fname.name}
    print(f"{season}: exported {df.height:,} rows; {len(cols)} columns",flush=True)
(OUT/"manifest.json").write_text(json.dumps({"source":"nflreadpy nflverse play-by-play",
    "seasons":totals,"note":"PBP includes game outcomes. Use only games strictly earlier than target game when building forecasts."},indent=2))
print(json.dumps({k:{"rows":v["rows"],"last_week":v["last_week"]} for k,v in totals.items()}))
