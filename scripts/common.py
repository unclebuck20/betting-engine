"""Shared helpers: time zone, team-name matching, compact odds snapshots, tunable parameters."""
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
MODEL_DIR = ROOT / "data" / "model"
PT = ZoneInfo("America/Los_Angeles")   # handles daylight saving (Nov 1 switch)

# Defaults. data/model/params.json (written by calibrate.py) overrides these.
DEFAULT_PARAMS = {
    "cfb_weight_open": 0.30,      # backtest: weight on (model - line) when betting at the opener
    "cfb_weight_close": 0.085,    # ...and at the close; the live weight slides between them by time
    "cfb_gap_clip": 10.0,
    "nfl_injury_scale": 1.0,      # multiplies injury point values
    "play_ev": 0.025,
    "lean_ev": 0.01,
    "kelly_fraction": 0.25,
    "max_units_nfl": 3.0,
    "max_units_cfb": 2.0,
    "slate_cap_units": 8.0,
    "veto_points": 3.0,
    "max_confidence": 0.59,       # best backtested cover rate (college, model gap 7+ vs the opener)
    "max_confidence_nfl": 0.56,   # best NFL bucket in the 2017-25 walk-forward vs closing lines
    "stale_price_ev": 0.06,       # a pure price edge this big vs the sharp line is usually a stale quote
}


def params():
    p = dict(DEFAULT_PARAMS)
    f = MODEL_DIR / "params.json"
    if f.exists():
        p.update(json.loads(f.read_text()).get("params", {}))
    return p


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = s.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", s)


# ------------------------------------------------------------ college names
class CFBNames:
    """Map odds-feed names ('Hawaii Rainbow Warriors') to CollegeFootballData schools ("Hawai'i")."""

    def __init__(self):
        self.exact = {}
        teams_path = RAW / "cfbd" / "teams.json"
        teams = json.loads(teams_path.read_text()) if teams_path.exists() else []
        for t in teams:
            if t.get("classification") not in ("fbs", "fcs") or not t.get("mascot"):
                continue
            names = {t["school"], *(t.get("alternateNames") or []), t.get("abbreviation") or ""}
            for n in names:
                if n:
                    self.exact.setdefault(norm(f"{n} {t['mascot']}"), t["school"])
                    self.exact.setdefault(norm(n), t["school"])
        self.misses = set()

    def school(self, odds_name):
        k = norm(odds_name)
        if k in self.exact:
            return self.exact[k]
        # fall back: longest known name that the odds name starts with
        best = max((n for n in self.exact if k.startswith(n) and len(n) >= 4), key=len, default=None)
        if best:
            return self.exact[best]
        self.misses.add(odds_name)
        return None


# ------------------------------------------------------------ compact odds snapshots
def compact_events(events):
    """Keep only what the engine uses: spreads and totals per book, update time, bet-slip links."""
    out = []
    for e in events:
        books = {}
        for b in e.get("bookmakers", []):
            for m in b.get("markets", []):
                o = {x["name"]: x for x in m["outcomes"]}
                if m["key"] == "spreads":
                    h, a = o.get(e["home_team"]), o.get(e["away_team"])
                    if not h or not a or h.get("point") is None:
                        continue
                    books.setdefault(b["key"], {}).update({
                        "hl": h["point"], "hp": h["price"], "ap": a["price"], "u": m.get("last_update"),
                        "hlink": h.get("link"), "alink": a.get("link"), "link": m.get("link") or b.get("link")})
                elif m["key"] == "totals":
                    ov, un = o.get("Over"), o.get("Under")
                    if not ov or not un or ov.get("point") is None:
                        continue
                    books.setdefault(b["key"], {})["tot"] = {
                        "l": ov["point"], "op": ov["price"], "up": un["price"], "u": m.get("last_update"),
                        "olink": ov.get("link") or m.get("link"), "ulink": un.get("link") or m.get("link")}
        out.append({"id": e["id"], "t": e["commence_time"], "home": e["home_team"], "away": e["away_team"],
                    "books": books})
    return out


def load_snapshot(path):
    d = json.loads(Path(path).read_text())
    events = d["events"]
    if events and "bookmakers" in events[0]:   # old full-format snapshot
        events = compact_events(events)
    return {"pulled_at": d["pulled_at"], "events": events}


def snapshot_paths(league):
    return sorted(p for p in (RAW / "odds" / league).glob("2*.json"))
