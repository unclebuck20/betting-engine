"""Grade every model pick and every bet you logged: closing line, closing-line value, result, units.

Closing line = last odds snapshot before kickoff. CLV is measured two ways:
  clv_pts     your number minus the closing fair number (from your side; positive = you beat the close)
  clv_ev_pct  expected value of your exact bet (line + price) under the closing fair line
The closing fair line is the best available estimate of the true price, so clv_ev_pct is the number
that says whether a pick was good, long before win/loss has enough games to mean anything.
"""
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import edge as E  # noqa: E402
from common import MODEL_DIR, RAW, CFBNames, load_snapshot, parse_ts, snapshot_paths  # noqa: E402
from picks import LOG_FIELDS, NFL_ABBR  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MODEL_LOG = ROOT / "data" / "picks" / "model_log.csv"
BETS = ROOT / "data" / "bets.csv"
PAGE = ROOT / "docs" / "data"
ET = ZoneInfo("America/New_York")
BET_FIELDS = ["logged_at", "issue", "pick_key", "market", "pick", "matchup", "kickoff_utc", "league", "book", "line", "price",
              "units", "model_tier", "model_ev_pct", "close_fair_line", "close_line", "clv_pts", "clv_ev_pct",
              "home_score", "away_score", "result", "units_won"]


def read(path):
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def write(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


class Closer:
    def __init__(self, models):
        self.models = models
        self.snaps = {lg: [(p, None) for p in snapshot_paths(lg)] for lg in ("nfl", "cfb")}
        self._cache = {}

    def _snap(self, league, i):
        key = (league, i)
        if key not in self._cache:
            self._cache[key] = load_snapshot(self.snaps[league][i][0])
        return self._cache[key]

    def closing(self, league, event_id, kickoff, totals=False):
        """last snapshot before kickoff that has this game -> (lines, pulled_at)"""
        for i in range(len(self.snaps[league]) - 1, -1, -1):
            name = self.snaps[league][i][0].stem          # 20261003T1500Z
            ts = datetime.strptime(name, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
            if ts >= kickoff:            # a snapshot at kickoff may already be in-play odds
                continue
            if kickoff - ts > timedelta(days=4):
                return None, None
            s = self._snap(league, i)
            e = next((x for x in s["events"] if x["id"] == event_id), None)
            if e and e["books"]:
                lines = E.total_lines(e) if totals else E.book_lines(e)
                if lines:
                    return lines, s["pulled_at"]
        return None, None


def scores():
    """final scores keyed by league -> list of (kickoff date ET, home, away, home_pts, away_pts)"""
    out = defaultdict(list)
    with open(RAW / "nflverse" / "games.csv") as f:
        for r in csv.DictReader(f):
            if r["home_score"] and r["season"] >= "2026":
                out["nfl"].append((r["gameday"], r["home_team"], r["away_team"], int(r["home_score"]),
                                   int(r["away_score"])))
    for p in (RAW / "cfbd").glob("week_*_games.json"):
        for g in json.loads(p.read_text()):
            if g.get("homePoints") is not None and g.get("startDate"):
                d = parse_ts(g["startDate"]).astimezone(ET).strftime("%Y-%m-%d")
                out["cfb"].append((d, g["homeTeam"], g["awayTeam"], g["homePoints"], g["awayPoints"]))
    return out


def find_score(sc, league, matchup, kickoff, names):
    away, home = matchup.split(" @ ")
    if league == "nfl":
        h, a = NFL_ABBR.get(home), NFL_ABBR.get(away)
    else:
        h, a = names.school(home), names.school(away)
    days = {(kickoff.astimezone(ET) + timedelta(days=d)).strftime("%Y-%m-%d") for d in (-1, 0, 1)}
    for d, hh, aa, hp, ap in sc[league]:
        if d in days and hh == h and aa == a:
            return hp, ap
    for d, hh, aa, hp, ap in sc[league]:       # neutral sites: sources can disagree on who is "home"
        if d in days and hh == a and aa == h:
            return ap, hp
    return None


def grade_row(r, closer, sc, names, models, now):
    if r.get("result") and r["result"] != "pending":
        return False
    kickoff = parse_ts(r["kickoff_utc"])
    if now < kickoff + timedelta(hours=4):
        return False
    league = r["league"]
    away, home = r["matchup"].split(" @ ")
    pick_side = r.get("side") or r["pick"].rsplit(" ", 2)[0]
    is_total = r.get("market") == "total" or pick_side in ("Over", "Under")
    line, price = float(r["line"]), int(float(r["price"]))
    changed = False
    if is_total:
        over = pick_side == "Over"
        hs, home_line, m = ("home" if over else "away"), -line, models["nfl_total"]
    else:
        is_home = (str(r.get("side_is_home", "")).lower() == "true") if r.get("side_is_home") else pick_side == home
        sgn = 1 if is_home else -1
        hs, home_line, m = ("home" if is_home else "away"), line * sgn, models[league]
    if not r.get("close_fair_line"):
        lines, _ = closer.closing(league, r["pick_key"].split("|")[0], kickoff, totals=is_total)
        if lines:
            _, books = E.fair_source(lines)
            if is_total:
                mu = E.fair_total(m, lines, books)
                r["close_fair_line"] = round(mu, 1)
                bl = lines.get(r["book"])
                r["close_line"] = bl["line"] if bl else ""
                r["clv_pts"] = round((mu - line) if over else (line - mu), 1)
            else:
                mu = E.fair_mu(m, lines, books)
                close_fair = -mu * sgn
                r["close_fair_line"] = round(close_fair, 1)
                bl = lines.get(r["book"])
                r["close_line"] = (bl["home_line"] * sgn) if bl else ""
                r["clv_pts"] = round(line - close_fair, 1)
            r["clv_ev_pct"] = round(100 * E.ev(m.dist(mu), home_line, hs, price), 2)
            changed = True
    s = find_score(sc, league, r["matchup"], kickoff, names)
    if s:
        hp, ap = s
        r["home_score"], r["away_score"] = hp, ap
        if is_total:
            v = (hp + ap - line) * (1 if over else -1)
        else:
            v = (hp - ap) * sgn + line
        units = float(r.get("units") or 0)
        r["result"] = "win" if v > 0 else "push" if v == 0 else "loss"
        r["units_won"] = round(units * (E.dec(price) - 1), 2) if v > 0 else 0.0 if v == 0 else -units
        changed = True
    elif now > kickoff + timedelta(days=5):
        r["result"] = "no_score"
        changed = True
    return changed


def summarize(rows, units_key="units"):
    g = [r for r in rows if r.get("result") in ("win", "loss", "push")]
    c = [r for r in rows if r.get("clv_ev_pct") not in (None, "")]
    risked = sum(float(r.get(units_key) or 0) for r in g)
    won = sum(float(r.get("units_won") or 0) for r in g)
    return {
        "n": len(rows), "graded": len(g),
        "w": sum(r["result"] == "win" for r in g), "l": sum(r["result"] == "loss" for r in g),
        "p": sum(r["result"] == "push" for r in g),
        "units_won": round(won, 2), "roi_pct": round(100 * won / risked, 1) if risked else None,
        "clv_n": len(c),
        "avg_clv_ev_pct": round(sum(float(r["clv_ev_pct"]) for r in c) / len(c), 2) if c else None,
        "avg_clv_pts": round(sum(float(r["clv_pts"]) for r in c) / len(c), 2) if c else None,
        "beat_close_pct": round(100 * sum(float(r["clv_pts"]) > 0 for r in c) / len(c), 1) if c else None,
    }


def main():
    now = datetime.now(timezone.utc)
    models = E.models()
    closer, sc, names = Closer(models), scores(), CFBNames()
    model_rows, bet_rows = read(MODEL_LOG), read(BETS)
    n_model = sum(grade_row(r, closer, sc, names, models, now) for r in model_rows)
    n_bets = sum(grade_row(r, closer, sc, names, models, now) for r in bet_rows)
    if model_rows:
        write(MODEL_LOG, model_rows, LOG_FIELDS)
    if bet_rows:
        write(BETS, bet_rows, BET_FIELDS)
    PAGE.mkdir(parents=True, exist_ok=True)
    (PAGE / "bets.json").write_text(json.dumps([{k: r.get(k, "") for k in BET_FIELDS} for r in bet_rows]))

    published = {r["pick_key"] for r in bet_rows}
    record = {
        "built_at": now.isoformat(),
        "mine": {"all": summarize(bet_rows),
                 "nfl": summarize([r for r in bet_rows if r["league"] == "nfl"]),
                 "cfb": summarize([r for r in bet_rows if r["league"] == "cfb"])},
        "model": {"all": summarize(model_rows),
                  "plays": summarize([r for r in model_rows if r["tier"] == "play"]),
                  "leans": summarize([r for r in model_rows if r["tier"] == "lean"]),
                  "nfl": summarize([r for r in model_rows if r["league"] == "nfl"]),
                  "cfb": summarize([r for r in model_rows if r["league"] == "cfb"]),
                  "you_passed": summarize([r for r in model_rows if r["pick_key"] not in published
                                           and r["tier"] == "play"])},
        "learned": [],
    }
    pf = MODEL_DIR / "params.json"
    if pf.exists():
        st = json.loads(pf.read_text())
        record["learned"] = st.get("changes", [])[-12:]
        record["calibration_note"] = st.get("last_run_note")
        record["calibrated_at"] = st.get("updated_at")
    (PAGE / "record.json").write_text(json.dumps(record, indent=1))
    print(f"::notice::graded: {n_model} model picks, {n_bets} bets updated; "
          f"model CLV {record['model']['all']['avg_clv_ev_pct']}% over {record['model']['all']['clv_n']}")


if __name__ == "__main__":
    main()
