"""NFL feature build: play-by-play (+ FTN charting) -> per-game team and QB tables.

Outputs (small, committed; the Action rebuilds only the current season):
  data/derived/nfl_team_game.parquet   one row per team per game, offense AND defense views come from
                                       joining a team's row to its opponent's row
  data/derived/nfl_qb_game.parquet     one row per QB per game
Garbage time is excluded from efficiency stats (win probability below 5% or above 95%).
"""
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DERIVED = ROOT / "data" / "derived"
TEAM_OUT, QB_OUT = DERIVED / "nfl_team_game.parquet", DERIVED / "nfl_qb_game.parquet"
REL = "https://github.com/nflverse/nflverse-data/releases/download"
TEAM_FIX = {"OAK": "LV", "SD": "LAC", "STL": "LA", "LAR": "LA"}
COLS = ["game_id", "season", "week", "season_type", "game_date", "posteam", "defteam", "home_team", "away_team",
        "play_type", "pass", "rush", "qb_dropback", "qb_scramble", "sack", "qb_hit", "epa", "qb_epa", "success",
        "wp", "down", "ydstogo", "yardline_100", "yards_gained", "air_yards", "cpoe", "xpass", "pass_oe",
        "interception", "fumble_lost", "qb_kneel", "qb_spike", "no_huddle", "shotgun", "qtr",
        "half_seconds_remaining", "game_seconds_remaining", "score_differential", "id", "play_id",
        "field_goal_result", "kick_distance", "special_teams_play", "touchdown", "first_down", "penalty",
        "passer_player_name"]


def load_pbp(path):
    import pyarrow.parquet as pq
    have = set(pq.read_schema(path).names)
    df = pq.read_table(path, columns=[c for c in COLS if c in have]).to_pandas()
    for c in ("posteam", "defteam", "home_team", "away_team"):
        df[c] = df[c].replace(TEAM_FIX)
    return df


def load_ftn(season, folder):
    p = Path(folder) / f"ftn_{season}.parquet"
    if not p.exists():
        r = requests.get(f"{REL}/ftn_charting/ftn_charting_{season}.parquet", timeout=120)
        if r.status_code != 200:
            return None
        p.write_bytes(r.content)
    f = pd.read_parquet(p, columns=["nflverse_game_id", "nflverse_play_id", "n_blitzers", "n_pass_rushers",
                                    "n_defense_box", "is_play_action", "is_motion", "is_screen_pass",
                                    "is_qb_out_of_pocket", "is_no_huddle"])
    return f.rename(columns={"nflverse_game_id": "game_id", "nflverse_play_id": "play_id"})


def team_game(df, ftn):
    """Per (game, offense team) stats. Defensive stats for a team = its opponent's offensive row."""
    s = df[((df["pass"] == 1) | (df["rush"] == 1)) & (df["qb_kneel"] != 1) & (df["qb_spike"] != 1)
           & df["epa"].notna() & df["posteam"].notna()].copy()
    s["live"] = (s["wp"] > 0.05) & (s["wp"] < 0.95)
    s["epa_w"] = s["epa"].clip(-4.5, 4.5)                  # tame a few huge plays
    s["neutral"] = s["live"] & s["down"].isin([1, 2]) & (s["qtr"] <= 3) & s["score_differential"].abs().le(10)
    s["explosive"] = ((s["pass"] == 1) & (s["yards_gained"] >= 20)) | ((s["rush"] == 1) & (s["yards_gained"] >= 12))
    s["pressure"] = ((s["sack"] == 1) | (s["qb_hit"] == 1)) & (s["qb_dropback"] == 1)
    s["turnover"] = (s["interception"] == 1) | (s["fumble_lost"] == 1)
    if ftn is not None:
        s = s.merge(ftn, on=["game_id", "play_id"], how="left")
        s["blitz"] = (s["n_blitzers"] > 0).where(s["n_blitzers"].notna())
        s["pa"] = s["is_play_action"].astype("float").where(s["is_play_action"].notna())
    else:
        s["blitz"] = np.nan
        s["pa"] = np.nan
        s["n_defense_box"] = np.nan
    keys = ["game_id", "season", "week", "season_type", "game_date", "posteam", "defteam", "home_team", "away_team"]
    L = s[s["live"]]
    P, R = L[L["pass"] == 1], L[L["rush"] == 1]
    g = s.groupby(keys)
    out = pd.DataFrame({
        "plays": g.size(),
        "neutral_plays": g["neutral"].sum(),
        "neutral_pass": s[s["neutral"]].groupby(keys)["pass"].sum(),
        "neutral_pass_oe_sum": s[s["neutral"]].groupby(keys)["pass_oe"].sum(),
        "no_huddle": g["no_huddle"].sum(),
    })
    def agg(frame, prefix):
        gg = frame.groupby(keys)
        return pd.DataFrame({f"{prefix}_n": gg.size(), f"{prefix}_epa": gg["epa_w"].sum(),
                             f"{prefix}_succ": gg["success"].sum(), f"{prefix}_expl": gg["explosive"].sum()})
    out = out.join(agg(P, "pass"), how="left").join(agg(R, "rush"), how="left")
    gL = L.groupby(keys)
    out = out.join(pd.DataFrame({
        "dropbacks": L[L["qb_dropback"] == 1].groupby(keys).size(),
        "pressures": gL["pressure"].sum(), "sacks": gL["sack"].sum(),
        "turnovers": gL["turnover"].sum(),
        "early_epa": L[L["down"].isin([1, 2])].groupby(keys)["epa_w"].sum(),
        "early_n": L[L["down"].isin([1, 2])].groupby(keys).size(),
        "rz_n": L[L["yardline_100"] <= 20].groupby(keys).size(),
        "rz_td": L[L["yardline_100"] <= 20].groupby(keys)["touchdown"].sum(),
        "blitzed_n": L[(L["qb_dropback"] == 1) & (L["blitz"] == 1)].groupby(keys).size(),
        "blitzed_epa": L[(L["qb_dropback"] == 1) & (L["blitz"] == 1)].groupby(keys)["epa_w"].sum(),
        "charted_db": L[(L["qb_dropback"] == 1) & L["blitz"].notna()].groupby(keys).size(),
        "pa_n": L[(L["pass"] == 1) & (L["pa"] == 1)].groupby(keys).size(),
        "pa_epa": L[(L["pass"] == 1) & (L["pa"] == 1)].groupby(keys)["epa_w"].sum(),
        "charted_pass": L[(L["pass"] == 1) & L["pa"].notna()].groupby(keys).size(),
        "heavy_box_rush_n": L[(L["rush"] == 1) & (L["n_defense_box"] >= 8)].groupby(keys).size(),
        "charted_rush": L[(L["rush"] == 1) & L["n_defense_box"].notna()].groupby(keys).size(),
    }), how="left")
    # special teams: EPA as the team with the ball on FG / punt / kickoff-return plays
    st = df[(df["special_teams_play"] == 1) & df["epa"].notna() & df["posteam"].notna()]
    out = out.join(st.groupby(keys)["epa"].sum().rename("st_epa"), how="left")
    fg = df[df["play_type"] == "field_goal"]
    out = out.join(fg.groupby(keys).agg(fg_att=("field_goal_result", "size"),
                                        fg_made=("field_goal_result", lambda x: (x == "made").sum())), how="left")
    out = out.fillna(0).reset_index()
    return out


def qb_game(df):
    d = df[(df["qb_dropback"] == 1) & df["id"].notna() & df["qb_epa"].notna()
           & (df["wp"] > 0.05) & (df["wp"] < 0.95)].copy()
    d["qb_epa_w"] = d["qb_epa"].clip(-4.5, 4.5)
    g = d.groupby(["game_id", "season", "week", "game_date", "posteam", "defteam", "id"])
    out = pd.DataFrame({"dropbacks": g.size(), "qb_epa": g["qb_epa_w"].sum(),
                        "cpoe_sum": g["cpoe"].sum(), "cpoe_n": g["cpoe"].count()}).reset_index()
    names = df[df["id"].notna() & (df["qb_dropback"] == 1)].groupby("id")["passer_player_name"].agg(
        lambda x: x.dropna().mode().iat[0] if x.notna().any() else None) if "passer_player_name" in df else None
    if names is not None:
        out["name"] = out["id"].map(names)
    return out.rename(columns={"posteam": "team", "defteam": "opp", "id": "qb_id"})


def build(pbp_files, ftn_folder):
    teams, qbs = [], []
    for f in sorted(pbp_files):
        df = load_pbp(f)
        season = int(df["season"].iloc[0])
        ftn = load_ftn(season, ftn_folder) if season >= 2022 else None
        teams.append(team_game(df, ftn))
        if "passer_player_name" not in df:
            import pyarrow.parquet as pq
            df["passer_player_name"] = pq.read_table(f, columns=["passer_player_name"]).to_pandas()["passer_player_name"]
        qbs.append(qb_game(df))
        print(f"{Path(f).name}: {len(teams[-1])} team-games, {len(qbs[-1])} qb-games")
    T, Q = pd.concat(teams), pd.concat(qbs)
    DERIVED.mkdir(parents=True, exist_ok=True)
    for new, path in ((T, TEAM_OUT), (Q, QB_OUT)):
        if path.exists():
            old = pd.read_parquet(path)
            new = pd.concat([old[~old["season"].isin(new["season"].unique())], new])
        new.sort_values(["season", "week", "game_id"]).to_parquet(path, index=False)
    print(f"wrote {TEAM_OUT.name}, {QB_OUT.name}")


def refresh_current(season):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / f"pbp_{season}.parquet"
        p.write_bytes(requests.get(f"{REL}/pbp/play_by_play_{season}.parquet", timeout=180).content)
        build([p], d)


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(list(Path(sys.argv[2]).glob("pbp_*.parquet")), sys.argv[3])
    else:
        refresh_current(int(sys.argv[2]))
