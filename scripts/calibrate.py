"""Weekly calibration: nudge the model's settings toward what its graded picks say works.

Graded on closing-line value (CLV), not win/loss: CLV settles in ~50 picks, win/loss needs thousands.
Every rule needs a minimum sample, moves in small steps, and stays inside hard bounds, so a bad week
can't swing the model. Each change is written to data/model/params.json with its evidence and shows
up on the page under "What the model learned".
"""
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import DEFAULT_PARAMS, MODEL_DIR, params, parse_ts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MODEL_LOG = ROOT / "data" / "picks" / "model_log.csv"
LOOKBACK_DAYS = 56          # last 8 weeks of picks

RULES = [
    # (param, selector, min_n, raise_if_clv_above, lower_if_clv_below, step (multiplicative or additive), bounds)
    {"param": "cfb_weight_open", "desc": "college picks where our ratings drove the edge (model gap 3+)",
     "select": lambda r: r["league"] == "cfb" and r["model_home_margin"] not in ("", None)
     and abs(float(r["model_home_margin"]) - float(r["market_home_margin"])) >= 3 and float(r["model_weight"] or 0) > 0,
     "min_n": 30, "up_above": 1.0, "down_below": -0.5, "mult": (1.10, 0.85), "bounds": (0.10, 0.45)},
    {"param": "nfl_injury_scale", "desc": "NFL picks driven by injury news",
     "select": lambda r: r["league"] == "nfl" and "injury_news" in (r["signals"] or ""),
     "min_n": 15, "up_above": 1.0, "down_below": -0.5, "mult": (1.10, 0.85), "bounds": (0.5, 1.5)},
    {"param": "lean_ev", "desc": "small edges (between the lean and play thresholds)",
     "select": lambda r, P=None: False,  # replaced at runtime with the EV band
     "min_n": 40, "up_above": None, "down_below": 0.0, "add": 0.0025, "bounds": (0.0075, 0.02),
     "tighten_when_bad": True},
    {"param": "play_ev", "desc": "edges at or above the play threshold",
     "select": lambda r, P=None: False,  # replaced at runtime with the EV band
     "min_n": 40, "up_above": None, "down_below": 0.0, "add": 0.0025, "bounds": (0.02, 0.04),
     "tighten_when_bad": True},
]


def main(force=False):
    now = datetime.now(timezone.utc)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    pf = MODEL_DIR / "params.json"
    state = json.loads(pf.read_text()) if pf.exists() else {"params": {}, "changes": [], "updated_at": None}
    if not force and state.get("updated_at") and now - parse_ts(state["updated_at"]) < timedelta(days=6):
        print("::notice::calibrate: ran within the last 6 days; skipping")
        return
    if not MODEL_LOG.exists():
        print("::notice::calibrate: no graded picks yet")
        return
    with open(MODEL_LOG) as f:
        rows = [r for r in csv.DictReader(f) if r.get("clv_ev_pct") not in ("", None)
                and now - parse_ts(r["kickoff_utc"]) <= timedelta(days=LOOKBACK_DAYS)]
    P = params()
    changes = []
    ev = lambda r: float(r.get("ev_pct") or 0) / 100  # noqa: E731
    band = {"lean_ev": lambda r: P["lean_ev"] <= ev(r) < P["play_ev"], "play_ev": lambda r: ev(r) >= P["play_ev"]}
    for rule in RULES:
        pick = band.get(rule["param"], rule["select"])
        sel = [r for r in rows if pick(r)]
        n = len(sel)
        if n < rule["min_n"]:
            continue
        clv = sum(float(r["clv_ev_pct"]) for r in sel) / n
        old = P[rule["param"]]
        new = old
        if "mult" in rule:
            if clv > rule["up_above"]:
                new = old * rule["mult"][0]
            elif clv < rule["down_below"]:
                new = old * rule["mult"][1]
        else:
            if clv < rule["down_below"]:
                new = old + rule["add"]          # demand a bigger edge before betting
            elif clv > 1.5 and old > DEFAULT_PARAMS[rule["param"]]:
                new = old - rule["add"]          # relax back toward the default when it's working
        lo, hi = rule["bounds"]
        new = round(max(lo, min(hi, new)), 4)
        if new != old:
            if rule["param"] == "cfb_weight_open":   # keep the close weight in proportion
                P["cfb_weight_close"] = round(P["cfb_weight_close"] * new / old, 4)
            P[rule["param"]] = new
            changes.append({"date": now.strftime("%Y-%m-%d"), "param": rule["param"], "from": old, "to": new,
                            "evidence": f"{rule['desc']}: avg closing-line value {clv:+.2f}% over {n} picks"})
    state["params"] = {k: v for k, v in P.items() if v != DEFAULT_PARAMS.get(k)}
    state["changes"] = state.get("changes", []) + changes
    state["updated_at"] = now.isoformat()
    state["last_run_note"] = (f"{len(changes)} change(s)" if changes else
                              f"no change: {len(rows)} graded picks in the last {LOOKBACK_DAYS} days; "
                              f"every rule needs its minimum sample and a clear signal")
    pf.write_text(json.dumps(state, indent=1))
    print(f"::notice::calibrate: {state['last_run_note']}")
    for c in changes:
        print(f"::notice::{c['param']}: {c['from']} -> {c['to']} ({c['evidence']})")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
