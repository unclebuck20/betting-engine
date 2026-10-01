"""Market layer: margin distributions, fair lines from the sharp books, and expected value at any book.

- Margin distributions are empirical: final margins of past games whose closing spread was near the
  number in question (kernel-weighted, bandwidth widened until there's enough data). That keeps key
  numbers real: NFL 3/7/10/14, college 3/7/10/14/17/21.
  NFL: nflverse closing lines 2006+. College: CollegeFootballData closing lines 2021-2025 (FBS vs FBS).
- Fair line: de-vig the sharpest available book (Pinnacle, else BetOnline/LowVig, else consensus) and
  solve for the expected home margin that reproduces its cover probability.
"""
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402
from common import PT, RAW, load_snapshot, parse_ts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "edges"
NFL_HISTORY_FROM = 2006
MIN_ESS = 250


# ------------------------------------------------------------ odds helpers
def dec(american):
    return 1 + (american / 100 if american > 0 else 100 / -american)


def american(decimal_odds):
    return round((decimal_odds - 1) * 100) if decimal_odds >= 2 else round(-100 / (decimal_odds - 1))


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


def load_cfb_history():
    rows = []
    hist = RAW / "cfbd_history"
    for lp in sorted(hist.glob("lines_*.json")):
        y = lp.stem.split("_")[1]
        gp = hist / f"games_{y}.json"
        if not gp.exists():
            continue
        fbs = {g["id"] for g in json.loads(gp.read_text())
               if g.get("homeClassification") == "fbs" and g.get("awayClassification") == "fbs"}
        for g in json.loads(lp.read_text()):
            if g["id"] not in fbs or g.get("homeScore") is None or g.get("awayScore") is None:
                continue
            ls = [l for l in g.get("lines") or [] if l.get("spread") is not None]
            if not ls:
                continue
            pref = next((l for l in ls if l["provider"] in ("DraftKings", "ESPN Bet", "Bovada")), ls[0])
            rows.append((-pref["spread"], g["homeScore"] - g["awayScore"]))
    return rows


class MarginModel:
    """P(final home margin = m | fair expected margin mu), from past games with a similar spread."""

    def __init__(self, hist, bw):
        self.hist, self.bw, self._cache = hist, bw, {}

    def dist(self, mu):
        key = round(mu, 1)
        if key in self._cache:
            return self._cache[key]
        bw = self.bw
        while True:
            d, tot, tot2 = {}, 0.0, 0.0
            for s, m in self.hist:
                w = math.exp(-0.5 * ((s - key) / bw) ** 2)
                if w < 1e-4:
                    continue
                # shift each past result by the (small) gap between its spread and this one,
                # rounded, so wide kernels don't blur the expected margin
                mm = m + round(key - s) if bw > self.bw and abs(key - s) >= 1 else m
                d[mm] = d.get(mm, 0) + w
                tot += w
                tot2 += w * w
            if tot and tot * tot / tot2 >= MIN_ESS or bw > 12:
                break
            bw *= 1.5
        self._cache[key] = {k: v / tot for k, v in d.items()}
        return self._cache[key]


def load_nfl_total_history():
    rows = []
    with open(RAW / "nflverse" / "games.csv") as f:
        for r in csv.DictReader(f):
            if r["total"] and r["total_line"] and int(r["season"]) >= NFL_HISTORY_FROM:
                rows.append((float(r["total_line"]), int(float(r["total"]))))
    return rows


def models():
    return {"nfl": MarginModel(load_nfl_history(), 1.0), "cfb": MarginModel(load_cfb_history(), 1.5),
            "nfl_total": MarginModel(load_nfl_total_history(), 1.0)}


# Totals reuse the spread machinery: "over L" wins when total - L > 0, i.e. side "home" with home_line = -L.
def total_lines(event):
    return {k: {"line": b["tot"]["l"], "over": b["tot"]["op"], "under": b["tot"]["up"],
                "olink": b["tot"].get("olink"), "ulink": b["tot"].get("ulink")}
            for k, b in event["books"].items() if "tot" in b}


def fair_total(model, tl, books):
    mus = [solve_mu(model, -tl[b]["line"], devig(tl[b]["over"], tl[b]["under"])) for b in books]
    return sum(mus) / len(mus)


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


def cover_prob(dist, home_line, side="home"):
    w, _, l = outcome(dist, home_line, side)
    return w / (w + l) if w + l else 0.5


def solve_mu(model, home_line, p_home):
    """Fair expected home margin reproducing the de-vigged home cover probability (bisection)."""
    lo, hi = -60.0, 60.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if cover_prob(model.dist(mid), home_line) < p_home:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 1)


def ev(dist, home_line, side, price):
    w, _, l = outcome(dist, home_line, side)
    return w * (dec(price) - 1) - l


def breakeven_price(dist, home_line, side):
    w, _, l = outcome(dist, home_line, side)
    return american(1 + l / w) if w else None


# ------------------------------------------------------------ per-game
def book_lines(event):
    """compact event -> {book: {home_line, home_price, away_price, updated, links}} (spreads)"""
    return {k: {"home_line": b["hl"], "home_price": b["hp"], "away_price": b["ap"], "updated": b.get("u"),
                "hlink": b.get("hlink") or b.get("link"), "alink": b.get("alink") or b.get("link")}
            for k, b in event["books"].items() if "hl" in b}


def fair_source(lines):
    for tier, books in (("pinnacle", ["pinnacle"]), ("sharp", ["betonlineag", "lowvig"])):
        have = [b for b in books if b in lines]
        if have:
            return tier, have
    return "consensus", list(lines)


def fair_mu(model, lines, books):
    mus = [solve_mu(model, lines[b]["home_line"], devig(lines[b]["home_price"], lines[b]["away_price"]))
           for b in books]
    return sum(mus) / len(mus)


def consensus_mu(model, lines):
    soft = [b for b in lines if b not in C.SHARP_BOOKS]
    return fair_mu(model, lines, soft) if soft else None


def price_floor(model, mu, side, start_line):
    """Worst line (home_line terms) still +EV at -110."""
    line, floor = start_line, None
    for _ in range(40):
        if ev(model.dist(mu), line, side, -110) > 0:
            floor = line
            line = line - 0.5 if side == "home" else line + 0.5
        else:
            break
    return floor


def main():
    M = models()
    results, now = [], datetime.now(timezone.utc)
    for league in ("nfl", "cfb"):
        p = RAW / "odds" / league / "latest.json"
        if not p.exists():
            continue
        snap = load_snapshot(p)
        m = M[league]
        for e in snap["events"]:
            if parse_ts(e["t"]) < now or not e["books"]:
                continue
            lines = book_lines(e)
            tier, books = fair_source(lines)
            mu = fair_mu(m, lines, books)
            dist = m.dist(mu)
            priced = sorted(({"book": bk, "side": s, "home_line": L["home_line"], "price": pr,
                              "ev": round(ev(dist, L["home_line"], s, pr), 4)}
                             for bk, L in lines.items() for s, pr in (("home", L["home_price"]), ("away", L["away_price"]))),
                            key=lambda x: -x["ev"])
            results.append({"league": league, "id": e["id"], "matchup": f"{e['away']} @ {e['home']}",
                            "kickoff_pt": parse_ts(e["t"]).astimezone(PT).strftime("%a %b %-d %-I:%M %p %Z"),
                            "fair_source": tier, "fair_home_margin": mu, "best": priced[:3]})
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"built_at": now.isoformat(), "nfl_history_games": len(M["nfl"].hist),
            "cfb_history_games": len(M["cfb"].hist), "games": len(results)}
    (OUT / "latest.json").write_text(json.dumps({"meta": meta, "games": results}, indent=1))
    print(f"::notice::edges: {len(results)} games priced (history: NFL {meta['nfl_history_games']}, "
          f"CFB {meta['cfb_history_games']})")


if __name__ == "__main__":
    main()
