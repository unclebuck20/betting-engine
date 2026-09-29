"""Scope for the betting engine. The one place to change what's covered."""

SEASON = 2026

# The Odds API: free tier = 500 credits/month.
# Cost per call = markets x regions; up to 10 bookmakers counts as 1 region.
ODDS_SPORTS = {
    "nfl": "americanfootball_nfl",
    "cfb": "americanfootball_ncaaf",
}
ODDS_MARKETS = ["spreads"]  # 1 credit per sport per snapshot
ODDS_BOOKMAKERS = [
    "pinnacle",      # sharp anchor (public site, may lag slightly)
    "betonlineag",   # semi-sharp offshore
    "lowvig",        # low-vig offshore
    "draftkings",    # default card book until Garrett names his
    "fanduel",
    "betmgm",
    "bovada",
    "betrivers",
    "espnbet",
    "hardrockbet",
]
SHARP_BOOKS = ["pinnacle", "betonlineag", "lowvig"]
CARD_BOOK = "draftkings"
ODDS_MIN_CREDITS_LEFT = 40  # stop pulling odds below this to protect closing-line snapshots

# Units: logged in units only. Dollar value lives in the UNIT_DOLLARS repo variable, not here.

# Guards (fail closed)
MIN_NFL_GAMES_IN_SCHEDULE = 250      # full regular season is 272
MIN_ODDS_EVENTS = {"nfl": 1, "cfb": 5}  # in-season minimums for a real pull
MIN_NFL_INJURY_TEAMS = 20

SOURCES = {
    "nflverse_games": "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv",
    "espn_nfl_injuries": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries",
    "espn_cfb_injuries": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/injuries",
    "odds_base": "https://api.the-odds-api.com/v4",
    "cfbd_base": "https://api.collegefootballdata.com",
}
