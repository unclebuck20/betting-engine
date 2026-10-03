# Data

## Sources
| Source | What | Auth / cost | Cadence | Notes |
|---|---|---|---|---|
| The Odds API | Spreads (NFL, college), totals (NFL) from 10 books incl. Pinnacle, BetOnline, LowVig, DraftKings, FanDuel; bet-slip links | Free key, 500 credits/month; 1 credit per market per league per pull | Daily baseline + pre-kickoff pulls for the league about to kick off (`schedule.py`) | Pinnacle is scraped from its public site and may lag slightly. Historical odds are paid-only (not used) |
| nflverse | NFL schedule, closing spread/total history, results, projected QBs, play-by-play (2013+), snap counts, FTN charting (2022+) | Free, no key (GitHub releases) | Schedules every 5 min; pbp nightly; snaps daily | Only closing lines historically, so NFL backtests are vs the close |
| ESPN (unofficial) | NFL injuries with status and notes | Free, no key | Every run | College endpoint is dead (3 entries from 2022) |
| CollegeFootballData | College games, DraftKings/Bovada lines with openers, per-game PPA, SP+, talent, returning production; 2021-25 history | Free key, 1,000 calls/month | Every run (~3-6 calls) | Whole-season pulls can 502; backfill falls back to week by week |

## Files
| Path | What |
|---|---|
| `data/raw/odds/{nfl,cfb}/YYYYMMDDTHHMMZ.json` + `latest.json` | Compact odds snapshots (spreads, totals, links); the closing line for grading is the last one before kickoff |
| `data/raw/nflverse/games.csv`, `data/raw/injuries/nfl/{latest,previous}.json`, `data/raw/cfbd/*`, `data/raw/cfbd_history/*` | Raw pulls |
| `data/derived/nfl_team_game.parquet`, `nfl_qb_game.parquet` | Per-game NFL team and QB stats (2013+) |
| `data/derived/nfl_features.parquet` | Pre-game ratings/features for every game 2015-2025 (walk-forward) |
| `data/derived/nfl_model_live.json` | Upcoming NFL games: model spread, total, starters, matchup notes; team table; QB ratings |
| `data/derived/nfl_injuries.json` | Team injury points, absences, change since last pull |
| `data/manual/cfb_injury_check.json` | Pre-slate college injury check: absences with sources, holds, one-line summary per game (written by the scheduled check) |
| `data/picks/published.json` | Picks that reached the page, with status and price history |
| `data/picks/model_log.csv` | Every non-pass card (the model's record) + grading columns |
| `data/bets.csv` | Garrett's logged bets + grading columns |
| `data/model/params.json` | Calibrated parameter overrides and change history |
| `docs/data/picks.json`, `bets.json`, `record.json` | What the page reads |

## Page data contract (`docs/data/picks.json`)
`meta` (built_at, odds_pulled_at, slate_cap_units) and `board[slot]` = {games, passed, best_pass, cap_applied_from, units_live, picks[]}. Each pick: pick_key, market (spread/total), league, tier, status, matchup, kickoff, side, line, price, book, link, fair_line, open_line, confidence, ev_pct, units, floor_text, model_home_margin, edges[], why, flags[], history[[time, line, price, book]], current{line, price, book, link, ev_pct, confidence}, latest{...}.

## Budgets
- Odds: ~200-250 credits/month expected; fetch stops pulling below 40 credits left to protect closing snapshots.
- CFBD: well under 1,000/month in normal weeks.
- GitHub Actions: free for public repos; runs every 30 min but usually exits in seconds when nothing is due.
- Repo growth: compact snapshots; a few MB per week.
