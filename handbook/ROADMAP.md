# Roadmap

## Live now (as of Oct 3, 2026)
College spreads (power ratings), NFL spreads and totals (unit + QB ratings), market fair lines with key-number pricing, steam / stale-price / injury-news signals, pre-slate college injury check, slate caps, published-pick tracking, bet logging, grading on closing-line value, weekly calibration, half-hourly scheduler, Ballard Syndicate dashboard.

## Next
1. **First calibration review** once ~50 picks are graded: CLV by league, play vs lean, model-driven vs price-driven; decide whether to keep half-size betting.
2. **NFL vs-opener test.** After 6-8 weeks of our own opening-line snapshots, re-test the NFL model against openers (the line actually bet) instead of only closing lines.
3. **Conference availability reports** as a scraped college injury feed, if the injury check misses things.
4. **More sportsbooks** if other legal Washington books become available to Garrett: free edge on every pick.

## Parked (not now)
- Line-movement model trained on purchased historical openers ($30 one-month Odds API plan): Garrett declined.
- Teasers, player props.
- Moneylines on small spreads.

## Tested and rejected (evidence in commit history)
- NFL efficiency ratings alone (old model): 47-48% ATS vs the close, 2021-25.
- NFL matchup interactions (pressure, explosives, blitz, play-action) and situational spots (rest, bye, travel, time zone, west-to-east early kickoffs, division): no out-of-sample improvement.
- College preseason prior (talent + returning production): more accurate, worse ATS vs the opener (56.9% → 53.9% in weeks 4-6).
- The pass/run mix interaction is kept but unproven: better ATS at 4+ pt gaps, slightly worse margin accuracy.
