"""Turn market + model + injuries into the nightly cards.

Layer 1 (market):   fair line from the sharpest book, de-vigged (edge.py).
Layer 2 (model):    college -> PPA power ratings, blended in at CFB_MODEL_WEIGHT because the
                    backtest shows the model predicts where lines move (2023-25: 57.7% ATS vs the
                    opener when it disagrees by 5+). NFL -> EPA ratings add nothing beyond the
                    closing line in backtests, so NFL weight is 0: NFL picks are price edges, and
                    the model + injuries only act as a veto when they point the other way.
Sizing:             quarter Kelly on expected value, 0.5u steps, capped.
Every play/lean is appended to data/picks/log.csv the first time it appears (for CLV grading).
"""
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402
import edge as E  # noqa: E402
import cfb_ratings as CR  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW, DERIVED, OUT = ROOT / "data" / "raw", ROOT / "data" / "derived", ROOT / "data" / "picks"

CFB_MODEL_WEIGHT = 0.15   # backtest: 0.30 vs the opener, 0.085 vs the close; midweek sits between
CFB_GAP_CLIP = 10.0       # beyond this the ratings are usually missing a roster/QB change
NFL_MODEL_WEIGHT = 0.0
PLAY_EV, LEAN_EV = 0.025, 0.01
KELLY_FRACTION = 0.25
MAX_UNITS = {"nfl": 3.0, "cfb": 2.0}   # college capped lower until injury reports are wired in
VETO_POINTS = 3.0                       # NFL: model+injuries on the other side by this much -> downgrade

NFL_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LA", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def slot_for(league, kick_pt):
    wd, hr = kick_pt.strftime("%a"), kick_pt.hour
    if wd == "Thu":
        return "thu"
    if wd == "Fri":
        return "fri"
    if wd == "Sat":
        return "sat" if league == "cfb" else None
    if wd == "Sun" and league == "nfl":
        return "snf" if hr >= 17 else "sun"
    if wd == "Mon" and league == "nfl":
        return "mnf"
    return None


def units_for(ev_, price, league, tier):
    if tier == "pass":
        return 0.0
    if tier == "lean":
        return 0.5
    f = ev_ / (E.dec(price) - 1)
    u = round(100 * f * KELLY_FRACTION * 2) / 2
    return max(1.0, min(MAX_UNITS[league], u))


def fmt_line(x):
    return "PK" if x == 0 else f"{x:+g}"


def cfb_open_lines():
    out = {}
    for f in (RAW / "cfbd").glob("week_*_lines.json"):
        for g in json.loads(f.read_text()):
            L = CR.pick_line(g.get("lines"))
            if L and L.get("spreadOpen") is not None:
                out[(g["homeTeam"], g["awayTeam"])] = {"open_home": L["spreadOpen"], "now_home": L["spread"],
                                                       "provider": L["provider"]}
    return out


def cfb_neutral():
    out = {}
    for f in (RAW / "cfbd").glob("week_*_games.json"):
        for g in json.loads(f.read_text()):
            out[(g["homeTeam"], g["awayTeam"])] = bool(g.get("neutralSite"))
    return out


def main():
    now = datetime.now(timezone.utc)
    nfl_model = E.NFLModel(E.load_nfl_history())
    cfb_model = E.CFBModel()
    nfl_r = json.loads((DERIVED / "nfl_ratings.json").read_text())
    nfl_games = {(g["home"], g["away"]): g for g in nfl_r["games"].values()}
    inj = json.loads((DERIVED / "nfl_injuries.json").read_text())["teams"] if (DERIVED / "nfl_injuries.json").exists() else {}
    cfb_L = CR.live_ratings(C.SEASON)
    opens, neutral = cfb_open_lines(), cfb_neutral()

    cards = []
    for league in ("nfl", "cfb"):
        p = RAW / "odds" / league / "latest.json"
        if not p.exists():
            continue
        snap = json.loads(p.read_text())
        mm = nfl_model if league == "nfl" else cfb_model
        for e in snap["events"]:
            kick = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
            if kick < now:
                continue
            kick_pt = kick.astimezone(E.PT)
            slot = slot_for(league, kick_pt)
            if not slot or (kick - now).days > 6:
                continue
            lines = E.book_lines(e)
            mine = {b: L for b, L in lines.items() if b in C.MY_BOOKS}
            if not mine:
                continue
            tier_src, books = E.fair_source(lines)
            mu_mkt = sum(E.solve_mu(mm, lines[b]["home_line"], E.devig(lines[b]["home_price"], lines[b]["away_price"]))
                         for b in books) / len(books)
            home, away = e["home_team"], e["away_team"]
            drivers, mu_model, flags = [], None, []

            if league == "nfl":
                ha, aa = NFL_ABBR.get(home), NFL_ABBR.get(away)
                g = nfl_games.get((ha, aa))
                if g:
                    ih, ia = inj.get(ha, {}).get("adj_points", 0), inj.get(aa, {}).get("adj_points", 0)
                    mu_model = g["model_home_margin"] - ih + ia
                    for team, abbr in ((home, ha), (away, aa)):
                        t = inj.get(abbr)
                        if t and t["absences"]:
                            top = t["absences"][0]
                            if top["points"] >= 0.5:
                                drivers.append(("inj", abbr, top, t["adj_points"]))
                mu = mu_mkt + NFL_MODEL_WEIGHT * ((mu_model - mu_mkt) if mu_model is not None else 0)
            else:
                ks = [k for k in neutral if home.startswith(k[0] + " ") and away.startswith(k[1] + " ")]
                is_neutral = neutral[ks[0]] if ks else False
                mu_model, hs, as_ = CR.live_margin(cfb_L, home, away, is_neutral)
                gap = 0.0 if mu_model is None else max(-CFB_GAP_CLIP, min(CFB_GAP_CLIP, mu_model - mu_mkt))
                mu = mu_mkt + CFB_MODEL_WEIGHT * gap
                if mu_model is not None and abs(mu_model - mu_mkt) > CFB_GAP_CLIP:
                    flags.append("model far from market; ratings may be missing a roster or QB change")
                if mu_model is None:
                    flags.append("no model rating (team not rated yet)")
                o = opens.get((hs, as_)) if hs and as_ else None
                if o:
                    drivers.append(("move", o))
                flags.append("college injury data incomplete")

            dist = mm.dist(mu)
            best = None
            for b, L in mine.items():
                for side, price in (("home", L["home_price"]), ("away", L["away_price"])):
                    v = E.ev(dist, L["home_line"], side, price)
                    if best is None or v > best["ev"]:
                        best = {"book": b, "side": side, "home_line": L["home_line"], "price": price, "ev": v}
            side = best["side"]
            team = home if side == "home" else away
            opp = away if side == "home" else home
            line = best["home_line"] if side == "home" else -best["home_line"]
            w, _, l = E.outcome(dist, best["home_line"], side)
            cover = w / (w + l)

            tier = "play" if best["ev"] >= PLAY_EV else "lean" if best["ev"] >= LEAN_EV else "pass"
            # NFL veto: model + injuries clearly on the other side
            if league == "nfl" and mu_model is not None and tier != "pass":
                lean_pts = (mu_model - mu_mkt) * (1 if side == "home" else -1)
                if lean_pts <= -VETO_POINTS:
                    tier = "lean" if tier == "play" else "pass"
                    flags.append(f"model + injuries disagree by {abs(lean_pts):.1f} pts")
            if league == "cfb" and mu_model is not None and tier == "play":
                if (mu_model - mu_mkt) * (1 if side == "home" else -1) < 0:
                    tier = "lean"
                    flags.append("price edge only; model on the other side")
            # sanity rails: a huge edge vs the sharp line is usually a stale or limited price
            def cap_lean(reason):
                nonlocal_tier[0] = "lean" if nonlocal_tier[0] == "play" else nonlocal_tier[0]
                flags.append(reason)
            nonlocal_tier = [tier]
            if tier_src == "consensus":
                cap_lean("no sharp book on this game; fair line is market consensus")
            if best["ev"] > 0.08:
                cap_lean("edge is suspiciously large; verify the price is live before betting")
            if abs(line) > 28:
                cap_lean("blowout spread; model and market are least reliable here")
            rails = sum(1 for f in flags if f.startswith(("no sharp", "edge is susp", "blowout")))
            tier = "pass" if rails >= 2 else nonlocal_tier[0]
            units = units_for(best["ev"], best["price"], league, tier)
            floor = E.price_floor(mm, mu, side, best["home_line"])
            floor_side = None if floor is None else (floor if side == "home" else -floor)
            be_dec = 1 + l / w if w else None
            be_price = None if not be_dec else (round((be_dec - 1) * 100) if be_dec >= 2 else round(-100 / (be_dec - 1)))
            if floor_side is not None:
                floor_text = f"Good to {fmt_line(floor_side)} at -110"
            elif be_price is not None:
                floor_text = f"Only at {fmt_line(line)} {be_price:+d} or better"
            else:
                floor_text = None

            # ---- why (max 3 sentences, every claim tied to a number)
            fair_side = (-mu_mkt if side == "home" else mu_mkt)
            why = []
            src = {"pinnacle": "Pinnacle", "sharp": "BetOnline/LowVig", "consensus": "market consensus"}[tier_src]
            why.append(f"{src} makes this {team.split()[-1]} {fmt_line(round(fair_side * 2) / 2)}; "
                       f"{best['book'].title().replace('Draftkings', 'DraftKings').replace('Fanduel', 'FanDuel')} "
                       f"is dealing {fmt_line(line)} ({best['price']:+d}).")
            if mu_model is not None:
                gap = (mu_model - mu_mkt) * (1 if side == "home" else -1)
                label = "Power ratings" if league == "cfb" else "Efficiency ratings with injuries"
                if abs(gap) >= 1:
                    why.append(f"{label} have {team.split()[-1]} {abs(gap):.1f} pts "
                               f"{'better' if gap > 0 else 'worse'} than the market.")
            for d in drivers:
                if len(why) >= 3:
                    break
                if d[0] == "inj":
                    _, abbr, top, pts = d
                    why.append(f"{abbr} is without {top['player']} ({top['pos']}, {top['status'].lower()}), "
                               f"worth ~{pts:.1f} pts by our injury model.")
                elif d[0] == "move":
                    o = d[1]
                    mv = o["now_home"] - o["open_home"]
                    if abs(mv) >= 1:
                        to = hs if mv < 0 else as_
                        why.append(f"Line has moved {abs(mv):g} pts toward {to} since opening "
                                   f"({o['provider']} {fmt_line(-o['open_home'] if False else o['open_home'])} → "
                                   f"{fmt_line(o['now_home'])}, home view).")
            cards.append({
                "id": e["id"], "league": league, "slot": slot, "tier": tier,
                "matchup": f"{away} @ {home}", "kickoff_utc": e["commence_time"],
                "kickoff_pt": kick_pt.strftime("%a %b %-d, %-I:%M %p PT"),
                "side": team, "opponent": opp, "line": line, "price": best["price"], "book": best["book"],
                "pick_label": f"{team} {fmt_line(line)} ({best['price']:+d})",
                "confidence": round(cover * 100, 1), "ev_pct": round(best["ev"] * 100, 2), "units": units,
                "price_floor": floor_side, "floor_text": floor_text, "fair_source": tier_src,
                "market_home_margin": round(mu_mkt, 1),
                "model_home_margin": round(mu_model, 1) if mu_model is not None else None,
                "why": " ".join(why[:3]), "flags": flags, "odds_pulled_at": snap["pulled_at"],
            })

    # ---- slot selection
    by_slot = {}
    for c in cards:
        by_slot.setdefault(c["slot"], []).append(c)
    board = {}
    for slot, cs in by_slot.items():
        cs.sort(key=lambda c: -c["ev_pct"])
        if slot in ("sat", "sun"):
            board[slot] = {"picks": [c for c in cs if c["tier"] != "pass"][:10],
                           "passed": sum(1 for c in cs if c["tier"] == "pass"), "games": len(cs)}
        else:
            board[slot] = {"picks": cs[:1], "passed": len(cs) - 1, "games": len(cs)}
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"built_at": now.isoformat(), "my_books": C.MY_BOOKS, "cfb_model_weight": CFB_MODEL_WEIGHT,
            "nfl_model_weight": NFL_MODEL_WEIGHT, "play_ev": PLAY_EV, "lean_ev": LEAN_EV,
            "nfl_backtest": nfl_r["meta"]["backtest"],
            "cfb_backtest": json.loads((DERIVED / "cfb_backtest.json").read_text())
            if (DERIVED / "cfb_backtest.json").exists() else None}
    (OUT / "latest.json").write_text(json.dumps({"meta": meta, "board": board, "all": cards}, indent=1))
    log_new(cards, now)
    n = sum(len(b["picks"]) for b in board.values())
    print(f"::notice::picks: {n} on the board from {len(cards)} games")
    return board


LOG_FIELDS = ["logged_at", "pick_key", "league", "slot", "tier", "game_id", "kickoff_utc", "matchup", "side",
              "line", "price", "book", "confidence", "ev_pct", "units", "fair_source", "market_home_margin",
              "model_home_margin"]


def log_new(cards, now):
    path = OUT / "log.csv"
    seen = set()
    if path.exists():
        with open(path) as f:
            seen = {r["pick_key"] for r in csv.DictReader(f)}
    new = []
    for c in cards:
        if c["tier"] == "pass":
            continue
        key = f"{c['id']}|{c['side']}"
        if key in seen:
            continue
        new.append({"logged_at": now.isoformat(), "pick_key": key, "game_id": c["id"],
                    **{k: c[k] for k in LOG_FIELDS if k in c}})
    if not new:
        return
    write_header = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(new)


if __name__ == "__main__":
    b = main()
    for slot in ("thu", "fri", "sat", "sun", "snf", "mnf"):
        if slot not in b:
            continue
        print(f"\n== {slot.upper()} ({b[slot]['games']} games, {b[slot]['passed']} passed)")
        for c in b[slot]["picks"]:
            print(f" [{c['tier'].upper():4}] {c['pick_label']:<42} {c['book']:<10} conf {c['confidence']}%  "
                  f"EV {c['ev_pct']:+.1f}%  {c['units']}u  {c['floor_text']}  | {c['kickoff_pt']}")
            print(f"        {c['why']}")
            if c["flags"]:
                print(f"        flags: {'; '.join(c['flags'])}")
