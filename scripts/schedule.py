"""Decide whether this (half-hourly) run should do anything, and whether it should spend odds credits.

GitHub's scheduler often starts jobs late, sometimes by hours, so the workflow wakes every 30 minutes
and this script decides. Odds are pulled (3 credits) only when they matter:
  - once a day as the morning baseline (openers, overnight moves), or
  - when a game kicks off in the next 20-120 minutes and the last pull is 50+ minutes old (closing lines).
Everything else (injuries, grading, the page) refreshes at most every 3 hours, or sooner when a finished
game is waiting to be graded. Writes run=/odds= to $GITHUB_OUTPUT.
"""
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import RAW, load_snapshot, parse_ts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAILY_HOUR_PT = 7          # morning baseline pull (PT)


def last_odds_pull():
    """{league: time of its last pull}"""
    out = {}
    for lg in ("nfl", "cfb"):
        p = RAW / "odds" / lg / "latest.json"
        if p.exists():
            out[lg] = parse_ts(json.loads(p.read_text())["pulled_at"])
    return out


def upcoming_kickoffs():
    out = []
    for lg in ("nfl", "cfb"):
        p = RAW / "odds" / lg / "latest.json"
        if p.exists():
            out += [(lg, parse_ts(e["t"])) for e in load_snapshot(p)["events"]]
    return out


def waiting_to_grade(now):
    p = ROOT / "data" / "picks" / "model_log.csv"
    if not p.exists():
        return 0
    with open(p) as f:
        return sum(1 for r in csv.DictReader(f)
                   if not r.get("result") and parse_ts(r["kickoff_utc"]) + timedelta(hours=4) <= now)


def decide(now, force=False):
    last_run_p = RAW / "_last_run.json"
    last_run = parse_ts(json.loads(last_run_p.read_text())["run_at"]) if last_run_p.exists() else None
    last_odds = last_odds_pull()
    since_run = (now - last_run) if last_run else timedelta(days=9)
    since = {lg: now - last_odds.get(lg, now - timedelta(days=9)) for lg in ("nfl", "cfb")}
    since_odds = max(since.values())       # the stalest league
    from zoneinfo import ZoneInfo
    pt = now.astimezone(ZoneInfo("America/Los_Angeles"))
    soon = [(lg, k) for lg, k in upcoming_kickoffs() if timedelta(minutes=20) <= k - now <= timedelta(minutes=120)]

    reasons, leagues = [], []
    if force:
        leagues, reasons = ["nfl", "cfb"], ["manual run"]
    elif since_odds >= timedelta(hours=20) or (pt.hour == DAILY_HOUR_PT and since_odds >= timedelta(hours=6)):
        leagues = ["nfl", "cfb"]
        reasons.append("daily baseline odds pull")
    elif any(since[lg] >= timedelta(minutes=50) for lg, _ in soon):
        leagues = sorted({lg for lg, _ in soon if since[lg] >= timedelta(minutes=50)})  # only leagues kicking off
        reasons.append(f"{len(soon)} game(s) kicking off within 2 hours: closing-line pull ({', '.join(leagues)})")
    odds = bool(leagues)
    chk = ROOT / "data" / "manual" / "cfb_injury_check.json"
    new_check = False
    if chk.exists():
        try:
            at = json.loads(chk.read_text()).get("checked_at")
            new_check = bool(at) and (last_run is None or parse_ts(at) > last_run)
        except (ValueError, AttributeError):
            pass
    grade_n = waiting_to_grade(now)
    run = odds or new_check or since_run >= timedelta(hours=3) or (grade_n and since_run >= timedelta(minutes=50))
    if not odds and run:
        reasons.append("new college injury check" if new_check else
                       f"{grade_n} pick(s) to grade" if grade_n else "3-hour refresh (injuries, page)")
    return {"run": bool(run), "odds": ",".join(leagues) if leagues else "false",
            "reason": "; ".join(reasons) or "nothing due"}


if __name__ == "__main__":
    d = decide(datetime.now(timezone.utc), force=os.environ.get("FORCE") == "true")
    print(f"::notice::schedule: run={d['run']} odds={d['odds']} ({d['reason']})")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"run={'true' if d['run'] else 'false'}\nodds={d['odds']}\n")
