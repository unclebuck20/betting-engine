# betting-engine

Market-anchored NFL and college football picks, tracked on closing line value.

- `scripts/fetch.py` pulls odds (The Odds API), schedules and closing lines (nflverse), injuries (ESPN), and college games and lines (CollegeFootballData) into `data/raw/`.
- Guards fail closed: if a critical source looks wrong, nothing is written and last good data stays live.
- Secrets: `ODDS_API_KEY`, `CFBD_API_KEY` (Settings → Secrets and variables → Actions).
- Bets are logged in units only.
