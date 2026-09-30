"""NFL injury layer: who is actually missing, and roughly what it's worth.

Starters are defined from real usage, not depth charts: players who played >= 60% of
offensive or defensive snaps in at least 2 of the team's last 3 games (nflverse snap counts).
A starter listed Out / Doubtful / IR (ESPN) is a real absence. Players already missing for
weeks aren't starters by this rule, so their absence is already in the ratings and the line.

Point values are priors (points of spread per missing starter). They are deliberately
conservative and get recalibrated against closing-line movement once we have our own history.
"""
import csv
import io
import json
import re
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
DERIVED = ROOT / "data" / "derived"
SNAPS_URL = "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{season}.csv"

STATUS_WEIGHT = {"Out": 1.0, "Injured Reserve": 1.0, "Doubtful": 0.85, "Questionable": 0.25}
QB_VALUE = 4.0          # starting QB out; placeholder until we have backup-specific values
POS_VALUE = {
    "T": 0.5, "G": 0.3, "C": 0.4, "OL": 0.35,
    "WR": 0.5, "TE": 0.3, "RB": 0.3, "FB": 0.0,
    "DE": 0.5, "OLB": 0.4, "EDGE": 0.5, "DT": 0.3, "NT": 0.25, "DL": 0.35,
    "LB": 0.25, "ILB": 0.25, "MLB": 0.25,
    "CB": 0.5, "S": 0.3, "FS": 0.3, "SS": 0.3, "DB": 0.35,
    "K": 0.3, "P": 0.1, "LS": 0.0,
}
NON_QB_CAP = 3.0
ESPN_TEAM_FIX = {"WSH": "WAS", "LAR": "LA", "JAX": "JAX"}


def norm(name):
    name = (name or "").lower()
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", name)
    return re.sub(r"[^a-z]", "", name)


def load_snaps(season):
    text = requests.get(SNAPS_URL.format(season=season), timeout=60).text
    return list(csv.DictReader(io.StringIO(text)))


def starters(snaps):
    by_team_games = defaultdict(set)
    for r in snaps:
        by_team_games[r["team"]].add((int(r["week"]), r["game_id"]))
    last3 = {t: {g for _, g in sorted(gs)[-3:]} for t, gs in by_team_games.items()}
    hits = defaultdict(int)
    info = {}
    for r in snaps:
        if r["game_id"] not in last3[r["team"]]:
            continue
        pct = max(float(r["offense_pct"] or 0), float(r["defense_pct"] or 0))
        key = (r["team"], norm(r["player"]))
        info[key] = {"player": r["player"], "pos": r["position"], "team": r["team"]}
        if pct >= 0.6:
            hits[key] += 1
    qb = {}
    for (team, n), c in hits.items():
        if info[(team, n)]["pos"] == "QB" and c >= 2:
            qb[team] = n
    return {k: info[k] for k, c in hits.items() if c >= 2}, qb


def team_adjustments(rows, st, qbs):
    teams = defaultdict(lambda: {"adj_points": 0.0, "qb_out": False, "absences": []})
    for r in rows:
        w = STATUS_WEIGHT.get(r["status"])
        if not w:
            continue
        team = ESPN_TEAM_FIX.get(r["team"], r["team"])
        key = (team, norm(r["player"]))
        if key not in st:
            continue
        pos = st[key]["pos"]
        is_qb = qbs.get(team) == key[1]
        val = (QB_VALUE if is_qb else POS_VALUE.get(pos, 0.2)) * w
        t = teams[team]
        t["absences"].append({"player": r["player"], "pos": "QB" if is_qb else pos, "status": r["status"],
                              "points": round(val, 2), "updated": r["updated"], "note": r["note"]})
        if is_qb and w >= 0.85:
            t["qb_out"] = True
    out = {}
    for team, t in teams.items():
        qb_pts = sum(a["points"] for a in t["absences"] if a["pos"] == "QB")
        other = min(NON_QB_CAP, sum(a["points"] for a in t["absences"] if a["pos"] != "QB"))
        t["adj_points"] = round(qb_pts + other, 2)
        t["absences"].sort(key=lambda a: -a["points"])
        out[team] = t
    return out


def build(season):
    snaps = load_snaps(season)
    st, qbs = starters(snaps)
    inj = json.loads((RAW / "injuries" / "nfl" / "latest.json").read_text())
    out = team_adjustments(inj["rows"], st, qbs)
    prev_path = RAW / "injuries" / "nfl" / "previous.json"
    prev = json.loads(prev_path.read_text()) if prev_path.exists() else None
    prev_adj = team_adjustments(prev["rows"], st, qbs) if prev else {}
    for team in set(out) | set(prev_adj):
        if prev is None:          # first run: no baseline, so nothing counts as "new"
            out.setdefault(team, {"adj_points": 0.0, "qb_out": False, "absences": []})
            out[team]["delta_since_last_pull"], out[team]["new_absences"] = 0.0, []
            continue
        now_pts = out.get(team, {}).get("adj_points", 0.0)
        was = prev_adj.get(team, {}).get("adj_points", 0.0)
        t = out.setdefault(team, {"adj_points": 0.0, "qb_out": False, "absences": []})
        t["delta_since_last_pull"] = round(now_pts - was, 2)
        before = {a["player"] for a in prev_adj.get(team, {}).get("absences", [])}
        t["new_absences"] = [a for a in t["absences"] if a["player"] not in before]
    meta = {"season": season, "starters_identified": len(st), "teams_with_qb": len(qbs),
            "injury_pull": inj["pulled_at"], "previous_pull": prev["pulled_at"] if prev else None,
            "matched_absences": sum(len(t["absences"]) for t in out.values())}
    DERIVED.mkdir(parents=True, exist_ok=True)
    (DERIVED / "nfl_injuries.json").write_text(json.dumps({"meta": meta, "teams": out}, indent=1))
    return meta, out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import config as C
    meta, out = build(C.SEASON)
    print(f"::notice::injuries: {meta}")
    for team, t in sorted(out.items(), key=lambda x: -x[1]["adj_points"])[:10]:
        print(team, t["adj_points"], "QB OUT" if t["qb_out"] else "",
              [f"{a['player']} {a['pos']} {a['status']}" for a in t["absences"][:4]])
