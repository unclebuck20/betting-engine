"""Helper for the scheduled slate review (procedure: handbook/SLATE_REVIEW.md).

Two jobs, one session:
  scout   every published pick (NFL and college) gets one judgment verdict before kickoff:
          agree / caution (half size) / veto (no bet). It can never add a bet. Graded separately on CLV.
  injury  college only (no feed exists): key starters out that the line hasn't absorbed hold the pick.
          morning = first run of the day, every college pick in the next 16 hours;
          prekick = later runs, only picks kicking off 45-105 minutes out.

  python scripts/review.py list       what this run needs to do: {"scout": [...], "injury": {"mode", "games"}}
  python scripts/review.py validate   check both files before committing (also prunes kicked-off injury entries)
  python scripts/review.py dispatch   start the refresh workflow so the review reaches the page now

Files (the only inputs picks.py takes from the review):
  data/manual/scout.json            {"picks": {pick_key: verdict}}   kept for the season (it's the scout's record)
  data/manual/cfb_injury_check.json {"games": {event id: check}}     pruned at kickoff
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "data" / "manual"
CHECK = MANUAL / "cfb_injury_check.json"
SCOUT = MANUAL / "scout.json"
MORNING_H = 16
PREKICK = (45, 105)        # minutes before kickoff
SCOUT_MIN_LEAD = 10        # don't start scouting a pick kicking off in under 10 minutes
VERDICTS = ("agree", "caution", "veto")
PT = ZoneInfo("America/Los_Angeles")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load(p, default):
    return json.loads(p.read_text()) if p.exists() else default


def published():
    return load(ROOT / "data" / "picks" / "published.json", {})


def injury_mode(now):
    """morning unless a morning check already ran today (PT)."""
    at = load(CHECK, {}).get("morning_at")
    if at and ts(at).astimezone(PT).date() == now.astimezone(PT).date():
        return "prekick"
    return "morning"


def injury_games(now, m):
    lo, hi = (timedelta(0), timedelta(hours=MORNING_H)) if m == "morning" else \
        (timedelta(minutes=PREKICK[0]), timedelta(minutes=PREKICK[1]))
    latest = load(ROOT / "data" / "picks" / "latest.json", {"all": []})
    out = {}
    cands = [p for p in published().values() if p["league"] == "cfb" and p["status"] in ("live", "held", "vetoed")]
    cands += [c for c in latest["all"] if c["league"] == "cfb" and c["tier"] != "pass"]
    for c in cands:
        k = ts(c["kickoff_utc"])
        if not now + lo < k <= now + hi or c["id"] in out:
            continue
        hist = c.get("history") or []
        out[c["id"]] = {
            "id": c["id"], "matchup": c["matchup"], "kickoff_utc": c["kickoff_utc"], "kickoff_pt": c["kickoff_pt"],
            "our_side": c["side"], "opponent": c["opponent"], "line": c["line"], "open_line": c.get("open_line"),
            "line_now": (c.get("current") or {}).get("line", c["line"]),
            "line_history": [[h[0][:16], h[1]] for h in hist][-6:],
        }
    return sorted(out.values(), key=lambda g: g["kickoff_utc"])


def scout_picks(now):
    """Published picks still in play, kicking off within 16 hours, with no verdict yet."""
    done = load(SCOUT, {"picks": {}})["picks"]
    out = []
    for p in published().values():
        k = ts(p["kickoff_utc"])
        if p["status"] not in ("live", "held") or p["pick_key"] in done:
            continue
        if not now + timedelta(minutes=SCOUT_MIN_LEAD) < k <= now + timedelta(hours=MORNING_H):
            continue
        cur = p.get("current") or {}
        L = p.get("latest") or p
        out.append({
            "pick_key": p["pick_key"], "league": p["league"], "market": p.get("market", "spread"),
            "matchup": p["matchup"], "kickoff_pt": p["kickoff_pt"], "kickoff_utc": p["kickoff_utc"],
            "side": p["side"], "line": cur.get("line", p["line"]), "price": cur.get("price", p["price"]),
            "fair_line": p.get("fair_line"), "open_line": p.get("open_line"),
            "model_home_margin": p.get("model_home_margin"), "side_is_home": p.get("side_is_home"),
            "tier": L.get("tier"), "units": p.get("units"), "ev_pct": L.get("ev_pct"),
            "why": L.get("why"), "edges": p.get("edges", []), "flags": L.get("flags", []),
            "line_history": [[h[0][:16], h[1]] for h in p.get("history", [])][-6:],
        })
    return sorted(out, key=lambda x: x["kickoff_utc"])


def validate(now):
    errs = []
    if CHECK.exists():
        d = json.loads(CHECK.read_text())
        games = d.get("games", {})
        if games and not d.get("checked_at"):
            errs.append("injury: top-level checked_at missing")
        for gid in list(games):
            g = games[gid]
            if ts(g.get("kickoff_utc", "1970-01-01T00:00:00Z")) <= now:
                games.pop(gid)                 # already kicked off; no longer needed
                continue
            teams = set(g.get("matchup", "").split(" @ "))
            for t in g.get("hold", []):
                if t not in teams:
                    errs.append(f"injury {gid}: hold team '{t}' must match a team name in '{g.get('matchup')}' exactly")
            if not g.get("summary"):
                errs.append(f"injury {gid}: summary missing")
            elif len(g["summary"]) > 160:
                errs.append(f"injury {gid}: summary over 160 characters")
            for a in g.get("absences", []):
                if a.get("team") not in teams:
                    errs.append(f"injury {gid}: absence team '{a.get('team')}' not in matchup")
                if not str(a.get("source", "")).startswith("http"):
                    errs.append(f"injury {gid}: absence {a.get('player')} needs a source URL")
        CHECK.write_text(json.dumps(d, indent=1))
    if SCOUT.exists():
        s = json.loads(SCOUT.read_text())
        for k, v in s.get("picks", {}).items():
            if v.get("verdict") not in VERDICTS:
                errs.append(f"scout {k}: verdict must be one of {VERDICTS}")
            if not v.get("reason") or len(v["reason"]) > 160:
                errs.append(f"scout {k}: reason missing or over 160 characters")
            if v.get("verdict") in ("caution", "veto") and not any(
                    str(u).startswith("http") for u in v.get("sources", [])):
                errs.append(f"scout {k}: a {v.get('verdict')} needs at least one source URL")
            for f in ("scouted_at", "kickoff_utc", "league", "side"):
                if not v.get(f):
                    errs.append(f"scout {k}: {f} missing")
    if errs:
        print("\n".join(errs))
        sys.exit(1)
    s = load(SCOUT, {"picks": {}})["picks"]
    counts = {v: sum(x.get("verdict") == v for x in s.values()) for v in VERDICTS}
    holds = sum(len(g.get("hold", [])) for g in load(CHECK, {}).get("games", {}).values())
    print(f"ok: scout {counts}; injury holds {holds}")


def dispatch():
    import subprocess
    body = json.dumps({"ref": "main", "inputs": {"force": "false"}})
    r = subprocess.run(["gh", "api", "-X", "POST",
                        "repos/unclebuck20/betting-engine/actions/workflows/refresh.yml/dispatches",
                        "--input", "-"], input=body, text=True, capture_output=True)
    print("refresh started" if r.returncode == 0 else
          f"could not start refresh ({r.stderr.strip()[:200]}); the half-hourly run will pick it up")


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    cmd = sys.argv[1:2]
    if cmd == ["list"]:
        m = injury_mode(now)
        print(json.dumps({"now_utc": now.isoformat(timespec="minutes"), "scout": scout_picks(now),
                          "injury": {"mode": m, "games": injury_games(now, m)}}, indent=1))
    elif cmd == ["validate"]:
        validate(now)
    elif cmd == ["dispatch"]:
        dispatch()
    else:
        print(__doc__)
