"""Helper for the pre-slate college injury check (procedure: handbook/CFB_INJURY_CHECK.md).

  python scripts/cfb_injury_check.py list       games to check, as {"mode", "games"}:
      morning  first run of a slate day (no morning check yet today): every college pick in the next 16 hours,
               to catch earlier-week news the line may not have absorbed
      prekick  later runs: only picks kicking off 45-105 minutes from now, when availability reports and
               game-day news land
  python scripts/cfb_injury_check.py dispatch   start the refresh workflow so a new check reaches the page now
  python scripts/cfb_injury_check.py validate   check data/manual/cfb_injury_check.json before committing;
                                                 also drops games that have already kicked off

The check file is the only input picks.py takes from it: a team named in a game's "hold" list has its
pick held (status "held", no bet) when it is the side we'd bet; every checked game shows the summary.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "data" / "manual" / "cfb_injury_check.json"
MORNING_H = 16
PREKICK = (45, 105)        # minutes before kickoff
PT = __import__("zoneinfo").ZoneInfo("America/Los_Angeles")


def mode(now):
    """morning unless a morning check already ran today (PT)."""
    if CHECK.exists():
        at = json.loads(CHECK.read_text()).get("morning_at")
        if at and ts(at).astimezone(PT).date() == now.astimezone(PT).date():
            return "prekick"
    return "morning"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def games_to_check(now, m):
    lo, hi = (timedelta(0), timedelta(hours=MORNING_H)) if m == "morning" else \
        (timedelta(minutes=PREKICK[0]), timedelta(minutes=PREKICK[1]))
    latest = json.loads((ROOT / "data" / "picks" / "latest.json").read_text())
    pub_p = ROOT / "data" / "picks" / "published.json"
    pub = json.loads(pub_p.read_text()) if pub_p.exists() else {}
    out = {}
    cands = [p for p in pub.values() if p["league"] == "cfb" and p["status"] in ("live", "held")]   # has history
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


def validate(now):
    d = json.loads(CHECK.read_text())
    errs = []
    if not d.get("checked_at"):
        errs.append("top-level checked_at missing")
    games = d.get("games", {})
    for gid in list(games):
        g = games[gid]
        if ts(g.get("kickoff_utc", "1970-01-01T00:00:00Z")) <= now:
            games.pop(gid)                 # already kicked off; no longer needed
            continue
        teams = set(g.get("matchup", "").split(" @ "))
        for t in g.get("hold", []):
            if t not in teams:
                errs.append(f"{gid}: hold team '{t}' must match a team name in '{g.get('matchup')}' exactly")
        if not g.get("summary"):
            errs.append(f"{gid}: summary missing")
        elif len(g["summary"]) > 160:
            errs.append(f"{gid}: summary over 160 characters")
        for a in g.get("absences", []):
            if a.get("team") not in teams:
                errs.append(f"{gid}: absence team '{a.get('team')}' not in matchup")
            if not a.get("source", "").startswith("http"):
                errs.append(f"{gid}: absence {a.get('player')} needs a source URL")
    CHECK.write_text(json.dumps(d, indent=1))
    if errs:
        print("\n".join(errs))
        sys.exit(1)
    holds = sum(len(g.get("hold", [])) for g in games.values())
    print(f"ok: {len(games)} games, {holds} hold(s)")


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    if sys.argv[1:] == ["list"]:
        m = mode(now)
        print(json.dumps({"mode": m, "now_utc": now.isoformat(timespec="minutes"),
                          "games": games_to_check(now, m)}, indent=1))
    elif sys.argv[1:] == ["dispatch"]:
        import subprocess
        body = json.dumps({"ref": "main", "inputs": {"force": "false"}})
        r = subprocess.run(["gh", "api", "-X", "POST",
                            "repos/unclebuck20/betting-engine/actions/workflows/refresh.yml/dispatches",
                            "--input", "-"], input=body, text=True, capture_output=True)
        print("refresh started" if r.returncode == 0 else
              f"could not start refresh ({r.stderr.strip()[:200]}); the half-hourly run will pick it up")
    elif sys.argv[1:] == ["validate"]:
        validate(now)
    else:
        print(__doc__)
