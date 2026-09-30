"""One-time college history pull (games, closing+opening lines, per-game PPA) for model backtests.

~3 CFBD calls per season. Run from the 'backfill' workflow.
"""
import json
import os
import sys
from pathlib import Path

import time

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "raw" / "cfbd_history"
BASE = "https://api.collegefootballdata.com"
SEASONS = range(2021, 2026)
WEEKS = range(1, 17)
calls = 0


def main():
    key = os.environ.get("CFBD_API_KEY")
    if not key:
        print("::error::CFBD_API_KEY not set"); sys.exit(1)
    h = {"Authorization": f"Bearer {key}"}
    OUT.mkdir(parents=True, exist_ok=True)
    for y in SEASONS:
        for name, path, extra in (
            ("games", "/games", {}),
            ("lines", "/lines", {}),
            ("ppa", "/ppa/games", {"excludeGarbageTime": "true"}),
        ):
            dest = OUT / f"{name}_{y}.json"
            if dest.exists():
                continue
            base = {"year": y, "seasonType": "regular", **extra}
            data = get(path, base, h)
            if data is None:  # whole season too big -> week by week
                data = []
                for w in WEEKS:
                    part = get(path, {**base, "week": w}, h)
                    if part is None:
                        print(f"::warning::{name} {y} wk{w}: failed")
                        continue
                    data.extend(part)
            dest.write_text(json.dumps(data))
            print(f"::notice::{name} {y}: {len(data)} rows")
    print(f"::notice::CFBD calls used: {calls}")


def get(path, params, h):
    global calls
    for attempt in range(2):
        calls += 1
        r = requests.get(BASE + path, params=params, headers=h, timeout=90)
        if r.status_code == 200:
            time.sleep(1)
            return r.json()
        time.sleep(5)
    return None

if __name__ == "__main__":
    main()
