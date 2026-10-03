# Slate review (scheduled Claude session)

The engine picks sides, prices and sizes by formula. The slate review adds the judgment numbers can't capture, in two jobs, and is held to the same standard as the model: every verdict is graded on closing-line value (CLV).

| Job | Leagues | What it can do | File |
|---|---|---|---|
| **Scout** | NFL + college | One verdict per published pick: **agree**, **caution** (half size) or **veto** (no bet). **It can never add a bet or increase a size.** | `data/manual/scout.json` (kept all season: it is the scout's record) |
| **Injury check** | College (no feed exists) | Hold a pick when a key starter on our side is out and the line hasn't absorbed it | `data/manual/cfb_injury_check.json` (pruned at kickoff) |

`picks.py` applies both on the next refresh; `grade.py` grades every reviewed pick as if bet at full size, vetoes included, so the page's **Scout verdicts** table shows whether vetoed and caution picks really do worse than agreed ones. Around 50 verdicts in, that decides whether the scout gets more say, keeps its current role, or is dropped.

## Schedule (PT, scheduled tasks; same prompt)
- **Sat** hourly at :45, 6:45 AM-7:45 PM · **Thu/Fri** hourly at :45, 11:45 AM-6:45 PM · **Sun/Mon** 6:45 AM and 12:45 PM.
- Scout: picks kicking off within 16 hours without a verdict, so normally all of them on the day's first run and any newly published ones later.
- Injury check: the first run of the day is the **morning** sweep (every college pick in the next 16 hours); later runs are **pre-kick** (only picks kicking off 45-105 minutes out, when availability reports and game-day news land).
- Most runs find nothing to do and stop immediately.

**On college slates, bet a pick only once its card shows the pre-kick Injury check line (about an hour before kickoff).**

## Procedure
1. `cd` into the `betting-engine` repo (clone `unclebuck20/betting-engine` if it isn't there), then `git pull`.
2. `python scripts/review.py list` prints `scout` (picks needing a verdict) and `injury` (`mode` and college games to check). If both lists are empty, stop right away: no research, no commit.
3. **Scout** each pick in `scout`, using the scouting guide below. Add one entry per pick to `data/manual/scout.json` under `picks`, keyed by its `pick_key`. Never change an existing verdict.
4. **Injury check** each game in `injury.games`, using the injury rules below. Write `data/manual/cfb_injury_check.json`: keep entries for other games, replace entries for games checked now, set top-level `checked_at`, and in morning mode also `morning_at`.
5. `python scripts/review.py validate`, and fix anything it reports.
6. Commit and push only those two files:
   ```
   git add data/manual/
   git commit -m "slate review: sat 12:45pm, scout 7 (6 agree, 1 caution), injury morning 8 games, 0 holds"
   for i in 1 2 3; do git pull --rebase -X theirs origin main && git push && break; sleep 10; done
   ```
   Then `python scripts/review.py dispatch` refreshes the page within about 5 minutes, with no odds credits spent. If that fails, the half-hourly run picks it up.
7. Report briefly: verdicts with reasons, holds with sources, and players listed as questionable worth watching.

## Scouting guide
You are a sharp's scout, not a handicapper: the price, model and market already account for team quality. Look only for **concrete, current facts** the numbers can't see:
- **Personnel news:** QB changes or a benching, suspensions, players opting out, a coordinator fired or a play-caller change, and key injuries on either side (for NFL, check the latest reports and practice participation).
- **Conditions:** severe weather (sustained wind over 15 mph, heavy rain or snow) for the game's venue and kickoff time.
- **Matchup reality:** a specific unit-vs-unit mismatch from current-season tape or stats that the card's reasons contradict (e.g. the card leans on a pass offense facing a top pass defense that just got healthy).
- **Market context:** why the line moved (sharp action, injury, weather) if the move went against our side.

**Verdicts:**
- **agree:** the default. Nothing concrete found, or what you found supports the pick. Reason: the single most relevant fact.
- **caution** (half size): a specific, sourced concern that plausibly costs about 1-2 points against our side and the line hasn't reflected it.
- **veto** (no bet): a specific, sourced fact that breaks the pick's premise (the starter it was built on is out, extreme weather on a total, a mass suspension) and that the line hasn't absorbed.
- Not allowed as a reason for caution or veto: "trap game", gut feel, records, narratives, revenge or motivation spots, or factors already tested and rejected (NFL rest, bye, travel, time zone and division spots; see ROADMAP.md).
- Every caution and veto needs at least one source URL. Reason: one plain sentence of 160 characters or fewer.

```json
{"picks": {"<pick_key from list>": {
  "matchup": "...", "side": "...", "league": "cfb", "kickoff_utc": "...", "scouted_at": "<now UTC>",
  "line": 14.0, "price": -110, "verdict": "caution",
  "reason": "Arkansas LT and RG ruled out Friday; A&M's pass rush leads the SEC in pressure rate; line hasn't moved.",
  "sources": ["https://..."]}}}
```

## Injury rules (college)
Research both teams: the starting QB first, then the two or three most important players. Use the conference's official availability report if one is published, plus team and beat-reporter news, coach press conferences, ESPN, On3, 247Sports and The Athletic. Use only this week's news, and note when each item broke.

Put a team in `hold` only when **all three** are true:
- **Who:** the player is the starting QB, or clearly one of the team's two or three most important players.
- **Status:** the player is listed or reported **out** or **doubtful**. Questionable and game-time decisions go in the summary only.
- **Not priced:** the line hasn't already moved for it. Compare the line history and the opener against when the news broke. As a rough guide, a starting QB is worth 3-7 points and another star 0.5-1.5.

List the team missing the player. The engine only holds the pick when that team is our side.

Summary: one plain sentence of 160 characters or fewer. Team names must be copied exactly from `matchup`.
```json
{"checked_at": "...", "morning_at": "...", "games": {"<id>": {
  "matchup": "Ohio State Buckeyes @ Iowa Hawkeyes", "kickoff_utc": "...", "checked_at": "...",
  "absences": [{"team": "Iowa Hawkeyes", "player": "Name", "pos": "QB", "status": "out",
                "key": true, "priced": false, "broke": "2026-10-02", "source": "https://..."}],
  "hold": ["Iowa Hawkeyes"], "summary": "Iowa QB (name) out, line unmoved since Tuesday; Ohio State healthy."}}}
```

## Rules
- When in doubt: agree, and don't hold. Mention the concern in the reason or summary instead.
- Never edit any file except the two in `data/manual/`. Never change picks, params or code.
- If web search is unavailable, write nothing and say so in the report.
