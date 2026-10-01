"""NFL team model: opponent-adjusted unit ratings, QB ratings, matchups, situational -> spread and total.

Ratings (refit before every week, using only earlier games):
  Each unit is a weighted ridge regression on per-game cells:  stat = league mean + offense team + defense team
  + home edge, weighted by plays x recency (half-life HALF_LIFE weeks; the offseason counts as a 12-week gap),
  and pulled toward average by a ridge penalty (more for noisier units).
  Units: pass EPA/play (QB separated out), rush EPA/play, success rates, pressure rate, explosive rate,
  turnover rate, pace (plays), special teams (net EPA/game), and offensive pass tendency.
QBs: each QB's own opponent-adjusted EPA per dropback over a long window, shrunk toward replacement level
  by sample size. A team's pass offense = its non-QB pass rating + the QB expected to start.
Game model: expected EPA per game for each offense (plays x per-play EPA, mixing pass/rush by the offense's
  tendency) + special teams -> expected margin; plus matchup and situational terms that earn their place in
  the walk-forward backtest. Totals from both offenses' expected output, pace and weather.
"""
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from stadiums import HOME_STADIUM, STADIUMS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DERIVED = ROOT / "data" / "derived"
GAMES_CSV = ROOT / "data" / "raw" / "nflverse" / "games.csv"
TEAM_FIX = {"OAK": "LV", "SD": "LAC", "STL": "LA", "LAR": "LA"}

HALF_LIFE = 10.0          # weeks; tuned on 2015-2019 only (see tune())
QB_HALF_LIFE = 40.0
QB_PRIOR_DB = 250.0       # dropbacks of "replacement level" mixed into every QB rating
OFFSEASON_GAP = 12
LAMBDA = {"pass": 600.0, "rush": 900.0, "pass_sr": 600.0, "rush_sr": 900.0, "press": 400.0, "expl": 800.0,
          "tov": 2000.0, "plays": 8.0, "proe": 300.0}


def tindex(season, week):
    return season * (18 + OFFSEASON_GAP) + week


# ------------------------------------------------------------------ data
def load_games():
    rows = []
    with open(GAMES_CSV) as f:
        for r in csv.DictReader(f):
            if int(r["season"]) < 2013:
                continue
            fx = lambda t: TEAM_FIX.get(t, t)  # noqa: E731
            num = lambda k: float(r[k]) if r.get(k) not in ("", None, "NA") else None  # noqa: E731
            rows.append({
                "game_id": r["game_id"], "season": int(r["season"]), "week": int(r["week"]),
                "game_type": r["game_type"], "gameday": r["gameday"], "gametime": r["gametime"],
                "weekday": r["weekday"], "home": fx(r["home_team"]), "away": fx(r["away_team"]),
                "result": num("result"), "total": num("total"), "spread_line": num("spread_line"),
                "total_line": num("total_line"), "home_rest": num("home_rest"), "away_rest": num("away_rest"),
                "neutral": r["location"] == "Neutral", "div_game": r["div_game"] == "1", "roof": r["roof"],
                "temp": num("temp"), "wind": num("wind"), "stadium_id": r["stadium_id"],
                "home_qb_id": r["home_qb_id"], "away_qb_id": r["away_qb_id"],
                "home_qb_name": r["home_qb_name"], "away_qb_name": r["away_qb_name"],
            })
    g = pd.DataFrame(rows)
    g["t"] = [tindex(s, w) for s, w in zip(g["season"], g["week"])]
    return g.sort_values(["t", "gameday", "game_id"]).reset_index(drop=True)


def load_cells():
    T = pd.read_parquet(DERIVED / "nfl_team_game.parquet")
    Q = pd.read_parquet(DERIVED / "nfl_qb_game.parquet")
    T["t"] = [tindex(s, w) for s, w in zip(T["season"], T["week"])]
    Q["t"] = [tindex(s, w) for s, w in zip(Q["season"], Q["week"])]
    T["off_home"] = (T["posteam"] == T["home_team"]).astype(float)
    Q["off_home"] = [1.0 if tm == gid.split("_")[3] else 0.0 for tm, gid in zip(Q["team"], Q["game_id"])]
    return T, Q


# ------------------------------------------------------------------ ridge on cells
class Ridge:
    """target_per_unit = mu + O[off] + D[def] + h*home (+ fixed offset), weights = n * recency."""

    def __init__(self, teams):
        self.teams = teams
        self.idx = {t: i for i, t in enumerate(teams)}

    def fit(self, off, dfn, home, y, w, lam, offset=None):
        k = len(self.teams)
        n = len(y)
        X = np.zeros((n, 2 + 2 * k))
        X[:, 0] = 1.0
        X[:, 1] = home
        oi = np.array([self.idx[o] for o in off])
        di = np.array([self.idx[d] for d in dfn])
        X[np.arange(n), 2 + oi] = 1.0
        X[np.arange(n), 2 + k + di] = 1.0
        yy = y - (offset if offset is not None else 0.0)
        W = w
        A = X.T @ (X * W[:, None])
        A[2:, 2:] += lam * np.eye(2 * k)
        b = X.T @ (W * yy)
        beta = np.linalg.solve(A + 1e-9 * np.eye(A.shape[0]), b)
        self.mu, self.h = beta[0], beta[1]
        self.O = dict(zip(self.teams, beta[2:2 + k]))
        self.D = dict(zip(self.teams, beta[2 + k:]))
        return self


def recency(t_rows, t_now, half_life):
    return 0.5 ** ((t_now - t_rows) / half_life)


# ------------------------------------------------------------------ weekly ratings
def ratings_at(T, Q, t_now, teams, prev_qb=None):
    """All unit ratings using games strictly before t_now."""
    tw = T[(T["t"] < t_now) & (T["t"] >= t_now - 4 * (18 + OFFSEASON_GAP))]
    if len(tw) < 200:
        return None
    rw = recency(tw["t"].values, t_now, HALF_LIFE)
    off, dfn, home = tw["posteam"].values, tw["defteam"].values, tw["off_home"].values
    R = {}

    def unit(name, num, den, extra_w=1.0, offset=None):
        n = np.maximum(tw[den].values.astype(float), 0)
        ok = n > 0
        y = np.where(ok, tw[num].values / np.where(ok, n, 1), 0.0)
        m = Ridge(teams).fit(off[ok], dfn[ok], home[ok], y[ok], (n * rw * extra_w)[ok], LAMBDA[name],
                             None if offset is None else offset[ok])
        R[name] = m
        return m

    # QB ratings (long window) and QB offsets for the pass model
    qw = Q[(Q["t"] < t_now) & (Q["t"] >= t_now - 6 * (18 + OFFSEASON_GAP))]
    qb_rating = dict(prev_qb or {})
    for _ in range(2):
        # pass model on QB-game cells with QB offset
        qrw = recency(qw["t"].values, t_now, HALF_LIFE)
        y = (qw["qb_epa"] / qw["dropbacks"]).values
        offs = np.array([qb_rating.get(q, REPLACEMENT_QB) for q in qw["qb_id"]])
        m = Ridge(teams).fit(qw["team"].values, qw["opp"].values, qw["off_home"].values, y,
                             (qw["dropbacks"] * qrw).values, LAMBDA["pass"], offs)
        R["pass"] = m
        # QB = shrunk residual over the long window
        res = y - m.mu - np.array([m.O[t] for t in qw["team"]]) - np.array([m.D[d] for d in qw["opp"]]) \
            - m.h * qw["off_home"].values
        lw = recency(qw["t"].values, t_now, QB_HALF_LIFE) * qw["dropbacks"].values
        df = pd.DataFrame({"q": qw["qb_id"].values, "r": res * lw, "w": lw})
        agg = df.groupby("q").sum()
        qb_rating = ((agg["r"] + QB_PRIOR_DB * REPLACEMENT_QB) / (agg["w"] + QB_PRIOR_DB)).to_dict()
        qb_sample = agg["w"].to_dict()
    R["qb"] = qb_rating
    R["qb_sample"] = qb_sample
    # QB actually used recently by each team (dropback-weighted), to swap starters later
    recent = qw[qw["t"] >= t_now - 3]
    used = defaultdict(float)
    tot = defaultdict(float)
    for tm, q, db, tt in zip(qw["team"], qw["qb_id"], qw["dropbacks"], qw["t"]):
        wgt = db * 0.5 ** ((t_now - tt) / HALF_LIFE)
        used[tm] += wgt * qb_rating.get(q, REPLACEMENT_QB)
        tot[tm] += wgt
    R["qb_used"] = {tm: used[tm] / tot[tm] for tm in tot}
    R["last_qb"] = (recent.sort_values("t").groupby("team").apply(
        lambda d: d.loc[d["dropbacks"].idxmax(), "qb_id"], include_groups=False).to_dict() if len(recent) else {})

    unit("rush", "rush_epa", "rush_n")
    unit("pass_sr", "pass_succ", "pass_n")
    unit("rush_sr", "rush_succ", "rush_n")
    unit("press", "pressures", "dropbacks")
    tw = tw.assign(all_n=tw["pass_n"] + tw["rush_n"], all_expl=tw["pass_expl"] + tw["rush_expl"])
    unit("expl", "all_expl", "all_n")
    unit("tov", "turnovers", "all_n")
    # pace: plays per game, offense + defense effects
    m = Ridge(teams).fit(off, dfn, home, tw["plays"].values.astype(float), rw, LAMBDA["plays"])
    R["plays"] = m
    # pass tendency: neutral-situation pass rate over expected (offense only matters; keep D effect too)
    unit("proe", "neutral_pass_oe_sum", "neutral_plays")
    # special teams net per game (own ST EPA minus opponent's), simple shrunk weighted mean
    st = defaultdict(float)
    stw = defaultdict(float)
    opp_st = tw.set_index(["game_id", "posteam"])["st_epa"].to_dict()
    for gid, tm, op, v, wv in zip(tw["game_id"], tw["posteam"], tw["defteam"], tw["st_epa"], rw):
        net = v - opp_st.get((gid, op), 0.0)
        st[tm] += wv * net
        stw[tm] += wv
    R["st"] = {tm: st[tm] / (stw[tm] + 6.0) for tm in teams}
    # FTN-era matchup inputs (2022+): blitz rate faced/sent, QB EPA vs blitz, play-action rate and D vs PA
    ft = tw[tw["charted_db"] > 0]
    if len(ft) > 200:
        fw = recency(ft["t"].values, t_now, HALF_LIFE)
        g = pd.DataFrame({"off": ft["posteam"], "def": ft["defteam"], "w": fw,
                          "bl_n": ft["blitzed_n"] * fw, "db": ft["charted_db"] * fw,
                          "bl_epa": ft["blitzed_epa"] * fw,
                          "pa_n": ft["pa_n"] * fw, "pa_epa": ft["pa_epa"] * fw, "cp": ft["charted_pass"] * fw,
                          "pass_epa": ft["pass_epa"] * fw, "pass_n": ft["pass_n"] * fw})
        o, d = g.groupby("off").sum(numeric_only=True), g.groupby("def").sum(numeric_only=True)
        k = 60.0
        R["blitz_rate_def"] = ((d["bl_n"] + k * 0.28) / (d["db"] + k)).to_dict()
        lg_pass = g["pass_epa"].sum() / g["pass_n"].sum()
        lg_bl = g["bl_epa"].sum() / max(1e-9, g["bl_n"].sum())
        R["vs_blitz_off"] = (((o["bl_epa"] + k * lg_bl) / (o["bl_n"] + k))
                             - ((o["pass_epa"] + k * lg_pass) / (o["pass_n"] + k))).to_dict()
        R["pa_rate_off"] = ((o["pa_n"] + k * 0.25) / (o["cp"] + k)).to_dict()
        lg_pa = g["pa_epa"].sum() / max(1e-9, g["pa_n"].sum())
        R["vs_pa_def"] = (((d["pa_epa"] + k * lg_pa) / (d["pa_n"] + k))
                          - ((d["pass_epa"] + k * lg_pass) / (d["pass_n"] + k))).to_dict()
    return R


REPLACEMENT_QB = -0.06   # EPA/dropback vs the team's non-QB pass rating for an unknown QB


# ------------------------------------------------------------------ game features
def travel(g, side):
    team = g[side]
    home_st = STADIUMS.get(HOME_STADIUM.get(team, ""), None)
    game_st = STADIUMS.get(g["stadium_id"], None)
    if not home_st or not game_st:
        return 0.0, 0.0
    la1, lo1, tz1 = home_st
    la2, lo2, tz2 = game_st
    r = math.radians
    d = 3959 * 2 * math.asin(math.sqrt(math.sin(r(la2 - la1) / 2) ** 2 +
                                       math.cos(r(la1)) * math.cos(r(la2)) * math.sin(r(lo2 - lo1) / 2) ** 2))
    dt = datetime.strptime(g["gameday"], "%Y-%m-%d")
    off = lambda tz: ZoneInfo(tz).utcoffset(dt).total_seconds() / 3600  # noqa: E731
    return d, off(tz2) - off(tz1)


def team_side(R, off, dfn, home_flag, qb_id):
    """Expected per-game offensive output for `off` against `dfn`."""
    P, Ru, PL, PR = R["pass"], R["rush"], R["plays"], R["proe"]
    qb_now = R["qb"].get(qb_id, REPLACEMENT_QB) if qb_id else R["qb_used"].get(off, REPLACEMENT_QB)
    qb_swap = qb_now - R["qb_used"].get(off, qb_now)
    pass_epa = P.mu + P.O[off] + R["qb_used"].get(off, 0) + qb_swap + P.D[dfn] + P.h * home_flag
    rush_epa = Ru.mu + Ru.O[off] + Ru.D[dfn] + Ru.h * home_flag
    pass_rate = min(0.75, max(0.45, 0.585 + PR.O[off]))
    plays = PL.mu + PL.O[off] + PL.D[dfn] + PL.h * home_flag
    epa_play = pass_rate * pass_epa + (1 - pass_rate) * rush_epa
    out = {"pass_epa": pass_epa, "rush_epa": rush_epa, "pass_rate": pass_rate, "plays": plays,
           "epa_play": epa_play, "epa_game": plays * epa_play, "qb_now": qb_now, "qb_swap": qb_swap,
           "press": R["press"].mu + R["press"].O[off] + R["press"].D[dfn],
           "press_int": R["press"].O[off] * R["press"].D[dfn],
           "expl_int": R["expl"].O[off] * R["expl"].D[dfn],
           "tov": R["tov"].mu + R["tov"].O[off] + R["tov"].D[dfn],
           "mix_int": (pass_rate - 0.585) * ((P.D[dfn]) - (Ru.D[dfn])),
           "sr": pass_rate * (R["pass_sr"].O[off] + R["pass_sr"].D[dfn]) +
                 (1 - pass_rate) * (R["rush_sr"].O[off] + R["rush_sr"].D[dfn])}
    if "blitz_rate_def" in R:
        out["blitz_int"] = R["blitz_rate_def"].get(dfn, 0.28) * R["vs_blitz_off"].get(off, 0.0)
        out["pa_int"] = R["pa_rate_off"].get(off, 0.25) * R["vs_pa_def"].get(dfn, 0.0)
    else:
        out["blitz_int"] = out["pa_int"] = 0.0
    return out


def _v(x, default):
    return default if x is None or (isinstance(x, float) and math.isnan(x)) else x


def game_row(g, R):
    g = {k: v for k, v in g.items()}
    for k, d in (("wind", None), ("temp", None), ("home_rest", 7.0), ("away_rest", 7.0)):
        g[k] = _v(g.get(k), d)
    hf = 0.0 if g["neutral"] else 1.0
    h = team_side(R, g["home"], g["away"], hf, g["home_qb_id"] or None)
    a = team_side(R, g["away"], g["home"], 0.0, g["away_qb_id"] or None)
    dist_a, tz_a = travel(g, "away")
    dist_h, tz_h = travel(g, "home")
    hour = int((g["gametime"] or "13:00").split(":")[0])
    dome = g["roof"] in ("dome", "closed")
    row = {
        "game_id": g["game_id"], "season": g["season"], "week": g["week"], "t": g["t"],
        "home": g["home"], "away": g["away"], "result": g["result"], "total": g["total"],
        "spread_line": g["spread_line"], "total_line": g["total_line"], "neutral": g["neutral"],
        # margin features (home minus away)
        "epa_diff": h["epa_game"] - a["epa_game"],
        "st_diff": R["st"][g["home"]] - R["st"][g["away"]],
        "sr_diff": h["sr"] - a["sr"],
        "tov_diff": a["tov"] - h["tov"],
        "press_int": a["press_int"] - h["press_int"],
        "expl_int": h["expl_int"] - a["expl_int"],
        "mix_int": h["mix_int"] - a["mix_int"],
        "blitz_int": h["blitz_int"] - a["blitz_int"],
        "pa_int": h["pa_int"] - a["pa_int"],
        "home_field": hf,
        "rest_diff": (g["home_rest"] or 7) - (g["away_rest"] or 7),
        "home_bye": 1.0 if (g["home_rest"] or 7) >= 13 else 0.0,
        "away_bye": 1.0 if (g["away_rest"] or 7) >= 13 else 0.0,
        "away_tz_shift": tz_a, "away_miles": dist_a / 1000,
        "west_to_east_early": 1.0 if tz_a >= 2 and hour <= 13 else 0.0,
        "div_game": 1.0 if g["div_game"] else 0.0,
        "qb_swap_diff": h["qb_swap"] - a["qb_swap"],
        # total features
        "epa_sum": h["epa_game"] + a["epa_game"],
        "plays_sum": h["plays"] + a["plays"],
        "pass_rate_avg": (h["pass_rate"] + a["pass_rate"]) / 2,
        "st_sum": R["st"][g["home"]] + R["st"][g["away"]],
        "dome": 1.0 if dome else 0.0,
        "wind": 0.0 if dome else (g["wind"] if g["wind"] is not None else 8.0),  # 8 mph = typical outdoor
        "cold": 0.0 if dome or g["temp"] is None else max(0.0, 40 - g["temp"]),
        "home_qb": g["home_qb_name"], "away_qb": g["away_qb_name"],
        "h_pass_epa": h["pass_epa"], "a_pass_epa": a["pass_epa"], "h_rush_epa": h["rush_epa"],
        "a_rush_epa": a["rush_epa"], "h_plays": h["plays"], "a_plays": a["plays"],
        "h_press": h["press"], "a_press": a["press"], "h_qb": h["qb_now"], "a_qb": a["qb_now"],
    }
    return row


def build_features(games=None, T=None, Q=None, start_season=2015):
    games = load_games() if games is None else games
    if T is None:
        T, Q = load_cells()
    teams = sorted(set(T["posteam"]) | set(T["defteam"]))
    rows, cache, prev_qb = [], {}, None
    for t_now, wk in games[games["season"] >= start_season].groupby("t"):
        R = ratings_at(T, Q, t_now, teams, prev_qb)
        if R is None:
            continue
        prev_qb = R["qb"]
        cache[t_now] = R
        for _, g in wk.iterrows():
            if g["home"] in R["pass"].O and g["away"] in R["pass"].O:
                rows.append(game_row(g, R))
    return pd.DataFrame(rows), cache


# ------------------------------------------------------------------ walk-forward models
MARGIN_BASE = ["epa_diff", "st_diff", "home_field"]
MARGIN_EXTRA = ["sr_diff", "tov_diff", "press_int", "expl_int", "mix_int", "rest_diff", "home_bye", "away_bye",
                "away_tz_shift", "away_miles", "west_to_east_early", "div_game"]
FTN_EXTRA = ["blitz_int", "pa_int"]
TOTAL_BASE = ["epa_sum", "plays_sum", "st_sum", "dome", "wind", "cold"]


def ols_fit(X, y, ridge=1e-6):
    X1 = np.column_stack([np.ones(len(X)), X])
    A = X1.T @ X1 + ridge * np.eye(X1.shape[1])
    A[0, 0] -= ridge
    return np.linalg.solve(A, X1.T @ y)


def ols_pred(beta, X):
    return beta[0] + X @ beta[1:]


def fit_standardized(Xtr, ytr, ridge):
    mu, sd = Xtr.mean(0), Xtr.std(0)
    sd[sd == 0] = 1.0
    beta = ols_fit((Xtr - mu) / sd, ytr, ridge)
    return {"mu": mu, "sd": sd, "beta": beta}


def predict_standardized(m, X):
    return ols_pred(m["beta"], (X - m["mu"]) / m["sd"])


def walk_forward(F, feats, target, line, seasons, train_from=2015, min_week=1, ridge=20.0):
    out = []
    for s in seasons:
        tr = F[(F["season"] >= train_from) & (F["season"] < s) & F[target].notna() & (F["week"] >= min_week)]
        te = F[(F["season"] == s) & F[target].notna() & (F["week"] >= min_week)]
        if len(tr) < 200 or not len(te):
            continue
        m = fit_standardized(tr[feats].values.astype(float), tr[target].values.astype(float), ridge)
        out.append(te.assign(pred=predict_standardized(m, te[feats].values.astype(float))))
    return pd.concat(out) if out else pd.DataFrame()


def evaluate(P, target, line, side_fn):
    """RMSE vs result, information beyond the line, ATS/OU record by disagreement size."""
    P = P[P[line].notna()]
    y, p, ln = P[target].values, P["pred"].values, P[line].values
    rm = lambda a: float(np.sqrt(np.mean((a - y) ** 2)))  # noqa: E731
    X = np.column_stack([ln, p - ln])
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    # bootstrap the weight's standard error
    rng = np.random.default_rng(0)
    bs = []
    for _ in range(200):
        i = rng.integers(0, len(y), len(y))
        bs.append(np.linalg.lstsq(X[i], y[i], rcond=None)[0][1])
    res = {"games": int(len(y)), "rmse_model": round(rm(p), 2), "rmse_line": round(rm(ln), 2),
           "weight_on_model_minus_line": round(float(b[1]), 3), "weight_se": round(float(np.std(bs)), 3)}
    for t in (1, 2, 3, 4, 5):
        w = l = 0
        for yy, pp, ll in zip(y, p, ln):
            if abs(pp - ll) < t:
                continue
            v = side_fn(yy, pp, ll)
            w += v > 0
            l += v < 0
        res[f"ats_{t}"] = f"{w}-{l} ({w / (w + l):.3f})" if w + l else "-"
    return res


def spread_side(y, p, ln):
    return (1 if p > ln else -1) * (y - ln)


def total_side(y, p, ln):
    return (1 if p > ln else -1) * (y - ln)




# ------------------------------------------------------------------ live
MARGIN_FEATS = MARGIN_BASE + ["sr_diff", "tov_diff"]
TOTAL_FEATS = ["epa_sum", "plays_sum", "wind", "cold"]
# How far to trust a disagreement with the market (from the 2017-25 walk-forward vs closing lines):
# under ~3 pts there is no edge; 4+ pts covered ~55% (spreads) and 4-6 pts ~56% (totals).
TRUST = {"floor": 2.0, "slope": 0.5, "cap": 1.75, "total_max_gap": 6.0}


def trusted_shift(gap):
    """Points to move the fair line toward the model, given model minus market."""
    a = abs(gap)
    if a <= TRUST["floor"]:
        return 0.0
    return math.copysign(min(TRUST["cap"], TRUST["slope"] * (a - TRUST["floor"])), gap)


def team_notes(R, row, home, away):
    """Plain-English matchup edges from the unit ratings (per-play EPA vs league average)."""
    P, Ru = R["pass"], R["rush"]
    notes = []
    for off, dfn, pe, re_ in ((home, away, row["h_pass_epa"], row["h_rush_epa"]),
                              (away, home, row["a_pass_epa"], row["a_rush_epa"])):
        pdiff = pe - P.mu
        rdiff = re_ - Ru.mu
        if abs(pdiff) >= 0.06:
            notes.append((abs(pdiff), f"{off} passing vs {dfn} pass D: {pdiff:+.2f} EPA/play vs average"))
        if abs(rdiff) >= 0.05:
            notes.append((abs(rdiff), f"{off} run game vs {dfn} run D: {rdiff:+.2f} EPA/play vs average"))
    for side, tm in (("h", home), ("a", away)):
        p = row[f"{side}_press"]
        if p >= 0.17:
            notes.append((p - 0.12, f"{tm} QB faces pressure on ~{p:.0%} of dropbacks"))
    notes.sort(key=lambda x: -x[0])
    return [n for _, n in notes[:3]]


def apply_qb_injuries(games, Q, season):
    """If the projected starter is listed Out/Doubtful, start the team's most-used other QB instead."""
    import re
    norm = lambda n: re.sub(r"[^a-z]", "", (n or "").lower())  # noqa: E731
    path = DERIVED / "nfl_injuries.json"
    if not path.exists():
        return games, []
    inj = json.loads(path.read_text())["teams"]
    swaps = []
    recent = Q[Q["season"] >= season - 1]
    for i, g in games[(games["season"] == season) & games["result"].isna()].iterrows():
        for side in ("home", "away"):
            team, name = g[side], g[f"{side}_qb_name"]
            hurt = [a for a in inj.get(team, {}).get("absences", [])
                    if a["pos"] == "QB" and a["status"] in ("Out", "Doubtful", "Injured Reserve")]
            if not hurt or not any(norm(a["player"]) == norm(name) for a in hurt):
                continue
            others = recent[(recent["team"] == team) & (recent["qb_id"] != g[f"{side}_qb_id"])]
            if not len(others):
                continue
            backup = others.sort_values(["t", "dropbacks"]).iloc[-1]
            games.at[i, f"{side}_qb_id"] = backup["qb_id"]
            games.at[i, f"{side}_qb_name"] = backup.get("name") or "backup QB"
            swaps.append(f"{team}: {name} out -> {games.at[i, side + '_qb_name']}")
    return games, sorted(set(swaps))


def live(season):
    games = load_games()
    T, Q = load_cells()
    games, swaps = apply_qb_injuries(games, Q, season)
    for sw in swaps:
        print(f"::notice::QB swap {sw}")
    hist = pd.read_parquet(DERIVED / "nfl_features.parquet")
    hist = hist[hist["season"] < season]
    cur, cache = build_features(games, T, Q, start_season=season)
    F = pd.concat([hist, cur[cur["result"].notna()]])
    mm = fit_standardized(F[MARGIN_FEATS].values.astype(float), F["result"].values.astype(float), 20.0)
    ft = F[F["total"].notna()]
    tm = fit_standardized(ft[TOTAL_FEATS].values.astype(float), ft["total"].values.astype(float), 20.0)
    up = cur[cur["result"].isna()].copy()
    out = {}
    for _, r in up.iterrows():
        R = cache[r["t"]]
        x = r[MARGIN_FEATS].values.astype(float)[None, :]
        xt = r[TOTAL_FEATS].values.astype(float)[None, :]
        out[r["game_id"]] = {
            "home": r["home"], "away": r["away"], "week": int(r["week"]),
            "model_margin": round(float(predict_standardized(mm, x)[0]), 2),
            "model_total": round(float(predict_standardized(tm, xt)[0]), 2),
            "home_qb": r["home_qb"], "away_qb": r["away_qb"],
            "home_qb_rating": round(r["h_qb"], 3), "away_qb_rating": round(r["a_qb"], 3),
            "wind": r["wind"], "dome": bool(r["dome"]),
            "notes": team_notes(R, r, r["home"], r["away"]),
        }
    # team table for reference / the page
    last_t = max(cache)
    R = cache[last_t]
    teams = sorted(R["pass"].O)
    table = []
    for t in teams:
        table.append({"team": t, "pass_off": round(R["pass"].O[t] + R["qb_used"].get(t, 0), 3),
                      "rush_off": round(R["rush"].O[t], 3), "pass_def": round(R["pass"].D[t], 3),
                      "rush_def": round(R["rush"].D[t], 3), "st": round(R["st"][t], 2),
                      "plays": round(R["plays"].O[t], 1)})
    res = {"built_at": datetime.utcnow().isoformat() + "Z", "season": season, "trust": TRUST,
           "games": out, "teams": table, "qb": {q: round(v, 3) for q, v in R["qb"].items()}}
    (DERIVED / "nfl_model_live.json").write_text(json.dumps(res, indent=1))
    print(f"::notice::nfl model: {len(out)} upcoming games priced")
    return res


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "live":
        live(int(sys.argv[2]))
    else:
        F, cache = build_features()
        F.to_parquet(DERIVED / "nfl_features.parquet", index=False)
        print(f"features: {len(F)} games")
