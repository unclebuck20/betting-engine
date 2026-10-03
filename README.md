# Ballard Syndicate

College football and NFL picks, built sharp-first and graded on closing line value.

**Page:** `docs/index.html` (GitHub Pages from `/docs`). One tab per slot (Thu, Fri, Sat top 10, Sun top 10, SNF, MNF). Each card shows the side, line, price, book, confidence, size, edge, fair line, line movement, a three-sentence reason, cautions, **Took it**, and a bet-slip link. **My bets** shows your record vs the model's, closing-line value, and what the model has changed about itself.

## How a pick is made

| Layer | What it does | Evidence |
|---|---|---|
| Market | Fair line from the sharpest book (Pinnacle > BetOnline/LowVig > consensus), de-vigged, priced with **empirical margin distributions** so key numbers carry their real weight | College final margins land on 3 about 8% of the time near a 3-pt spread; a bell curve says 2.6% |
| College model | In-season PPA power ratings. Weight = 30% of (model − opener), minus however much the line has already moved that way; never below 8.5% of the current gap | 2023-25 backtest, weeks 4+: 57.7% ATS vs the opener when the gap is 5+ (370-271); lines moved toward the model 71% of the time |
| NFL model | Weekly-refit ridge ratings on every play since 2013: pass/rush offense and defense (opponent- and home-adjusted, recency-weighted), QB-specific ratings with backups (starter swaps when the projected QB is out), success rate, turnovers, special teams, pace. Spread = expected EPA per game for each offense (plays x per-play EPA, mixed by pass tendency) + ST; total = both offenses' output + pace + wind + cold. Trusted only past a 2-pt disagreement, up to 1.75 pts | Walk-forward 2017-25 vs **closing** lines: spreads 296-243 (54.9%) at 4+ pt disagreement; totals 192-147 (56.6%) at 4-6 pts, 45-54 at 6+ (flagged, not bet) |
| Tested, not used (NFL) | Pass rush vs protection, explosive-play, blitz and play-action matchup interactions; rest, bye, travel, time zone, division | No out-of-sample improvement; the market prices them |
| NFL injuries | QBs through the model's starter ratings; other starters (snap-count based) via position values; plus **injury news the sharp line hasn't absorbed** since the last pull | |
| Signals | Steam (sharp line moved, your book hasn't), stale sharp price, SP+ disagreement (zeroes the model when our ratings and SP+ + the market disagree by 5+) | |
| Rails | Confidence capped at 59% (best backtested rate); cautions cap a play at lean, two cautions = pass | |
| Sizing | Quarter Kelly on EV, 0.5u steps, 2u college / 3u NFL max, **8u per slate** (scaled, then weakest trimmed) | |

Tested and rejected: a preseason prior from returning production + roster talent. It improves accuracy but the market already prices that information, so it cut ATS vs the opener from 56.9% to 53.9% (weeks 4-6).

## Learning loop

1. `picks.py` logs every non-pass card to `data/picks/model_log.csv` (the model's record) and keeps page picks in `data/picks/published.json` with a live status (still good / edge gone / line moved / trimmed) until kickoff.
2. `grade.py` finds each pick's closing line (last snapshot before kickoff), computes CLV in points and in expected value, and grades results from nflverse / CollegeFootballData.
3. `calibrate.py` (weekly) nudges the college model weight, NFL injury scale and the lean/play thresholds based on CLV, with minimum samples, small steps and hard bounds. Changes appear on the page.

## Running

- `refresh.yml`: ~17 scheduled runs a week (daily 7 AM PT plus pre-kickoff windows), about 150 of the 500 free odds credits a month. Raw data is committed even if a later step fails.
- `log-bet.yml`: a `BET:` issue opened by the repo owner is parsed into `data/bets.csv` and closed.
- `backfill.yml`: one-time college history (games, lines, PPA, talent, returning production 2021-25).
- Secrets: `ODD_API_KEY`, `CFBD_API_KEY`.
- `tests/smoke_test.py`: phone-width browser test (both themes, every tab, Took it, $ toggle).

Bets are logged in units; the dollar toggle on the page is stored only on your device.
