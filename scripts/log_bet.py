"""Log a bet from a 'BET:' GitHub issue into data/bets.csv and docs/data/bets.json."""
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "bets.csv"
PAGE = ROOT / "docs" / "data" / "bets.json"
FIELDS = ["logged_at", "issue", "pick_key", "market", "pick", "matchup", "kickoff_utc", "league", "book", "line", "price",
          "units", "model_tier", "model_ev_pct", "close_fair_line", "close_line", "clv_pts", "clv_ev_pct",
          "home_score", "away_score", "result", "units_won"]  # keep in sync with grade.BET_FIELDS


def parse(body):
    out = {}
    for line in (body or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            if k in FIELDS:
                out[k] = v.strip()
    return out


def main():
    body, number = os.environ.get("ISSUE_BODY", ""), os.environ.get("ISSUE_NUMBER", "")
    row = parse(body)
    if not row.get("pick_key"):
        print("::error::no pick_key in issue body"); sys.exit(1)
    row.update({"logged_at": datetime.now(timezone.utc).isoformat(), "issue": number, "result": "pending"})
    rows = []
    if CSV.exists():
        with open(CSV) as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r["pick_key"] != row["pick_key"]] + [row]  # re-submitting replaces
    CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    PAGE.parent.mkdir(parents=True, exist_ok=True)
    PAGE.write_text(json.dumps([{k: r.get(k, "") for k in FIELDS} for r in rows]))
    print(f"::notice::logged {row.get('pick', row['pick_key'])} ({row.get('units')}u at {row.get('book')})")


if __name__ == "__main__":
    main()
