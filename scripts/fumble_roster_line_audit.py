"""FUMBLE research audit: roster hierarchy and TEAM line performance proxies.
This is NOT the FUMBLE executable and does not score individual lineman talent.
"""
import csv
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import nflreadpy as nfl

SEASON = int(os.environ.get("SEASON", "2026"))
THROUGH_WEEK = int(os.environ.get("THROUGH_WEEK", "8"))
CUTOFF = os.environ.get("CUTOFF_UTC") or datetime.now(timezone.utc).isoformat()
OUT = Path("fumble_line_audit")
OUT.mkdir(exist_ok=True)
warnings = []

def as_utc(value):
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

cutoff = as_utc(CUTOFF)
if cutoff is None:
    raise ValueError("CUTOFF_UTC must be an ISO 8601 timestamp")

def num(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None

def flag(value):
    return str(value).lower() in ("1", "true", "yes")

def write(name, rows, fields=None):
    rows = list(rows)
    fields = fields or sorted({key for row in rows for key in row})
    with (OUT / name).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"{name}: {len(rows):,} records")
    return len(rows)

def safe_load(name, call, required=False):
    try:
        df = call()
        if df.is_empty():
            raise ValueError("empty dataset")
        print(f"{name}: {df.height:,} rows, columns={df.columns}")
        return df.to_dicts()
    except Exception as exc:
        warnings.append(f"{name}: {exc}")
        if required:
            raise
        return []

# 2025+ depth charts use dt and pos_rank; rank is ORDER on chart, not ability.
depth = safe_load("depth", lambda: nfl.load_depth_charts(seasons=SEASON), True)
eligible = []
for row in depth:
    dt = as_utc(row.get("dt"))
    if dt is not None and dt <= cutoff:
        eligible.append((dt, row))
latest = {}
for dt, row in eligible:
    team = row.get("team")
    if team and (team not in latest or dt > latest[team]):
        latest[team] = dt
lines = []
for dt, row in eligible:
    if latest.get(row.get("team")) != dt:
        continue
    pos = str(row.get("pos_abb") or "").upper()
    name = str(row.get("pos_name") or "").lower()
    if pos in ("LT","LG","C","RG","RT","OT","OG","T","G","OL") or "offensive lin" in name:
        group = "OL"
    elif pos in ("DE","DT","NT","DL","EDGE","LE","RE","LDE","RDE") or "defensive lin" in name:
        group = "DL"
    elif pos in ("OLB","LOLB","ROLB"):
        group = "EDGE_OR_LB"  # Edge candidate, NOT automatically a DL starter.
    else:
        continue
    lines.append({
        "team": row.get("team"), "snapshot_utc": dt.isoformat(),
        "player_name": row.get("player_name"), "gsis_id": row.get("gsis_id"),
        "espn_id": row.get("espn_id"), "pos_abb": pos, "pos_name": row.get("pos_name"),
        "pos_slot": row.get("pos_slot"), "pos_rank": row.get("pos_rank"),
        "line_group": group, "listed_first_string": num(row.get("pos_rank")) == 1,
    })
n_line = write("ol_dl_depth_positions.csv", lines, [
    "team","snapshot_utc","player_name","gsis_id","espn_id","pos_abb",
    "pos_name","pos_slot","pos_rank","line_group","listed_first_string"
])
if not lines:
    raise RuntimeError("No eligible OL/DL depth chart rows; check snapshot coverage and cutoff.")

roster = safe_load("weekly rosters", lambda: nfl.load_rosters_weekly(seasons=SEASON))
roster_rows = []
for row in roster:
    week = num(row.get("week"))
    if week is not None and 1 <= week <= THROUGH_WEEK:
        roster_rows.append({k: row.get(k) for k in (
            "season","week","team","position","depth_chart_position","gsis_id",
            "espn_id","full_name","status","status_description_abbr"
        )})
write("weekly_roster_history.csv", roster_rows,
      ["season","week","team","position","depth_chart_position","gsis_id",
       "espn_id","full_name","status","status_description_abbr"])

# These are auditable TEAM proxies; scheme, QB, RB and opponent also affect them.
pbp = safe_load("play by play", lambda: nfl.load_pbp(seasons=SEASON))
agg = defaultdict(lambda: defaultdict(float))
for p in pbp:
    week = num(p.get("week"))
    if week is None or not 1 <= week <= THROUGH_WEEK:
        continue
    if p.get("season_type", "REG") != "REG" or flag(p.get("no_play")):
        continue
    offense, defense = p.get("posteam"), p.get("defteam")
    if not offense or not defense or offense == defense:
        continue
    drop = flag(p.get("qb_dropback"))
    sack = flag(p.get("sack"))
    run = (flag(p.get("rush_attempt")) or p.get("play_type") == "run") and not flag(p.get("qb_scramble")) and not sack
    if drop:
        agg[offense]["dropbacks"] += 1
        agg[defense]["def_dropbacks"] += 1
        agg[offense]["sacks_allowed"] += int(sack)
        agg[defense]["sacks_generated"] += int(sack)
    if run:
        s = num(p.get("success"))
        if s is not None:
            agg[offense]["run_samples"] += 1
            agg[defense]["def_run_samples"] += 1
            agg[offense]["run_successes"] += s
            agg[defense]["run_successes_allowed"] += s
teams = []
for team, vals in sorted(agg.items()):
    def ratio(n, d):
        return round(vals[n] / vals[d], 5) if vals[d] else None
    teams.append({
       "team": team,
       "dropbacks": int(vals["dropbacks"]),
       "def_dropbacks": int(vals["def_dropbacks"]),
       "designed_run_samples": int(vals["run_samples"]),
       "def_run_samples": int(vals["def_run_samples"]),
       "sack_rate_allowed": ratio("sacks_allowed","dropbacks"),
       "run_success_rate": ratio("run_successes","run_samples"),
       "sack_rate_generated": ratio("sacks_generated","def_dropbacks"),
       "run_success_allowed": ratio("run_successes_allowed","def_run_samples"),
    })
for metric, descending in (
    ("sack_rate_allowed", False), ("run_success_rate", True),
    ("sack_rate_generated", True), ("run_success_allowed", False),
):
    valid = [r for r in teams if r[metric] is not None and
             r["dropbacks"] >= 40 and r["def_dropbacks"] >= 40 and
             r["designed_run_samples"] >= 40 and r["def_run_samples"] >= 40]
    for rank, row in enumerate(sorted(valid, key=lambda r: r[metric], reverse=descending), 1):
        row[metric + "_proxy_rank"] = rank
write("team_line_performance_proxies.csv", teams)

# Defensive advanced stats provide extra information, but aren't OL/DL player grades.
adv = safe_load("PFR defensive advanced", lambda: nfl.load_pfr_advstats(
    seasons=SEASON, stat_type="def", summary_level="week"))
adv_rows = [r for r in adv if num(r.get("week")) is not None and
            num(r.get("week")) <= THROUGH_WEEK]
write("pfr_defensive_advanced_raw.csv", adv_rows, list(adv_rows[0]) if adv_rows else ["unavailable"])
summary = {
  "season": SEASON, "through_week": THROUGH_WEEK,
  "cutoff_utc": cutoff.isoformat(), "depth_snapshot_rows": n_line,
  "weekly_roster_rows": len(roster_rows), "team_proxy_rows": len(teams),
  "advanced_defender_rows": len(adv_rows),
  "depth_rank_meaning": "pos_rank=1 is first string for that position slot; NOT NFL skill rank",
  "metric_caveat": "Line ranks are TEAM-LEVEL sack/run proxy measures; NOT independent OL/DL grades, not opponent adjusted",
  "safety": "Weekly roster statuses are NOT verified final pregame inactives. Never use future depth snapshots for a past game.",
  "warnings": warnings
}
(OUT / "coverage_and_limits.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2))
print("AUDIT COMPLETE: these files do not automatically change the FUMBLE executable.")
