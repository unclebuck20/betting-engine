"""Market layer: fair line from the sharp books, then edge at every book.

Method
1. De-vig the sharpest available price for each game (Pinnacle, else BetOnline/LowVig,
   else all-book consensus) to get the fair probability that the home side covers.
2. Solve for the fair home margin (mu) whose margin distribution reproduces that probability.
   - NFL: empirical distribution of final margins in past games with a similar closing spread
     (nflverse 2006+), kernel-weighted, so key numbers (3, 7, 10, 14) keep their real weight.
   - CFB: discrete normal, sd = CFB_SD (placeholder until college history is loaded).
3. Price every book's spread with that distribution -> win / push / lose -> expected value.
4. Price floor: the worst number the pick is still +EV at -110.
"""
import csv
import json
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "edges"
PT = timezone(timedelta(hours=-7))  # display only; fine through early Nov

CFB_SD = 15.5
NFL_KERNEL_BW = 1.0
NFL_HISTORY_FROM = 2006
MARGINS = range(-70, 71)


# ------------------------------------------------------------ odds helpers
def dec(american):
    return 1 + (american / 100 if american > 0 else 100 / -american)


def devig(price_a, price_b):
    ia, ib = 1 / dec(price_a), 1 / dec(price_b)
    return ia / (ia + ib)


# ------------------------------------------------------------ margin models
def load_nfl_history():
    rows = []
    with open(RAW / "nflverse" / "games.csv") as f:
        for r in csv.DictReader(f):
            if r["result"] and r["spread_line"] and int(r["season"]) >= NFL_HISTORY_FROM:
                rows.append((float(r["spread_line"]), int(float(r["result"]))))
    return rows


class NFLModel:
    def __init__(self, hist):
        self.hist = hist
        self._cache = {}

    def dist(self, mu):
        key = round(mu, 2)
        if key not in self._cache:
            d = {}
            tot = 0.0
            for s, m in self.hist:
                w = math.exp(-0.5 * ((s - mu) / NFL_KERNEL_BW) ** 2)
                if w < 1e-4:
                    continue
                d[m] = d.get(m, 0) + w
                tot += w
            self._cache[key] = {k: v / tot for k, v in d.items()}
        return self._cache[key]


class CFBModel:
    def dist(self, mu):
        cdf = lambda x: 0.5 * (1 + math.erf((x - mu) / (CFB_SD * math.sqrt(2))))  # noqa: E731
        return {k: cdf(k + 0.5) - cdf(k - 0.5) for k in MARGINS}


def outcome(dist, home_line, side):
    """win/push/lose for a spread bet. home_line: home team's point (e.g. -3)."""
    win = push = 0.0
    for m, p in dist.items():
        v = m + home_line if side == "home" else -(m + home_line)
        if v > 0:
            win += p
        elif v == 0:
            push += p
    return win, push, 1 - win - push


def cover_prob(dist, home_line):
    w, _, l = outcome(dist, home_line, "home")
    return w / (w + l) if w + l else 0.5


def solve_mu(model, home_line, p_home):
    """Fair expected home margin that reproduces the de-vigged home cover probability."""
    best, best_err = 0.0, 9
    for i in range(-450, 451):
        mu = i / 10
        err = abs(cover_prob(model.dist(mu), home_line) - p_home)
        if err < best_err:
            best, best_err = mu, err
    return best


def ev(dist, home_line, side, price):
    w, _, l = outcome(dist, home_line, side)
    return w * (dec(price) - 1) - l


# ------------------------------------------------------------ per-game
def book_lines(event):
    out = {}
    for b in event.get("bookmakers", []):
        for m in b.get("markets", []):
            if m["key"] != "spreads":
                continue
            o = {x["name"]: x for x in m["outcomes"]}
            h, a = o.get(event["home_team"]), o.get(event["away_team"])
            if h and a and h.get("point") is not None:
                out[b["key"]] = {"home_line": h["point"], "home_price": h["price"],
                                 "away_price": a["price"], "updated": m.get("last_update")}
    return out


def fair_source(lines):
    for tier, books in (("pinnacle", ["pinnacle"]), ("sharp", ["betonlineag", "lowvig"])):
        have = [b for b in books if b in lines]
        if have:
            return tier, have
    return "consensus", list(lines)


def price_floor(model, mu, side, start_line):
    """Worst home_line (from bettor's view) still +EV at -110."""
    step = 0.5
    line, floor = start_line, None
    for _ in range(30):
        if ev(model.dist(mu), line, side, -110) > 0:
            floor = line
            line = line - step if side == "home" else line + step
        else:
            break
    return floor


def analyze(event, model, league):
    lines = book_lines(event)
    if not lines:
        return None
    tier, books = fair_source(lines)
    mus = []
    for b in books:
        L = lines[b]
        p = devig(L["home_price"], L["away_price"])
        mus.append(solve_mu(model, L["home_line"], p))
    mu = sum(mus) / len(mus)
    dist = model.dist(mu)

    priced = []
    for bk, L in lines.items():
        for side, price in (("home", L["home_price"]), ("away", L["away_price"])):
            priced.append({"book": bk, "side": side, "home_line": L["home_line"], "price": price,
                           "ev": round(ev(dist, L["home_line"], side, price), 4)})
    best_any = max(priced, key=lambda x: x["ev"])
    card = [x for x in priced if x["book"] in C.MY_BOOKS]
    best_card = max(card, key=lambda x: x["ev"]) if card else None

    def label(x):
        team = event["home_team"] if x["side"] == "home" else event["away_team"]
        pt = x["home_line"] if x["side"] == "home" else -x["home_line"]
        return f"{team} {pt:+g} ({x['price']:+d})"

    kick = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
    res = {
        "league": league, "id": event["id"],
        "matchup": f"{event['away_team']} @ {event['home_team']}",
        "kickoff_utc": event["commence_time"],
        "kickoff_pt": kick.astimezone(PT).strftime("%a %b %-d %-I:%M %p PT"),
        "fair_source": tier, "fair_books": books,
        "fair_home_margin": round(mu, 1),
        "fair_home_spread": round(-mu, 1),
        "books": len(lines),
        "best_any": {**best_any, "label": label(best_any)},
    }
    if best_card:
        res["card"] = {**best_card, "label": label(best_card),
                       "price_floor_home_line": price_floor(model, mu, best_card["side"], best_card["home_line"])}
    return res


def main():
    nfl, cfb = NFLModel(load_nfl_history()), CFBModel()
    results, now = [], datetime.now(timezone.utc)
    for league, model in (("nfl", nfl), ("cfb", cfb)):
        p = RAW / "odds" / league / "latest.json"
        if not p.exists():
            continue
        snap = json.loads(p.read_text())
        for e in snap["events"]:
            if datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) < now:
                continue
            r = analyze(e, model, league)
            if r:
                r["odds_pulled_at"] = snap["pulled_at"]
                results.append(r)
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"built_at": now.isoformat(), "my_books": C.MY_BOOKS, "cfb_sd": CFB_SD,
            "nfl_history_games": len(nfl.hist), "games": len(results)}
    (OUT / "latest.json").write_text(json.dumps({"meta": meta, "games": results}, indent=1))
    print(f"::notice::edges: {len(results)} games priced")
    return results


if __name__ == "__main__":
    main()
