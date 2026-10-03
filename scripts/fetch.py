"""Pull raw data -> validate -> write data/raw/.

Everything is fetched and validated first; nothing is written unless the
critical guards pass, so a bad run leaves the last good data in place.
Results are reported as GitHub Actions annotations (::notice:: / ::warning:: / ::error::).
"""
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402
from common import compact_events  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
NOW = datetime.now(timezone.utc)
STAMP = NOW.strftime("%Y%m%dT%H%MZ")
HEADERS = {"User-Agent": "betting-engine (personal research; github.com/unclebuck20/betting-engine)"}

errors, warnings, notices = [], [], []
writes = {}  # relative path -> python object (json) or str (text)
summary = {"run_at": NOW.isoformat(), "sources": {}}


def get(url, **kw):
    r = requests.get(url, headers={**HEADERS, **kw.pop("headers", {})}, timeout=45, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- nflverse
def fetch_nflverse():
    text = get(C.SOURCES["nflverse_games"]).text
    rows = list(csv.DictReader(io.StringIO(text)))
    season = [r for r in rows if r["season"] == str(C.SEASON)]
    with_line = [r for r in season if r["spread_line"]]
    done = [r for r in season if r["result"]]
    info = {"rows_total": len(rows), "season_games": len(season),
            "with_spread_line": len(with_line), "completed": len(done)}
    summary["sources"]["nflverse_games"] = info
    if len(season) < C.MIN_NFL_GAMES_IN_SCHEDULE:
        errors.append(f"nflverse: only {len(season)} {C.SEASON} games (min {C.MIN_NFL_GAMES_IN_SCHEDULE})")
        return
    writes["nflverse/games.csv"] = text
    notices.append(f"nflverse: {len(season)} games, {len(with_line)} with lines, {len(done)} final")


# ---------------------------------------------------------------- odds
def fetch_odds():
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        warnings.append("odds: ODDS_API_KEY not set; skipped")
        return
    wanted = os.environ.get("ODDS_PULL", "nfl,cfb")
    if wanted == "false":
        notices.append("odds: not due this run (scheduler); keeping the last pull")
        return
    wanted = set(wanted.split(","))
    base = C.SOURCES["odds_base"]
    # /sports is free and returns quota headers
    r = get(f"{base}/sports", params={"apiKey": key})
    left = int(r.headers.get("x-requests-remaining", "0"))
    info = {"credits_left_before": left}
    summary["sources"]["odds"] = info
    if left < C.ODDS_MIN_CREDITS_LEFT:
        warnings.append(f"odds: only {left} credits left; skipping to protect closing snapshots")
        return
    for name, sport in C.ODDS_SPORTS.items():
        if name not in wanted:
            continue
        r = get(f"{base}/sports/{sport}/odds", params={
            "apiKey": key, "markets": ",".join(C.ODDS_MARKETS[name]),
            "bookmakers": ",".join(C.ODDS_BOOKMAKERS), "oddsFormat": "american", "includeLinks": "true",
        })
        events = r.json()
        books = {b["key"] for e in events for b in e.get("bookmakers", [])}
        with_pin = sum(1 for e in events if any(b["key"] == "pinnacle" for b in e.get("bookmakers", [])))
        info[name] = {"events": len(events), "books_seen": sorted(books),
                      "events_with_pinnacle": with_pin, "cost": r.headers.get("x-requests-last")}
        info["credits_left_after"] = int(r.headers.get("x-requests-remaining", "0"))
        if len(events) < C.MIN_ODDS_EVENTS[name]:
            warnings.append(f"odds/{name}: only {len(events)} events (offseason or bye?)")
            continue
        snap = {"pulled_at": NOW.isoformat(), "sport": sport, "events": compact_events(events)}
        writes[f"odds/{name}/{STAMP}.json"] = snap
        writes[f"odds/{name}/latest.json"] = snap
        notices.append(f"odds/{name}: {len(events)} events, {len(books)} books, Pinnacle on {with_pin}")
    notices.append(f"odds: {info.get('credits_left_after', left)} credits left this month")


# ---------------------------------------------------------------- injuries
def _flatten_espn_injuries(data):
    out = []
    for team in data.get("injuries", []):
        for inj in team.get("injuries", []):
            a = inj.get("athlete", {})
            out.append({
                "team": a.get("team", {}).get("abbreviation") or team.get("displayName"),
                "player": a.get("displayName"),
                "espn_id": inj.get("id"),
                "pos": a.get("position", {}).get("abbreviation"),
                "status": inj.get("status"),
                "type": inj.get("type", {}).get("abbreviation"),
                "detail": inj.get("details", {}).get("type"),
                "return_date": inj.get("details", {}).get("returnDate"),
                "updated": inj.get("date"),
                "note": (inj.get("shortComment") or "")[:160],
            })
    return out


def fetch_injuries():
    for league, critical in (("nfl", True), ("cfb", False)):
        url = C.SOURCES[f"espn_{league}_injuries"]
        try:
            data = get(url).json()
        except Exception as e:  # noqa: BLE001
            (errors if critical else warnings).append(f"injuries/{league}: {type(e).__name__}: {e}")
            continue
        rows = _flatten_espn_injuries(data)
        teams = {r["team"] for r in rows}
        newest = max((r["updated"] or "" for r in rows), default=None)
        summary["sources"][f"injuries_{league}"] = {"rows": len(rows), "teams": len(teams), "newest": newest}
        if league == "nfl" and len(teams) < C.MIN_NFL_INJURY_TEAMS:
            errors.append(f"injuries/nfl: only {len(teams)} teams (min {C.MIN_NFL_INJURY_TEAMS})")
            continue
        prev = RAW / "injuries" / league / "latest.json"
        if prev.exists():
            writes[f"injuries/{league}/previous.json"] = json.loads(prev.read_text())
        writes[f"injuries/{league}/latest.json"] = {"pulled_at": NOW.isoformat(), "rows": rows}
        notices.append(f"injuries/{league}: {len(rows)} entries across {len(teams)} teams, newest {newest}")


# ---------------------------------------------------------------- CFBD
def fetch_cfbd():
    key = os.environ.get("CFBD_API_KEY")
    if not key:
        warnings.append("cfbd: CFBD_API_KEY not set; skipped")
        return
    base, auth = C.SOURCES["cfbd_base"], {"Authorization": f"Bearer {key}"}
    cal = get(f"{base}/calendar", params={"year": C.SEASON}, headers=auth).json()
    upcoming = [w for w in cal if w.get("endDate", w.get("lastGameStart", "")) >= NOW.isoformat()[:10]]
    if not upcoming:
        warnings.append("cfbd: no upcoming week in calendar")
        return
    wk = upcoming[0]
    week, stype = wk["week"], wk.get("seasonType", "regular")
    params = {"year": C.SEASON, "week": week, "seasonType": stype}
    games = get(f"{base}/games", params=params, headers=auth).json()
    lines = get(f"{base}/lines", params=params, headers=auth).json()
    fbs = [g for g in games if g.get("homeClassification") == "fbs" or g.get("awayClassification") == "fbs"]
    with_lines = sum(1 for l in lines if l.get("lines"))
    summary["sources"]["cfbd"] = {"week": week, "season_type": stype, "games": len(games),
                                  "fbs_games": len(fbs), "games_with_lines": with_lines, "calls": 3}
    writes[f"cfbd/week_{week:02d}_games.json"] = games
    writes[f"cfbd/week_{week:02d}_lines.json"] = lines

    # current-season per-game efficiency for the college ratings (only weeks not yet saved)
    ppa_path = RAW / "cfbd" / f"ppa_{C.SEASON}.json"
    have = json.loads(ppa_path.read_text()) if ppa_path.exists() else []
    have_weeks = {p["week"] for p in have}
    new = []
    for w in range(1, week):
        if w in have_weeks:
            continue
        part = get(f"{base}/ppa/games", params={"year": C.SEASON, "week": w, "seasonType": "regular",
                                                "excludeGarbageTime": "true"}, headers=auth).json()
        new.extend(part)
        summary["sources"]["cfbd"]["calls"] += 1
    if new or not ppa_path.exists():
        writes[f"cfbd/ppa_{C.SEASON}.json"] = have + new
    # team names (school + mascot) to match the odds feed; pulled once
    if not (RAW / "cfbd" / "teams.json").exists():
        writes["cfbd/teams.json"] = get(f"{base}/teams", headers=auth).json()
        summary["sources"]["cfbd"]["calls"] += 1
    # preseason inputs (once per season) and SP+ as a second opinion (once per week)
    for name, path, params in (("talent", "/talent", {"year": C.SEASON}),
                               ("returning", "/player/returning", {"year": C.SEASON})):
        if not (RAW / "cfbd" / f"{name}_{C.SEASON}.json").exists():
            writes[f"cfbd/{name}_{C.SEASON}.json"] = get(f"{base}{path}", params=params, headers=auth).json()
            summary["sources"]["cfbd"]["calls"] += 1
    if not (RAW / "cfbd" / f"sp_{C.SEASON}_wk{week:02d}.json").exists():
        writes[f"cfbd/sp_{C.SEASON}_wk{week:02d}.json"] = get(
            f"{base}/ratings/sp", params={"year": C.SEASON}, headers=auth).json()
        summary["sources"]["cfbd"]["calls"] += 1
    # past weeks' games (final scores) for the ratings, pulled once per week
    for w in range(1, week):
        p = RAW / "cfbd" / f"week_{w:02d}_games.json"
        fbs = [g for g in json.loads(p.read_text()) if g.get("homeClassification") == "fbs"] if p.exists() else []
        done = bool(fbs) and sum(g.get("homePoints") is not None for g in fbs) >= 0.95 * len(fbs)
        if not done:
            writes[f"cfbd/week_{w:02d}_games.json"] = get(
                f"{base}/games", params={"year": C.SEASON, "week": w, "seasonType": "regular"}, headers=auth).json()
            summary["sources"]["cfbd"]["calls"] += 1
    notices.append(f"cfbd: week {week} ({stype}) {len(fbs)} FBS games, {with_lines} with lines")


# ---------------------------------------------------------------- main
def main():
    for fn in (fetch_nflverse, fetch_odds, fetch_injuries, fetch_cfbd):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {type(e).__name__}: {e}")

    for w in warnings:
        print(f"::warning::{w}")
    if errors:
        for e in errors:
            print(f"::error::{e}")
        print("::error::Guards failed; nothing written. Last good data stays live.")
        sys.exit(1)

    for rel, obj in writes.items():
        p = RAW / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        compact = rel.startswith(("odds/", "cfbd/", "injuries/"))
        p.write_text(obj if isinstance(obj, str) else
                     json.dumps(obj, separators=(",", ":")) if compact else json.dumps(obj, indent=1))
    (RAW / "_last_run.json").write_text(json.dumps(summary, indent=1))
    for n in notices:
        print(f"::notice::{n}")
    print(f"::notice::wrote {len(writes)} files")


if __name__ == "__main__":
    main()
