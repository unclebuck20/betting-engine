"""College power ratings from per-game PPA (CollegeFootballData), backtested vs opening AND closing lines.

Same idea as the NFL ratings: exponentially weighted, opponent-adjusted offensive and defensive
PPA per play, regressed between seasons. The key test for college is against the OPENING line:
if the model predicts where the line moves, betting early captures closing line value.
"""
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nfl_ratings import ols, ols2  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "raw" / "cfbd_history"
DERIVED = ROOT / "data" / "derived"
PROVIDERS = ["DraftKings", "ESPN Bet", "Bovada", "consensus", "William Hill (New Jersey)", "teamrankings"]

ALPHA = 0.15
SEASON_CARRY = 0.5
FIT_SEASONS = {2021, 2022}
TEST_SEASONS = {2023, 2024, 2025}


def pick_line(lines):
    by = {l.get("provider"): l for l in lines or []}
    for p in PROVIDERS + list(by):
        l = by.get(p)
        if l and l.get("spread") is not None:
            return l
    return None


def load(seasons):
    games, ppa = [], {}
    for y in seasons:
        gpath, lpath, ppath = (HIST / f"{n}_{y}.json" for n in ("games", "lines", "ppa"))
        if not (gpath.exists() and lpath.exists() and ppath.exists()):
            print(f"missing history for {y}")
            continue
        lines = {l["id"]: l for l in json.loads(lpath.read_text())}
        for g in json.loads(gpath.read_text()):
            L = pick_line(lines.get(g["id"], {}).get("lines"))
            hp, ap = g.get("homePoints"), g.get("awayPoints")
            games.append({
                "id": g["id"], "season": g["season"], "week": g["week"],
                "start": g.get("startDate", ""), "home": g["homeTeam"], "away": g["awayTeam"],
                "neutral": bool(g.get("neutralSite")),
                "fbs_both": g.get("homeClassification") == "fbs" and g.get("awayClassification") == "fbs",
                "result": (hp - ap) if hp is not None and ap is not None else None,
                # CFBD spread is from the home side: -7 = home favored by 7 -> expected home margin +7
                "close": -L["spread"] if L else None,
                "open": -L["spreadOpen"] if L and L.get("spreadOpen") is not None else None,
            })
        for p in json.loads(ppath.read_text()):
            ppa[(p["gameId"], p["team"])] = (p["offense"]["overall"], p["defense"]["overall"])
    games.sort(key=lambda g: (g["season"], g["week"], g["start"]))
    return games, ppa


def run(games, ppa):
    off, dfn = defaultdict(float), defaultdict(float)
    season, rows = None, []
    for g in games:
        if g["season"] != season:
            for t in list(off):
                off[t] *= SEASON_CARRY; dfn[t] *= SEASON_CARRY
            season = g["season"]
        h, a = g["home"], g["away"]
        rows.append({**g, "diff": (off[h] - dfn[h]) - (off[a] - dfn[a])})
        ph, pa = ppa.get((g["id"], h)), ppa.get((g["id"], a))
        if not ph or not pa or ph[0] is None or pa[0] is None:
            continue
        oh, oa = ph[0] - dfn[a], pa[0] - dfn[h]
        dh, da = ph[1] - off[a], pa[1] - off[h]
        off[h] += ALPHA * (oh - off[h]); off[a] += ALPHA * (oa - off[a])
        dfn[h] += ALPHA * (dh - dfn[h]); dfn[a] += ALPHA * (da - dfn[a])
    return rows, off, dfn


def ats(pred, line, res, t):
    w = l = 0
    for p, ln, r in zip(pred, line, res):
        if abs(p - ln) < t:
            continue
        v = (1 if p > ln else -1) * (r - ln)
        w += v > 0; l += v < 0
    return {"w": w, "l": l, "pct": round(w / (w + l), 3) if w + l else None}


CURRENT = ROOT / "data" / "raw" / "cfbd"


def live_ratings(season):
    """History + this season's completed games -> (hfa, beta, rating fn, odds-name -> school)."""
    games, ppa = load(sorted(FIT_SEASONS | TEST_SEASONS))
    fit = run(games, ppa)[0]
    fit = [r for r in fit if r["season"] in FIT_SEASONS and r["fbs_both"] and r["result"] is not None
           and r["week"] >= 4 and not r["neutral"]]
    hfa, beta = ols([r["diff"] for r in fit], [r["result"] for r in fit])
    for f in sorted(CURRENT.glob("week_*_games.json")):
        for g in json.loads(f.read_text()):
            hp, ap = g.get("homePoints"), g.get("awayPoints")
            if hp is None or ap is None:
                continue
            games.append({"id": g["id"], "season": g["season"], "week": g["week"], "start": g.get("startDate", ""),
                          "home": g["homeTeam"], "away": g["awayTeam"], "neutral": bool(g.get("neutralSite")),
                          "fbs_both": True, "result": hp - ap, "close": None, "open": None})
    pp = CURRENT / f"ppa_{season}.json"
    if pp.exists():
        for p in json.loads(pp.read_text()):
            ppa[(p["gameId"], p["team"])] = (p["offense"]["overall"], p["defense"]["overall"])
    games.sort(key=lambda g: (g["season"], g["week"], g["start"]))
    _, off, dfn = run(games, ppa)
    names = {}
    tp = CURRENT / "teams.json"
    if tp.exists():
        for t in json.loads(tp.read_text()):
            if t.get("mascot"):
                names[f"{t['school']} {t['mascot']}"] = t["school"]
    current_season_teams = {g["home"] for g in games if g["season"] == season} | \
                           {g["away"] for g in games if g["season"] == season}
    return {"hfa": hfa, "beta": beta, "off": off, "dfn": dfn, "names": names,
            "played": current_season_teams}


def live_margin(L, home_odds_name, away_odds_name, neutral=False):
    def school(n):
        if n in L["names"]:
            return L["names"][n]
        cands = [s for s in set(L["names"].values()) if n.startswith(s + " ")]
        return max(cands, key=len) if cands else None
    h, a = school(home_odds_name), school(away_odds_name)
    if not h or not a or h not in L["played"] or a not in L["played"]:
        return None, h, a
    diff = (L["off"][h] - L["dfn"][h]) - (L["off"][a] - L["dfn"][a])
    return L["hfa"] * (not neutral) + L["beta"] * diff, h, a


def main():
    games, ppa = load(sorted(FIT_SEASONS | TEST_SEASONS))
    rows, off, dfn = run(games, ppa)
    usable = [r for r in rows if r["fbs_both"] and r["result"] is not None and r["week"] >= 4]
    fit = [r for r in usable if r["season"] in FIT_SEASONS and not r["neutral"]]
    hfa, beta = ols([r["diff"] for r in fit], [r["result"] for r in fit])
    test = [r for r in usable if r["season"] in TEST_SEASONS and r["close"] is not None and r["open"] is not None]
    pred = [hfa * (not r["neutral"]) + beta * r["diff"] for r in test]
    res = [r["result"] for r in test]
    close = [r["close"] for r in test]
    opn = [r["open"] for r in test]
    rmse = lambda p: round(math.sqrt(sum((a - b) ** 2 for a, b in zip(p, res)) / len(res)), 2)  # noqa: E731
    # does model predict the line MOVE (open -> close)?
    move = [c - o for c, o in zip(close, opn)]
    gap = [p - o for p, o in zip(pred, opn)]
    _, move_slope = ols(gap, move)
    same_dir = sum(1 for g, m in zip(gap, move) if abs(g) >= 3 and m != 0 and (g > 0) == (m > 0))
    any_dir = sum(1 for g, m in zip(gap, move) if abs(g) >= 3 and m != 0)
    bt = {
        "games": len(test), "hfa": round(hfa, 2), "beta": round(beta, 1),
        "rmse_model": rmse(pred), "rmse_open": rmse(opn), "rmse_close": rmse(close),
        "weight_model_vs_close": round(ols2(close, [p - c for p, c in zip(pred, close)], res)[1], 3),
        "weight_model_vs_open": round(ols2(opn, [p - o for p, o in zip(pred, opn)], res)[1], 3),
        "line_move_per_point_of_model_gap": round(move_slope, 3),
        "move_toward_model_when_gap_ge_3": f"{same_dir}/{any_dir}",
        "ats_vs_open": {t: ats(pred, opn, res, t) for t in (3, 5, 7)},
        "ats_vs_close": {t: ats(pred, close, res, t) for t in (3, 5, 7)},
    }
    DERIVED.mkdir(parents=True, exist_ok=True)
    (DERIVED / "cfb_backtest.json").write_text(json.dumps(bt, indent=1))
    print(json.dumps(bt, indent=1))


if __name__ == "__main__":
    main()
