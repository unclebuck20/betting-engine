# Operations

## Day to day
- **Site:** unclebuck20.github.io/betting-engine (add to home screen). Tap **$** once and enter 15 to see dollars.
- **Betting a card:** confirm the price is at or better than the "Good to" floor → place it → tap **Took it** → change line/price/units on the GitHub form if you got a different number → **Submit**. The card shows Pending, then ✓ Logged within ~1 minute.
- **Status chips:** *Still good* · *Edge gone at today's price* · *Line moved past the number* (don't chase) · *Cut to keep the slate under its cap*.
- **College injuries:** no feed. Before a college slate, check each pick's starting QB and key starters; pass if one is newly out and the line hasn't moved.

## Schedule (`refresh.yml` + `schedule.py`)
The workflow wakes at :11 and :41 every hour (GitHub delays jobs scheduled at :00 by hours) and decides:
- **Odds pull:** once each morning (~7 AM PT) for both leagues; then 20-120 minutes before kickoffs, only for the league kicking off, if that league's last pull is 50+ minutes old.
- **Full refresh without odds:** every 3 hours, or sooner when a finished game is waiting to be graded.
- **Nothing due:** the run exits in seconds.
A push to `scripts/` or a manual run (Actions → refresh → Run workflow) forces a full pull.

**GitHub's own scheduler is unreliable** (on Oct 1-3 it skipped or delayed most scheduled runs by hours), so an outside service pokes the workflow every 30 minutes:
- cron-job.org job, every 30 min: `POST https://api.github.com/repos/unclebuck20/betting-engine/actions/workflows/refresh.yml/dispatches`
- Headers: `Accept: application/vnd.github+json`, `Authorization: Bearer <fine-grained token>`, `X-GitHub-Api-Version: 2022-11-28`
- Body: `{"ref":"main","inputs":{"force":"false"}}`
- Token: GitHub → Settings → Developer settings → Fine-grained tokens; only the `betting-engine` repo; permission **Actions: Read and write**. Renew before it expires.
- The scheduler still decides what's due, so extra pokes cost nothing.

## Secrets and settings
- Repo secrets: `ODD_API_KEY` (The Odds API), `CFBD_API_KEY`.
- Pages: Settings → Pages → Deploy from branch → `main` / `/docs`.
- The Claude GitHub App is installed on `betting-engine` so Claude Code sessions can push.

## When something looks wrong
| Symptom | Check | Fix |
|---|---|---|
| "Odds X hr ago · check prices" in red | Actions tab: is refresh running? | Run the workflow manually; if failing, open the run's annotations |
| A step failed but the run is green | Steps after Fetch use continue-on-error; check annotations for `::error::` | Fix in a Claude Code session; raw data is still committed |
| College game with no model rating | Annotation "college names not matched" | Add the name to CFBD alternate names handling in `common.py` |
| Bet didn't log | Issue still open on GitHub | Title must start with `BET:` and be opened by the repo owner; re-submit or close and re-tap |
| Credits running low | Fetch notice "credits left" | Lower pre-kickoff pulls or drop NFL totals temporarily |
| Runs stopped happening | Actions tab: no runs for hours | Check the cron-job.org job's history and that the token hasn't expired |
| A source is down (e.g. CFBD 525) | `::error::` for that source; others still written | Nothing to do; last good data for that source stays live |
| Kickoff times off by an hour | Should not happen (zoneinfo) | Cron runs shift an hour in PT after Nov 1; the scheduler doesn't care |

## Making changes
Open a Claude Code session on `unclebuck20/betting-engine`. Rules that keep the engine honest:
1. No model change ships without a walk-forward backtest (or live CLV evidence) showing it helps out of sample.
2. Tune on early seasons, confirm on later ones; report both.
3. Don't commit locally built data files; let the Action build what ships.
4. Run `tests/smoke_test.py` after page changes.
5. Update this handbook when behavior changes.
