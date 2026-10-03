"""Nightly cards: market + model + injuries + line movement -> picks, sizing, and tracking.

Layer 1 (market)  Fair line from the sharpest book, de-vigged, priced with empirical margin
                  distributions (edge.py) so key numbers carry their real weight.
Layer 2 (model)   College: in-season PPA power ratings. Backtest 2023-25 (weeks 4+): 57.7% ATS vs the
                  opener when the model disagrees by 5+, ~break-even vs the close. So the model's weight
                  slides from cfb_weight_open (a week out) to cfb_weight_close (kickoff). Weeks 1-3: no weight.
                  NFL: efficiency ratings add nothing beyond the closing line in backtests, so they only
                  veto. NFL edge comes from price and from injury news the sharp line hasn't absorbed yet.
Signals           Steam (sharp line moved, your book hasn't), stale sharp price, SP+ disagreement.
Slate review      A scheduled Claude session (handbook/SLATE_REVIEW.md) adds judgment the numbers can't:
                  scout verdicts on every published pick (agree / caution = half size / veto = no bet; never adds
                  a bet; graded separately) in data/manual/scout.json, and the college injury check (no feed
                  exists) in data/manual/cfb_injury_check.json: a key starter out on our side, unpriced, holds it.
Sizing            Quarter Kelly on EV, 0.5u steps, per-league cap, per-slate exposure cap.
Tracking          Every non-pass card goes to data/picks/model_log.csv (the model's record, graded on CLV).
                  Picks that reach the page are "published" and stay visible with a live status until kickoff.
"""
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402
import edge as E  # noqa: E402
import cfb_ratings as CR  # noqa: E402
from common import PT, RAW, CFBNames, load_snapshot, params, parse_ts, snapshot_paths  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DERIVED, OUT, PAGE = ROOT / "data" / "derived", ROOT / "data" / "picks", ROOT / "docs" / "data"
BOOK_NAMES = {"draftkings": "DraftKings", "fanduel": "FanDuel"}
SLOT_TOP_N = {"sat": 10, "sun": 10}

NFL_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LA", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def slot_for(league, kick_pt):
    wd, hr = kick_pt.strftime("%a"), kick_pt.hour
    if wd == "Thu":
        return "thu"
    if wd == "Fri":
        return "fri"
    if wd == "Sat":
        return "sat"          # college, plus late-season NFL Saturdays
    if wd == "Sun" and league == "nfl":
        return "snf" if hr >= 17 else "sun"
    if wd == "Mon" and league == "nfl":
        return "mnf"
    return None


def fmt_line(x):
    return "PK" if x == 0 else f"{x:+g}"


def short(team):
    return team.split()[-1]


def round_half(x):
    return round(x * 2) / 2


def nonqb_injury(t, P):
    if not t:
        return 0.0
    pts = sum(a["points"] for a in t.get("absences", []) if a["pos"] != "QB")
    return min(3.0, pts) * P["nfl_injury_scale"]


def trusted_shift(gap, trust):
    """Points to move the fair line toward the model (calibrated in nfl_model.py's walk-forward)."""
    a = abs(gap)
    if a <= trust["floor"]:
        return 0.0
    return math.copysign(min(trust["cap"], trust["slope"] * (a - trust["floor"])), gap)


# ------------------------------------------------------------ inputs
def cfb_week_info(names):
    """open lines + neutral-site flags keyed by (home school, away school)."""
    opens, neutral = {}, {}
    for f in (RAW / "cfbd").glob("week_*_lines.json"):
        for g in json.loads(f.read_text()):
            L = CR.pick_line(g.get("lines"))
            if L and L.get("spreadOpen") is not None:
                opens[(g["homeTeam"], g["awayTeam"])] = {"open_home": L["spreadOpen"], "provider": L["provider"]}
    for f in (RAW / "cfbd").glob("week_*_games.json"):
        for g in json.loads(f.read_text()):
            neutral[(g["homeTeam"], g["awayTeam"])] = bool(g.get("neutralSite"))
    return opens, neutral


def sp_ratings():
    files = sorted((RAW / "cfbd").glob(f"sp_{C.SEASON}_wk*.json"))
    if not files:
        return {}
    return {r["team"]: r["rating"] for r in json.loads(files[-1].read_text()) if r.get("rating") is not None}


def cfb_injury_check():
    """{odds event id: check} from the college injury check (handbook/SLATE_REVIEW.md)."""
    p = ROOT / "data" / "manual" / "cfb_injury_check.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()).get("games", {})
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"::warning::cfb_injury_check.json unreadable ({e}); ignoring it")
        return {}


def apply_scout(cards):
    """Scout verdicts (handbook/SLATE_REVIEW.md): caution halves the size, veto makes it no bet. Never adds."""
    p = ROOT / "data" / "manual" / "scout.json"
    try:
        scout = json.loads(p.read_text()).get("picks", {}) if p.exists() else {}
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"::warning::scout.json unreadable ({e}); ignoring it")
        return
    for c in cards:
        s = scout.get(c["pick_key"])
        if not s or s.get("verdict") not in ("agree", "caution", "veto"):
            continue
        c["scout"] = {"verdict": s["verdict"], "reason": s.get("reason", ""), "at": s.get("scouted_at"),
                      "sources": [u for u in s.get("sources", []) if str(u).startswith("http")][:3]}
        if s["verdict"] == "veto":
            c["vetoed"], c["tier"], c["units"] = True, "pass", 0.0
        elif s["verdict"] == "caution" and c["units"] > 0:
            c["units_pre_scout"] = c["units"]
            c["units"] = max(0.5, round_half(c["units"] / 2))


def previous_snapshot(league, now_pulled):
    """Most recent snapshot at least 45 minutes older than the current one."""
    cur = parse_ts(now_pulled)
    for p in reversed(snapshot_paths(league)):
        s = load_snapshot(p)
        if (cur - parse_ts(s["pulled_at"])).total_seconds() >= 45 * 60:
            return s
    return None


# ------------------------------------------------------------ one game
def evaluate(e, league, ctx, now):
    P, m = ctx["P"], ctx["models"][league]
    kick = parse_ts(e["t"])
    kick_pt = kick.astimezone(PT)
    slot = slot_for(league, kick_pt)
    if not slot or kick <= now or (kick - now).days > 6:
        return None
    lines = E.book_lines(e)
    mine = {b: L for b, L in lines.items() if b in C.MY_BOOKS}
    if not mine:
        return None
    hours = (kick - now).total_seconds() / 3600
    src, books = E.fair_source(lines)
    mu_mkt = E.fair_mu(m, lines, books)
    home, away = e["home"], e["away"]
    flags, notes, signals = [], [], []   # flags = cautions; notes = supporting reasons (for "why")
    mu_model, adj, cap_reasons = None, 0.0, []

    # ---- line movement since the previous snapshot: steam and stale sharp prices
    prev = ctx["prev"].get(league, {}).get(e["id"])
    sharp_move = None
    if prev:
        pl = E.book_lines(prev)
        if all(b in pl for b in books):
            sharp_move = mu_mkt - E.fair_mu(m, pl, books)
            soft_now, soft_prev = E.consensus_mu(m, lines), E.consensus_mu(m, pl)
            if soft_now is not None and soft_prev is not None:
                soft_move = soft_now - soft_prev
                if abs(sharp_move) < 0.25 and abs(soft_move) >= 1.0 and abs(mu_mkt - soft_now) >= 1.5:
                    cap_reasons.append("sharp price hasn't moved while every other book has; it may be stale")
            if abs(sharp_move) >= 1.0:
                signals.append(("steam", sharp_move, pl, ctx["prev_at"][league]))

    # ---- league-specific model layer
    if league == "nfl":
        ha, aa = NFL_ABBR.get(home), NFL_ABBR.get(away)
        g = ctx["nfl_live"].get((ha, aa))
        inj = ctx["inj"]
        # QBs are inside the model (starter-specific ratings); other injuries adjust it here
        ih, ia = nonqb_injury(inj.get(ha), P), nonqb_injury(inj.get(aa), P)
        if g:
            mu_model = g["model_margin"] - ih + ia
            for n in g.get("notes", []):
                notes.append(("matchup", n))
        # injury news since the last pull that the sharp line hasn't absorbed
        dh = inj.get(ha, {}).get("delta_since_last_pull", 0) * P["nfl_injury_scale"]
        da = inj.get(aa, {}).get("delta_since_last_pull", 0) * P["nfl_injury_scale"]
        expected = -dh + da           # change in home margin the news should cause
        if abs(expected) >= 1.0:
            unpriced = expected - (sharp_move or 0.0)
            if sharp_move is not None and unpriced * expected > 0 and abs(unpriced) >= 0.5:
                adj = max(-abs(expected), min(abs(expected), unpriced))
                who = ha if abs(dh) >= abs(da) else aa
                news = (inj.get(who, {}).get("new_absences") or [{}])[0]
                signals.append(("injury_news", adj, who, news))
        for abbr in (ha, aa):
            t = inj.get(abbr)
            if t and t.get("absences") and t["absences"][0]["points"] >= 0.5 and t["absences"][0]["pos"] != "QB":
                notes.append(("inj", abbr, t["absences"][0], nonqb_injury(t, P)))
        shift = trusted_shift(mu_model - mu_mkt, ctx["nfl_trust"]) if mu_model is not None else 0.0
        mu = mu_mkt + shift + adj
        weight = round(shift / (mu_model - mu_mkt), 3) if mu_model is not None and mu_model != mu_mkt else 0.0
    else:
        hs, as_ = ctx["names"].school(home), ctx["names"].school(away)
        is_neutral = ctx["neutral"].get((hs, as_), False)
        mu_model, _, _ = CR.live_margin(ctx["cfb_L"], home, away, is_neutral, hs, as_)
        o = ctx["opens"].get((hs, as_))
        open_mu = -o["open_home"] if o else None
        weight = P["cfb_weight_open"]
        if ctx["cfb_week"] < CR.MODEL_FIRST_WEEK:
            weight = 0.0
        shift = 0.0
        if mu_model is None:
            flags.append("no power rating for one of these teams yet")
        else:
            clip = P["cfb_gap_clip"]
            raw_gap = mu_model - mu_mkt
            gap = max(-clip, min(clip, raw_gap))
            sp = ctx["sp"]
            if hs in sp and as_ in sp:
                sp_margin = sp[hs] - sp[as_] + (0 if is_neutral else 2.5)
                sp_gap = sp_margin - mu_mkt
                if abs(raw_gap) >= 5 and abs(sp_gap) <= 2:
                    weight = 0.0
                    flags.append(f"our ratings disagree with SP+ and the market by {abs(raw_gap):.0f} pts; "
                                 f"likely a roster or QB change, so no model weight")
                elif abs(raw_gap) >= 3 and sp_gap * raw_gap > 0 and abs(sp_gap) >= abs(raw_gap) / 2:
                    notes.append(("sp", sp_gap))
            if weight > 0:
                # Backtest: the model's information is worth w_open x (model - opener). The line has
                # already moved some of that way; bet only what's left, and never less than the
                # close-line weight the backtest still supports.
                floor_shift = P["cfb_weight_close"] * gap
                if open_mu is not None:
                    gap_open = max(-clip, min(clip, mu_model - open_mu))
                    remaining = open_mu + weight * gap_open - mu_mkt
                    shift = remaining if remaining * gap > 0 and abs(remaining) > abs(floor_shift) else floor_shift
                    if abs(shift) > abs(weight * gap):
                        shift = weight * gap
                else:
                    frac = min(1.0, hours / 144.0)
                    shift = (P["cfb_weight_close"] + (P["cfb_weight_open"] - P["cfb_weight_close"]) * frac * 0.5) * gap
                weight = round(shift / gap, 3) if gap else 0.0
            if abs(raw_gap) > clip and weight > 0:
                ctx["_far"] = True
        mu = mu_mkt + shift
        if o:
            notes.append(("open", o))
        if e["id"] not in ctx["cfb_inj"]:
            flags.append("college injuries not checked yet")

    # ---- best side at my books
    dist = m.dist(mu)
    best = None
    for b, L in mine.items():
        for side, price in (("home", L["home_price"]), ("away", L["away_price"])):
            v = E.ev(dist, L["home_line"], side, price)
            if best is None or v > best["ev"]:
                best = {"book": b, "side": side, "home_line": L["home_line"], "price": price, "ev": v,
                        "link": L["hlink"] if side == "home" else L["alink"]}
    side, sgn = best["side"], (1 if best["side"] == "home" else -1)
    team, opp = (home, away) if side == "home" else (away, home)
    if league == "cfb":
        nm = {home: hs or home, away: as_ or away}
    else:
        nm = {home: short(home), away: short(away)}
    tn, on = nm[team], nm[opp]
    line = best["home_line"] * sgn
    w, _, l = E.outcome(dist, best["home_line"], side)
    cover = w / (w + l)
    cap = P["max_confidence_nfl"] if league == "nfl" else P["max_confidence"]
    if cover > cap:                          # never claim more than the backtest has shown
        cover = cap
        w, l = cover * (w + l), (1 - cover) * (w + l)
        best["ev"] = w * (E.dec(best["price"]) - 1) - l
    ev_price_only = E.ev(m.dist(mu_mkt), best["home_line"], side, best["price"])

    # ---- tiering and rails
    tier = "play" if best["ev"] >= P["play_ev"] else "lean" if best["ev"] >= P["lean_ev"] else "pass"
    if league == "nfl" and mu_model is not None and tier != "pass" and (mu_model - mu_mkt) * sgn <= -2:
        cap_reasons.append(f"our NFL model leans the other way by {abs(mu_model - mu_mkt):.1f} pts")
    if ctx.pop("_far", False):
        cap_reasons.append("our ratings are 10+ pts off the market; they may be missing a roster or QB change")
    if league == "cfb" and mu_model is not None and weight > 0 and tier == "play" and (mu_model - mu_mkt) * sgn < 0:
        cap_reasons.append("price edge only; our ratings favor the other side")
    if src == "consensus":
        cap_reasons.append("no sharp book has this game, so the fair line is a market average")
    if ev_price_only > P["stale_price_ev"]:
        cap_reasons.append("price is far better than the sharp line; confirm it's live before betting")
    if abs(line) > 28:
        cap_reasons.append("blowout spread; model and market are least reliable here")
    inj_check, held = None, False
    chk = ctx["cfb_inj"].get(e["id"]) if league == "cfb" else None
    if chk:
        hold = set(chk.get("hold") or [])
        held = bool(hold & {team, nm[team]})
        opp_held = bool(hold & {opp, nm[opp]})
        inj_check = {"at": chk.get("checked_at"), "text": chk.get("summary") or "No key starters out.",
                     "hold": held, "opp_out": opp_held,
                     "sources": [a["source"] for a in chk.get("absences", []) if a.get("source")][:3]}
    if len(cap_reasons) >= 2:
        tier = "pass"
    elif cap_reasons and tier == "play":
        tier = "lean"
    flags = cap_reasons + flags
    if held:                                  # key starter out on our side, line hasn't absorbed it
        tier = "pass"                         # the card's injury-check line says why

    # ---- units (before the slate cap)
    if tier == "play":
        f = best["ev"] / (E.dec(best["price"]) - 1)
        units = max(1.0, min(P[f"max_units_{league}"], round_half(100 * f * P["kelly_fraction"])))
    elif tier == "lean":
        units = 0.5
    else:
        units = 0.0

    floor = E.price_floor(m, mu, side, best["home_line"])
    floor_side = None if floor is None else floor * sgn
    be = E.breakeven_price(dist, best["home_line"], side)
    floor_text = (f"Good to {fmt_line(floor_side)} at -110" if floor_side is not None
                  else f"Only at {fmt_line(line)} {be:+d} or better" if be is not None else None)

    # ---- why: at most three sentences, each tied to a number
    fair_line = round_half(-mu_mkt * sgn)
    src_name = {"pinnacle": "Pinnacle", "sharp": "BetOnline/LowVig", "consensus": "The market average"}[src]
    why = [f"{src_name} makes {tn} {fmt_line(fair_line)}; {BOOK_NAMES.get(best['book'], best['book'])} "
           f"has {fmt_line(line)} ({best['price']:+d})."]
    for s in signals:
        if s[0] == "injury_news" and len(why) < 3:
            _, pts, who, news = s
            who_txt = f"{news.get('player')} ({news.get('status', '').lower()})" if news else "new injury news"
            why.append(f"{who}: {who_txt} since our last check is worth ~{abs(pts):.1f} pts the sharp line "
                       f"hasn't absorbed yet.")
        if s[0] == "steam" and len(why) < 3:
            mv = s[1] * sgn
            if mv > 0:
                why.append(f"Sharp money moved this {abs(s[1]):.1f} pts toward {tn} since "
                           f"{parse_ts(s[3]).astimezone(PT).strftime('%a %-I:%M %p')}; your book hasn't fully followed.")
            else:
                flags.append(f"sharp line moved {abs(s[1]):.1f} pts against this side recently")
    if league == "cfb" and mu_model is not None and weight > 0 and len(why) < 3:
        g = (mu_model - mu_mkt) * sgn
        if abs(g) >= 1.5:
            why.append(f"Our in-season power ratings have {tn} {abs(g):.1f} pts "
                       f"{'better' if g > 0 else 'worse'} than the market.")
    for n in notes:
        if len(why) >= 3:
            break
        if n[0] == "sp" and weight > 0:
            why.append("SP+ agrees with our ratings on the direction.")
        elif n[0] == "inj":
            _, abbr, top, pts = n
            why.append(f"{abbr} is without {top['player']} ({top['pos']}, {top['status'].lower()}); "
                       f"injuries are worth ~{pts:.1f} pts to them by our injury model.")
        elif n[0] == "matchup":
            continue                      # shown as the card's matchup-edges list instead
        elif n[0] == "open":
            o = n[1]
            open_side = o["open_home"] * sgn
            if abs(open_side - line) >= 1:
                toward = tn if line < open_side else on
                why.append(f"The line opened {fmt_line(open_side)} and has moved toward {toward}.")
    if league == "nfl" and mu_model is not None:
        g = (mu_model - mu_mkt) * sgn
        if abs(g) >= 1:
            why.insert(1, f"Our NFL model makes {tn} {fmt_line(round_half(-mu_model * sgn))}, "
                          f"{abs(g):.1f} pts {'better' if g > 0 else 'worse'} than the market.")

    open_line = None
    if league == "cfb":
        o = ctx["opens"].get((ctx["names"].school(home), ctx["names"].school(away)))
        open_line = o["open_home"] * sgn if o else None
    return {
        "id": e["id"], "pick_key": f"{e['id']}|{team}", "market": "spread", "league": league, "slot": slot,
        "tier": tier, "edges": [n[1] for n in notes if n[0] == "matchup"][:3],
        "matchup": f"{away} @ {home}", "kickoff_utc": e["t"],
        "kickoff_pt": kick_pt.strftime("%a %b %-d, %-I:%M %p %Z"),
        "side": team, "opponent": opp, "side_is_home": side == "home",
        "line": line, "price": best["price"], "book": best["book"], "link": best["link"],
        "fair_line": fair_line, "open_line": open_line,
        "confidence": round(cover * 100, 1), "ev_pct": round(best["ev"] * 100, 2), "units": units,
        "price_floor": floor_side, "floor_text": floor_text, "fair_source": src,
        "market_home_margin": round(mu_mkt, 1),
        "model_home_margin": round(mu_model, 1) if mu_model is not None else None,
        "model_weight": round(weight, 3), "hours_to_kick": round(hours, 1),
        "signals": sorted({s[0] for s in signals}),
        "why": " ".join(why[:3]), "flags": flags, "inj_check": inj_check, "held": held,
    }


def evaluate_total(e, ctx, now):
    """NFL totals: fair total from the sharp books vs our model total (pace x efficiency + weather)."""
    P, m = ctx["P"], ctx["models"]["nfl_total"]
    kick = parse_ts(e["t"])
    kick_pt = kick.astimezone(PT)
    slot = slot_for("nfl", kick_pt)
    if not slot or kick <= now or (kick - now).days > 6:
        return None
    tl = E.total_lines(e)
    mine = {b: L for b, L in tl.items() if b in C.MY_BOOKS}
    if not mine:
        return None
    src, books = E.fair_source(tl)
    mkt = E.fair_total(m, tl, books)
    home, away = e["home"], e["away"]
    g = ctx["nfl_live"].get((NFL_ABBR.get(home), NFL_ABBR.get(away)))
    trust = ctx["nfl_trust"]
    model_total = g["model_total"] if g else None
    cap_reasons, flags = [], []
    gap = (model_total - mkt) if model_total is not None else 0.0
    shift = trusted_shift(gap, trust)
    if abs(gap) > trust["total_max_gap"]:
        cap_reasons.append(f"model total is {abs(gap):.1f} pts off the market; usually missing weather or injury news")
        cap_reasons.append("totals backtest: 6+ pt disagreements went 45-54")
    mu = mkt + shift
    dist = m.dist(mu)
    best = None
    for b, L in mine.items():
        for side, price, link in (("Over", L["over"], L["olink"]), ("Under", L["under"], L["ulink"])):
            v = E.ev(dist, -L["line"], "home" if side == "Over" else "away", price)
            if best is None or v > best["ev"]:
                best = {"book": b, "side": side, "line": L["line"], "price": price, "ev": v, "link": link}
    hs = "home" if best["side"] == "Over" else "away"
    w, _, l = E.outcome(dist, -best["line"], hs)
    cover = w / (w + l)
    if cover > P["max_confidence_nfl"]:
        cover = P["max_confidence_nfl"]
        w, l = cover * (w + l), (1 - cover) * (w + l)
        best["ev"] = w * (E.dec(best["price"]) - 1) - l
    ev_price_only = E.ev(m.dist(mkt), -best["line"], hs, best["price"])
    if ev_price_only > P["stale_price_ev"]:
        cap_reasons.append("price is far better than the sharp total; confirm it's live before betting")
    if src == "consensus":
        cap_reasons.append("no sharp book has this total, so the fair number is a market average")
    tier = "play" if best["ev"] >= P["play_ev"] else "lean" if best["ev"] >= P["lean_ev"] else "pass"
    if len(cap_reasons) >= 2:
        tier = "pass"
    elif cap_reasons and tier == "play":
        tier = "lean"
    if tier == "play":
        f = best["ev"] / (E.dec(best["price"]) - 1)
        units = max(1.0, min(P["max_units_nfl"], round_half(100 * f * P["kelly_fraction"])))
    else:
        units = 0.5 if tier == "lean" else 0.0
    fl = E.price_floor(m, mu, hs, -best["line"])
    floor_total = None if fl is None else -fl
    be = E.breakeven_price(dist, -best["line"], hs)
    floor_text = (f"Good to {best['side'].lower()} {floor_total:g} at -110" if floor_total is not None
                  else f"Only at {best['line']:g} {be:+d} or better" if be is not None else None)
    src_name = {"pinnacle": "Pinnacle", "sharp": "BetOnline/LowVig", "consensus": "The market average"}[src]
    why = [f"{src_name}'s fair total is {round_half(mkt):g}; {BOOK_NAMES.get(best['book'], best['book'])} has "
           f"{best['side']} {best['line']:g} ({best['price']:+d})."]
    if model_total is not None and abs(gap) >= 1:
        why.append(f"Our model projects {model_total:.1f} points from both offenses' efficiency and pace"
                   f"{' and ' + str(int(g['wind'])) + ' mph wind' if g and not g['dome'] and g['wind'] >= 12 else ''}.")
    if g and g["dome"] and len(why) < 3:
        why.append("Indoors, so no weather adjustment.")
    flags = cap_reasons + flags
    return {
        "id": e["id"], "pick_key": f"{e['id']}|{best['side']}", "market": "total", "league": "nfl", "slot": slot,
        "tier": tier, "edges": (g or {}).get("notes", [])[:3],
        "matchup": f"{away} @ {home}", "kickoff_utc": e["t"], "kickoff_pt": kick_pt.strftime("%a %b %-d, %-I:%M %p %Z"),
        "side": best["side"], "opponent": "", "side_is_home": best["side"] == "Over",
        "line": best["line"], "price": best["price"], "book": best["book"], "link": best["link"],
        "fair_line": round_half(mkt), "open_line": None,
        "confidence": round(cover * 100, 1), "ev_pct": round(best["ev"] * 100, 2), "units": units,
        "price_floor": floor_total, "floor_text": floor_text, "fair_source": src,
        "market_home_margin": round(mkt, 1), "model_home_margin": round(model_total, 1) if model_total else None,
        "model_weight": round(shift / gap, 3) if gap else 0.0, "hours_to_kick": round((kick - now).total_seconds() / 3600, 1),
        "signals": [], "why": " ".join(why[:3]), "flags": flags,
    }


# ------------------------------------------------------------ published picks
def current_price(card_side, lines, mine_books, event=None):
    """Best current line/price for a given side across my books (ties go to the book it was published at)."""
    if card_side.get("market") == "total":
        tl = E.total_lines(event) if event else {}
        over = card_side["side"] == "Over"
        best = None
        for b in [card_side["book"]] + [x for x in mine_books if x != card_side["book"]]:
            L = tl.get(b)
            if not L:
                continue
            pr = L["over"] if over else L["under"]
            key = (-L["line"] if over else L["line"]) + (E.dec(pr) - 1.909) * 2.5
            if best is None or key > best["_k"] + 1e-9:
                best = {"line": L["line"], "price": pr, "book": b, "link": L["olink"] if over else L["ulink"], "_k": key}
        if best:
            best.pop("_k")
        return best
    best = None
    order = [card_side["book"]] + [b for b in mine_books if b != card_side["book"]]
    for b in order:
        L = lines.get(b)
        if not L:
            continue
        home = card_side["side_is_home"]
        ln = L["home_line"] if home else -L["home_line"]
        pr = L["home_price"] if home else L["away_price"]
        key = ln + (E.dec(pr) - 1.909) * 2.5      # ~20 cents of price ~ half a point
        if best is None or key > best["line"] + (E.dec(best["price"]) - 1.909) * 2.5 + 1e-9:
            best = {"line": ln, "price": pr, "book": b, "link": L["hlink"] if home else L["alink"]}
    return best


def update_published(pub, cards, events, ctx, now):
    by_key = {c["pick_key"]: c for c in cards}
    for key, p in pub.items():
        kick = parse_ts(p["kickoff_utc"])
        if kick <= now:
            p["status"] = "started"
            continue
        c = by_key.get(key)
        ev_ = events.get(p["id"])
        if ev_ is None:
            p["status"] = "off_board"
            continue
        lines = E.book_lines(ev_)
        cur = current_price(p, lines, C.MY_BOOKS, ev_)
        if cur:
            last = p["history"][-1] if p["history"] else None
            if not last or (last[1], last[2], last[3]) != (cur["line"], cur["price"], cur["book"]):
                p["history"].append([now.isoformat(), cur["line"], cur["price"], cur["book"]])
            p["current"] = {**cur, "ev_pct": c["ev_pct"] if c else None,
                            "confidence": c["confidence"] if c else None}
        floor = p.get("price_floor")
        if c:
            p["inj_check"], p["scout"] = c.get("inj_check"), c.get("scout")
        if c and c.get("held"):
            p["status"] = "held"
        elif c and c.get("vetoed"):
            p["status"] = "vetoed"
        elif c and c["tier"] != "pass":
            p["status"] = "live"
        elif cur and floor is not None and (cur["line"] > floor if p.get("market") == "total" and p["side"] == "Over"
                                            else cur["line"] < floor):
            p["status"] = "moved"
        else:
            p["status"] = "faded"
        if c:
            p["latest"] = {k: c[k] for k in ("tier", "ev_pct", "confidence", "line", "price", "book", "why", "flags")}
    return pub


def apply_slate_cap(items, cap):
    """Scale live picks down to the slate cap; if 0.5u minimums still overflow, trim the weakest."""
    for x in items:
        x.pop("units_uncapped", None)
    live = [x for x in items if x["status"] == "live"]
    total = sum(x["units"] for x in live)
    if total <= cap or total == 0:
        return None
    f = cap / total
    for x in live:
        x["units_uncapped"] = x["units"]
        x["units"] = max(0.5, round_half(x["units"] * f))
    by_ev = sorted(live, key=lambda x: x["ev_pct"])
    while sum(x["units"] for x in live if x["status"] == "live") > cap:
        big = [x for x in by_ev if x["status"] == "live" and x["units"] > 0.5]
        if big:
            big[0]["units"] -= 0.5
        else:
            weakest = next(x for x in by_ev if x["status"] == "live")
            weakest["status"], weakest["units"] = "trimmed", 0.0
    return round(total, 1)


# ------------------------------------------------------------ main
def main():
    now = datetime.now(timezone.utc)
    P = params()
    names = CFBNames()
    opens, neutral = cfb_week_info(names)
    live_path = DERIVED / "nfl_model_live.json"
    nfl_live = json.loads(live_path.read_text()) if live_path.exists() else {"games": {}, "trust": None}
    nfl_by_teams = {}
    for gid, g in sorted(nfl_live["games"].items(), key=lambda kv: kv[1]["week"]):
        nfl_by_teams.setdefault((g["home"], g["away"]), g)
    inj_path = DERIVED / "nfl_injuries.json"
    cfb_games = [json.loads(f.read_text()) for f in sorted((RAW / "cfbd").glob("week_*_games.json"))]
    ctx = {
        "P": P, "models": E.models(), "names": names, "opens": opens, "neutral": neutral,
        "nfl_live": nfl_by_teams,
        "nfl_trust": nfl_live.get("trust") or {"floor": 2.0, "slope": 0.5, "cap": 1.75, "total_max_gap": 6.0},
        "inj": json.loads(inj_path.read_text())["teams"] if inj_path.exists() else {},
        "cfb_L": CR.live_ratings(C.SEASON), "sp": sp_ratings(), "cfb_inj": cfb_injury_check(),
        "cfb_week": max((g[0]["week"] for g in cfb_games if g), default=0),
        "prev": {}, "prev_at": {},
    }
    cards, events_by_id, pulled = [], {}, {}
    for league in ("nfl", "cfb"):
        p = RAW / "odds" / league / "latest.json"
        if not p.exists():
            continue
        snap = load_snapshot(p)
        pulled[league] = snap["pulled_at"]
        prev = previous_snapshot(league, snap["pulled_at"])
        ctx["prev"][league] = {e["id"]: e for e in prev["events"]} if prev else {}
        ctx["prev_at"][league] = prev["pulled_at"] if prev else None
        for e in snap["events"]:
            events_by_id[e["id"]] = e
            for c in (evaluate(e, league, ctx, now) if any("hl" in b for b in e["books"].values()) else None,
                      evaluate_total(e, ctx, now) if league == "nfl" else None):
                if c:
                    c["odds_pulled_at"] = snap["pulled_at"]
                    cards.append(c)

    apply_scout(cards)

    if names.misses:
        miss = sorted(names.misses)
        n_cfb = sum(1 for c in cards if c["league"] == "cfb")
        level = "error" if len(miss) > max(3, 0.1 * n_cfb) else "warning"
        print(f"::{level}::college names not matched ({len(miss)}): {', '.join(miss[:12])}")
        if level == "error":
            sys.exit(1)

    # ---- choose what goes on the page
    OUT.mkdir(parents=True, exist_ok=True)
    pub_path = OUT / "published.json"
    pub = json.loads(pub_path.read_text()) if pub_path.exists() else {}
    by_slot = {}
    for c in cards:
        by_slot.setdefault(c["slot"], []).append(c)
    board = {}
    for slot, cs in by_slot.items():
        cs.sort(key=lambda c: -c["ev_pct"])
        top = [c for c in cs if c["tier"] != "pass"][:SLOT_TOP_N.get(slot, 1)]
        for c in top:
            if c["pick_key"] not in pub:
                pub[c["pick_key"]] = {**c, "published_at": now.isoformat(), "status": "live",
                                      "history": [[now.isoformat(), c["line"], c["price"], c["book"]]]}
        best_pass = None if top else (cs[0] if cs else None)
        board[slot] = {"games": len(cs), "passed": sum(1 for c in cs if c["tier"] == "pass"),
                       "best_pass": best_pass}
    pub = update_published(pub, cards, events_by_id, ctx, now)
    # every slot that has a card this run OR a published pick still to play
    for sl in {p["slot"] for p in pub.values() if p["status"] != "started"}:
        board.setdefault(sl, {"games": 0, "passed": 0, "best_pass": None})
    for slot in board:
        mine = [p for p in pub.values() if p["slot"] == slot and p["status"] != "started"]
        for p in mine:
            c = next((x for x in cards if x["pick_key"] == p["pick_key"]), None)
            if c and p["status"] == "live":
                p["units"] = c["units"]
        board[slot]["cap_applied_from"] = apply_slate_cap(mine, P["slate_cap_units"])
        board[slot]["picks"] = sorted(
            mine,
            key=lambda p: ({"live": 0, "held": 1, "vetoed": 2, "faded": 3, "moved": 4, "trimmed": 5, "off_board": 6}.get(p["status"], 5),
                           0 if (p.get("latest") or p)["tier"] == "play" else 1, -p["ev_pct"]))
        board[slot]["units_live"] = sum(p["units"] for p in mine if p["status"] == "live")
    pub_path.write_text(json.dumps(pub, indent=1))

    meta = {"built_at": now.isoformat(), "odds_pulled_at": pulled, "params": P, "my_books": C.MY_BOOKS,
            "cfb_week": ctx["cfb_week"]}
    (OUT / "latest.json").write_text(json.dumps({"meta": meta, "board": board, "all": cards}, indent=1))
    PAGE.mkdir(parents=True, exist_ok=True)
    (PAGE / "picks.json").write_text(json.dumps({"meta": {"built_at": meta["built_at"], "odds_pulled_at": pulled,
                                                          "slate_cap_units": P["slate_cap_units"]},
                                                 "board": board}))
    log_model(cards, now)
    n = sum(len(b["picks"]) for b in board.values())
    print(f"::notice::picks: {n} on the page from {len(cards)} games; "
          f"signals: {sum(1 for c in cards if c['signals'])} games")
    return board


LOG_FIELDS = ["logged_at", "pick_key", "game_id", "market", "league", "slot", "tier", "kickoff_utc", "matchup", "side",
              "side_is_home", "line", "price", "book", "confidence", "ev_pct", "units", "fair_source", "fair_line",
              "market_home_margin", "model_home_margin", "model_weight", "hours_to_kick", "signals",
              "close_fair_line", "close_line", "clv_pts", "clv_ev_pct", "home_score", "away_score", "result",
              "units_won"]


def log_model(cards, now):
    """The model's own record: first sighting of every non-pass card, graded later by grade.py."""
    path = OUT / "model_log.csv"
    seen = set()
    if path.exists():
        with open(path) as f:
            rd = csv.DictReader(f)
            old_rows, header = list(rd), rd.fieldnames
        if header != LOG_FIELDS:                 # columns changed: rewrite once with the new header
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows({**r, "market": r.get("market") or "spread"} for r in old_rows)
        seen = {r["pick_key"] for r in old_rows}
    new = [{**{k: c.get(k) for k in LOG_FIELDS}, "logged_at": now.isoformat(), "game_id": c["id"],
            "signals": "|".join(c["signals"])}
           for c in cards if c["tier"] != "pass" and c["pick_key"] not in seen]
    if not new:
        return
    header = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if header:
            w.writeheader()
        w.writerows(new)


if __name__ == "__main__":
    b = main()
    for slot in ("thu", "fri", "sat", "sun", "snf", "mnf"):
        if slot not in b:
            continue
        s = b[slot]
        print(f"\n== {slot.upper()} ({s['games']} games, {s['passed']} passed, {s['units_live']}u live"
              f"{', capped from ' + str(s['cap_applied_from']) + 'u' if s.get('cap_applied_from') else ''})")
        for c in s["picks"]:
            print(f" [{c['status']:<5}|{c['tier']:<4}] {c['side']} {fmt_line(c['line'])} ({c['price']:+d}) "
                  f"{c['book']:<10} conf {c['confidence']}% EV {c['ev_pct']:+.1f}% {c['units']}u "
                  f"{c['floor_text']} | {c['kickoff_pt']} {c['signals']}")
            print(f"        {c['why']}")
            if c["flags"]:
                print(f"        flags: {'; '.join(c['flags'])}")
        if s.get("best_pass"):
            bp = s["best_pass"]
            print(f" [no bet] best available: {bp['side']} {fmt_line(bp['line'])} EV {bp['ev_pct']:+.1f}%")
