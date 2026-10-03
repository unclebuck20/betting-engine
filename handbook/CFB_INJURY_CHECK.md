# College injury check (pre-slate)

College football has no injury feed (ESPN's college endpoint is dead), so a scheduled Claude session checks before each college slate. It writes one file, `data/manual/cfb_injury_check.json`. `picks.py` holds any pick whose side is listed in a game's `hold` and shows each checked game's summary on its card.

**Schedule (PT):** hourly at :45 on Saturdays 6:45 AM-7:45 PM and Thu/Fri 11:45 AM-6:45 PM (two scheduled tasks, same prompt).
- **Morning run** (the first run of the day): every college pick in the next 16 hours. Catches news from earlier in the week that the line may not have absorbed yet.
- **Pre-kick runs** (every later run): only picks kicking off 45-105 minutes out, when availability reports and game-day news land. Most hours have no games in the window, so the run stops immediately.

Late news is almost always priced by the sharp books within minutes, so the check is protection, not an edge: it stops a bet on a pick whose QB was just ruled out. **On college slates, bet a pick only after its card shows the pre-kick injury check line (about an hour before kickoff).**

## Procedure (what the session does)
1. `cd` into the `betting-engine` repo (clone `unclebuck20/betting-engine` if it isn't there), then `git pull`.
2. `python scripts/cfb_injury_check.py list` prints `mode` (morning or prekick) and the games to check, with our side, the line, the opener and line history. If `games` is empty, stop right away: no research, no commit.
3. For each game, research **both** teams with web search:
   - Starting QB status first, then other key starters (top skill players, starting OL/edge/CB if clearly a top player).
   - Use the conference's official availability report if it publishes one, plus team/beat-reporter news, coach press conferences, ESPN, On3, 247Sports and The Athletic.
   - Use only news from this week, and note when each item broke.
4. Decide holds. Put a team in `hold` only when **all three** are true:
   - **Who:** the player is the starting QB, or clearly one of the team's two or three most important players.
   - **Status:** listed or reported **out** or **doubtful**. Questionable or game-time decision goes in the summary, not the hold.
   - **Not priced:** the line has **not** already moved for it. Compare the line history and the opener with when the news broke. Rough guide: a starting QB out is worth 3-7 pts to the line, depending on the backup; another star 0.5-1.5. If the line moved by about that much toward the opponent after the news, it's priced, so don't hold.

   List the team that is missing the player; the engine only holds a pick when that team is the side we're betting. An unpriced absence on the opponent still goes in `hold`: the engine shows it but doesn't bet more because of it.
5. Write `data/manual/cfb_injury_check.json`. Keep entries already in the file for other games, and replace entries for games checked now. Set top-level `checked_at` to now; in **morning** mode also set top-level `morning_at` to now (that's what makes later runs pre-kick).
   - Team names must be copied **exactly** from `matchup`.
   - `summary` is one plain sentence, 160 characters or fewer, e.g. "Iowa QB (name) out, line unmoved; Ohio State healthy." or "No key starters out."
   ```json
   {"checked_at": "2026-10-03T13:45:00Z", "slate": "sat",
    "games": {"<id from list>": {
      "matchup": "Ohio State Buckeyes @ Iowa Hawkeyes", "kickoff_utc": "2026-10-03T19:30:00Z",
      "checked_at": "2026-10-03T13:45:00Z",
      "absences": [{"team": "Iowa Hawkeyes", "player": "Name", "pos": "QB", "status": "out",
                    "key": true, "priced": false, "broke": "2026-10-02", "source": "https://..."}],
      "hold": ["Iowa Hawkeyes"],
      "summary": "Iowa QB Name out, line unmoved since Tuesday; Ohio State healthy."}}}
   ```
6. `python scripts/cfb_injury_check.py validate`, and fix anything it reports.
7. Commit and push with a short message like `cfb injury check (prekick): sat 3:45pm, 4 games, 1 hold`:
   ```
   git add data/manual/cfb_injury_check.json
   git commit -m "..."
   for i in 1 2 3; do git pull --rebase -X theirs origin main && git push && break; sleep 10; done
   ```
   Then `python scripts/cfb_injury_check.py dispatch` starts the refresh so the page updates within ~5 minutes, without spending odds credits. If that fails, the half-hourly run picks it up.
8. Finish with a short report: games checked, holds, and anything questionable worth watching.

## Rules
- When in doubt, don't hold: name it in the summary. Holds are for clear, sourced news.
- Never edit any file other than the check file. Never change picks, params or code.
- If web search is unavailable, write nothing and say so in the report.
