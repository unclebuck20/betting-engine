"""One-time college history pull (games, closing+opening lines, per-game PPA) for model backtests.

~3 CFBD calls per season. Run from the 'backfill' workflow.
"""
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "raw" / "cfbd_history"
BASE = "https://api.collegefootballdata.com"
SEASONS = range(2019, 2026)


def main():
    key = os.environ.get("CFBD_API_KEY")
    if not key:
        print("::error::CFBD_API_KEY not set"); sys.exit(1)
    h = {"Authorization": f"Bearer {key}"}
    OUT.mkdir(parents=True, exist_ok=True)
    for y in SEASONS:
        for name, path, params in (
            ("games", "/games", {"year": y, "seasonType": "both"}),
            ("lines", "/lines", {"year": y, "seasonType": "both"}),
            ("ppa", "/ppa/games", {"year": y, "excludeGarbageTime": "true"}),
        ):
            r = requests.get(BASE + path, params=params, headers=h, timeout=90)
            if r.status_code != 200:
                print(f"::warning::{name} {y}: HTTP {r.status_code} {r.text[:150]}")
                continue
            data = r.json()
            (OUT / f"{name}_{y}.json").write_text(json.dumps(data))
            print(f"::notice::{name} {y}: {len(data)} rows")


if __name__ == "__main__":
    main()
