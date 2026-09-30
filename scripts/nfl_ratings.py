"""NFL power ratings from play-by-play efficiency (nflverse), plus backtest vs closing lines.

Build:   python scripts/nfl_ratings.py build <pbp_dir>   -> data/derived/nfl_team_games.csv
Rate:    python scripts/nfl_ratings.py rate               -> data/derived/nfl_ratings.json (+ backtest)

Rating = exponentially weighted, opponent-adjusted EPA/play on offense and defense,
regressed toward average between seasons. Garbage time (win prob <5% or >95%) is excluded.
Predicted home margin = HFA + BETA * (home net rating - away net rating), with HFA and BETA
fitted on past seasons only.
"""
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DERIVED = ROOT / "data" / "derived"
GAMES_CSV = ROOT / "data" / "raw" / "nflverse" / "games.csv"
TEAM_GAMES = DERIVED / "nfl_team_games.csv"

ALPHA = 0.12          # weight of the newest game in the running rating
SEASON_CARRY = 0.55   # share of last season's rating kept at week 1
FIT_SEASONS = range(2016, 2021)
TEST_SEASONS = range(2021, 2026)

TEAM_FIX = {"OAK": "LV", "SD": "LAC", "STL": "LA"}


# ------------------------------------------------------------------ build
def build(pbp_dir):
    import pandas as pd

    cols = ["game_id", "season", "week", "posteam", "defteam", "epa", "success", "pass", "rush",
            "wp", "qb_kneel", "qb_spike", "play_type"]
    frames = []
    for f in sorted(Path(pbp_dir).glob("pbp_*.parquet")):
        df = pd.read_parquet(f, columns=cols)
        df = df[(df["pass"] == 1) | (df["rush"] == 1)]
        df = df[(df["qb_kneel"] != 1) & (df["qb_spike"] != 1) & df["epa"].notna()]
        df = df[(df["wp"] > 0.05) & (df["wp"] < 0.95)]
        keys = ["game_id", "season", "week", "posteam", "defteam"]
        g = df.groupby(keys).agg(plays=("epa", "size"), epa=("epa", "mean"),
                                 success=("success", "mean")).reset_index()
        pe = df[df["pass"] == 1].groupby(keys)["epa"].agg(["mean", "size"]).reset_index()
        pe.columns = keys + ["pass_epa", "dropbacks"]
        re_ = df[df["rush"] == 1].groupby(keys)["epa"].mean().reset_index(name="rush_epa")
        g = g.merge(pe, on=keys, how="left").merge(re_, on=keys, how="left")
        frames.append(g)
        print(f"{f.name}: {len(g)} team-games")
    out = pd.concat(frames)
    for c in ("posteam", "defteam"):
        out[c] = out[c].replace(TEAM_FIX)
    if TEAM_GAMES.exists():  # merge: replace only the seasons we just rebuilt
        old = pd.read_csv(TEAM_GAMES)
        out = pd.concat([old[~old["season"].isin(out["season"].unique())], out])
    out = out.sort_values(["season", "week", "game_id", "posteam"])
    DERIVED.mkdir(parents=True, exist_ok=True)
    out.round(5).to_csv(TEAM_GAMES, index=False)
    print(f"wrote {TEAM_GAMES} ({len(out)} rows)")


# ------------------------------------------------------------------ rate
def load_schedule():
    games = []
    with open(GAMES_CSV) as f:
        for r in csv.DictReader(f):
            if int(r["season"]) < 2015:
                continue
            games.append({
                "game_id": r["game_id"], "season": int(r["season"]), "week": int(r["week"]),
                "gameday": r["gameday"], "home": TEAM_FIX.get(r["home_team"], r["home_team"]),
                "away": TEAM_FIX.get(r["away_team"], r["away_team"]),
                "result": float(r["result"]) if r["result"] else None,
                "spread_line": float(r["spread_line"]) if r["spread_line"] else None,
                "neutral": r["location"] == "Neutral",
                "home_qb": r["home_qb_name"], "away_qb": r["away_qb_name"],
            })
    games.sort(key=lambda g: (g["gameday"], g["game_id"]))
    return games


def load_team_games():
    tg = defaultdict(dict)
    with open(TEAM_GAMES) as f:
        for r in csv.DictReader(f):
            tg[r["game_id"]][r["posteam"]] = {"epa": float(r["epa"]), "plays": int(r["plays"])}
    return tg


def run_ratings(games, tg):
    """Walk games in date order. Record pre-game ratings for each game, then update."""
    off = defaultdict(float)
    dfn = defaultdict(float)   # EPA/play allowed (higher = worse defense)
    season_seen = None
    rows = []
    for g in games:
        if g["season"] != season_seen:
            for t in list(off):
                off[t] *= SEASON_CARRY
                dfn[t] *= SEASON_CARRY
            season_seen = g["season"]
        h, a = g["home"], g["away"]
        net_h, net_a = off[h] - dfn[h], off[a] - dfn[a]
        rows.append({**g, "net_home": net_h, "net_away": net_a, "diff": net_h - net_a})
        stats = tg.get(g["game_id"])
        if not stats or h not in stats or a not in stats:
            continue
        # opponent-adjusted observations
        oh = stats[h]["epa"] - dfn[a]
        oa = stats[a]["epa"] - dfn[h]
        dh = stats[a]["epa"] - off[a]   # what home D allowed, relative to away O
        da = stats[h]["epa"] - off[h]
        off[h] += ALPHA * (oh - off[h])
        off[a] += ALPHA * (oa - off[a])
        dfn[h] += ALPHA * (dh - dfn[h])
        dfn[a] += ALPHA * (da - dfn[a])
    return rows, off, dfn


def ols(xs, ys):
    """y = a + b*x"""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def ols2(x1, x2, y):
    """y = b1*x1 + b2*x2 (no intercept) via normal equations."""
    a11 = sum(v * v for v in x1); a22 = sum(v * v for v in x2); a12 = sum(u * v for u, v in zip(x1, x2))
    c1 = sum(u * v for u, v in zip(x1, y)); c2 = sum(u * v for u, v in zip(x2, y))
    det = a11 * a22 - a12 * a12
    return (c1 * a22 - c2 * a12) / det, (a11 * c2 - a12 * c1) / det


def backtest(rows, hfa, beta):
    out = {}
    test = [r for r in rows if r["season"] in TEST_SEASONS and r["result"] is not None
            and r["spread_line"] is not None and r["week"] >= 3]
    pred = [hfa * (not r["neutral"]) + beta * r["diff"] for r in test]
    res = [r["result"] for r in test]
    line = [r["spread_line"] for r in test]
    rmse = lambda p: math.sqrt(sum((a - b) ** 2 for a, b in zip(p, res)) / len(res))  # noqa: E731
    out["games"] = len(test)
    out["rmse_model"] = round(rmse(pred), 2)
    out["rmse_closing_line"] = round(rmse(line), 2)
    # does the model add information beyond the closing line?
    b_line, b_model = ols2(line, [p - l for p, l in zip(pred, line)], res)
    out["weight_on_line"] = round(b_line, 3)
    out["weight_on_model_minus_line"] = round(b_model, 3)
    # ATS when model disagrees with the close by >= t points
    for t in (1, 2, 3, 4):
        w = l = 0
        for p, r, ln in zip(pred, res, line):
            if abs(p - ln) < t:
                continue
            side = 1 if p > ln else -1
            v = side * (r - ln)
            if v > 0:
                w += 1
            elif v < 0:
                l += 1
        out[f"ats_diff_ge_{t}"] = {"w": w, "l": l, "pct": round(w / (w + l), 3) if w + l else None}
    return out


def rate():
    games, tg = load_schedule(), load_team_games()
    rows, off, dfn = run_ratings(games, tg)
    fit = [r for r in rows if r["season"] in FIT_SEASONS and r["result"] is not None and r["week"] >= 3]
    # fit HFA separately on non-neutral games: result = hfa + beta*diff
    hfa, beta = ols([r["diff"] for r in fit if not r["neutral"]], [r["result"] for r in fit if not r["neutral"]])
    bt = backtest(rows, hfa, beta)

    # blend weight for live use: how much of (model - market) to trust
    blend = max(0.0, min(0.5, bt["weight_on_model_minus_line"]))

    upcoming = [r for r in rows if r["result"] is None and r["season"] == max(g["season"] for g in games)]
    preds = {r["game_id"]: {
        "home": r["home"], "away": r["away"], "gameday": r["gameday"], "week": r["week"],
        "model_home_margin": round(hfa * (not r["neutral"]) + beta * r["diff"], 1),
        "net_home": round(r["net_home"], 4), "net_away": round(r["net_away"], 4),
        "home_qb": r["home_qb"], "away_qb": r["away_qb"],
    } for r in upcoming}
    table = sorted(((t, off[t] - dfn[t], off[t], dfn[t]) for t in off), key=lambda x: -x[1])
    out = {
        "meta": {"alpha": ALPHA, "season_carry": SEASON_CARRY, "hfa": round(hfa, 2), "beta": round(beta, 1),
                 "blend_weight": round(blend, 3), "backtest": bt},
        "ratings": [{"team": t, "net": round(n, 4), "pts_vs_avg": round(beta * n, 1),
                     "off_epa": round(o, 4), "def_epa_allowed": round(d, 4)} for t, n, o, d in table],
        "games": preds,
    }
    DERIVED.mkdir(parents=True, exist_ok=True)
    (DERIVED / "nfl_ratings.json").write_text(json.dumps(out, indent=1))
    return out


def refresh_current(season):
    """Download this season's play-by-play and rebuild only its rows (used by the Action)."""
    import tempfile
    import requests
    url = f"https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet"
    with tempfile.TemporaryDirectory() as d:
        Path(d, f"pbp_{season}.parquet").write_bytes(requests.get(url, timeout=120).content)
        build(d)


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2])
    elif sys.argv[1] == "refresh":
        refresh_current(int(sys.argv[2]))
        print(json.dumps(rate()["meta"]["backtest"]))
    else:
        o = rate()
        print(json.dumps(o["meta"], indent=1))
