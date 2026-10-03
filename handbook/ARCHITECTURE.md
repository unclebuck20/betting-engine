# Architecture

## Flow (every run)
```
schedule.py   decide: anything due? pull odds? which league?
fetch.py      odds (The Odds API) · NFL schedule/lines (nflverse) · NFL injuries (ESPN) · college games, lines, PPA, SP+ (CollegeFootballData)
edge.py       fair lines for every game at every book (debug table)
injuries.py   NFL starters (snap counts) x injury status -> points per team, and change since last pull
nfl_features.py  current-season play-by-play -> per-game team and QB tables
nfl_model.py  weekly ratings + QB ratings -> model spread and total for upcoming games
picks.py      cards: market + model + signals + rails + sizing; published-pick tracking; model log
grade.py      closing line, CLV, result, units for model picks and logged bets; record.json
review.py     (scheduled Claude session, not the Action) scout verdicts + college injury check -> data/manual/
calibrate.py  weekly: nudge weights/thresholds from CLV (min samples, small steps, bounds)
commit        data + docs/data -> GitHub Pages redeploys the site
```
Bets: **Took it** opens a pre-filled `BET:` GitHub issue → `log-bet.yml` → `log_bet.py` → `data/bets.csv` + `docs/data/bets.json`, issue closed.

## Market layer (`edge.py`)
- Fair line: Pinnacle; if missing, BetOnline/LowVig; else all-book consensus (capped at lean).
- De-vig: multiplicative. Fair expected margin solved by bisection so the de-vigged cover probability matches.
- Margin distributions: kernel-weighted final margins of past games with a similar closing spread (NFL 2006+, college FBS 2021-25; NFL totals the same way). Expected value of any line/price = win × payout − loss, pushes counted.

## College model (`cfb_ratings.py`)
- Exponentially weighted, opponent-adjusted offensive and defensive PPA per play (CFBD, garbage time excluded), 50% carried between seasons. Margin = 2.4-2.7 HFA + beta × rating gap.
- Live weight: target = opener + 30% × (model − opener), clipped to 10 pts; bet only what's left vs the current fair line, never below 8.5% of the current gap; zero in weeks 1-3.
- SP+ check: if ours disagrees with the market by 5+ and SP+ sides with the market, model weight → 0 (likely roster/QB change).

## NFL model (`nfl_model.py`)
- Per-game cells → weighted ridge regressions refit before every week (half-life 10 weeks, offseason = 12-week gap): pass EPA (on QB-game cells, QB as offset), rush EPA, pass/rush success, pressure, explosives, turnovers, plays (pace), neutral pass-rate over expected, special teams net EPA.
- QB ratings: long-window (half-life 40 weeks) opponent-adjusted EPA/dropback residuals shrunk with 250 dropbacks of replacement level (−0.06). Team pass offense = non-QB rating + expected starter; projected starters from nflverse, swapped to the most-used backup if ESPN lists the starter Out/Doubtful.
- Spread features: expected EPA per game difference (plays × per-play EPA mixed by pass tendency), special teams, success-rate and turnover differences, home field. Total features: sum of expected offensive EPA per game, total plays, wind, cold. Standardized ridge fit on all prior seasons.
- Trust: shift = 0 under 2 pts of model-vs-market disagreement, then 0.5 per extra point, max 1.75. Totals with 6+ pt disagreement are flagged and passed.

## Cards (`picks.py`)
- Tiers: play ≥2.5% EV, lean ≥1%, else pass. Cautions (stale-looking price, no sharp book, 28+ pt spread, model far off, model the other way) cap at lean; two cautions = pass.
- Size: quarter Kelly on EV, 0.5u steps, 2u college / 3u NFL; slate cap 8u (scale, then trim weakest).
- Slots: Thu, Fri = best single; Sat (college) and Sun (NFL early/late) = up to 10; SNF, MNF = best single.
- Signals: steam (sharp moved ≥1 pt since the last snapshot, your book hasn't), stale sharp price, NFL injury news not yet in the sharp line.
- Slate review (`SLATE_REVIEW.md`), a scheduled Claude session: **scout** verdicts on every published pick (agree / caution = half size / veto = no bet; never adds; graded separately on CLV) and the **college injury check** (key starter out on our side, not yet priced → *held*). Files in `data/manual/`.
- Published picks stay on the page until kickoff with a live status (still good / edge gone / line moved past floor / trimmed).

## Learning loop
`model_log.csv` holds every non-pass card at first sighting (the model's record). `grade.py` adds closing fair line, CLV in points and EV, scores, result, units. `calibrate.py` (weekly) adjusts `cfb_weight_open` (+ close), `nfl_injury_scale`, `lean_ev`, `play_ev` from 8-week CLV with minimum samples (30/15/40/40) and hard bounds; changes and evidence go to `data/model/params.json` and the page.
