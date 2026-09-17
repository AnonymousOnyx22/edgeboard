"""Sport registry: everything that differs between NFL, MLB and NHL.

The picker itself is sport-agnostic. All the league-specific knowledge lives
here - ESPN's path segments, The Odds API's sport and market keys, which stats
a position is bet on, what a line moves in, and how the game model is scaled.

Every stat key below came out of a real ESPN gamelog response, verified
2026-09-17:

  NFL  passingYards, rushingYards, receivingYards, receptions, ...
  MLB  batting:  atBats, runs, hits, doubles, triples, homeRuns, RBIs, walks,
                 strikeouts, stolenBases
       pitching: innings, hits, runs, earnedRuns, walks, strikeouts, pitches,
                 battersFaced
  NHL  goals, assists, points, plusMinus, shotsTotal, powerPlayGoals, ...

MLB athletes carry two stat categories (batting and pitching) and ESPN serves
whichever matches the player's position, so a pitcher's log has no `hits`
meaning "hits recorded" - it means "hits allowed".
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SportConfig:
    code: str                      # our internal code, matches the sports table
    name: str
    espn_sport: str                # ESPN path segment, e.g. "football"
    espn_league: str               # ESPN path segment, e.g. "nfl"
    odds_key: str                  # The Odds API sport key
    emoji: str

    stat_labels: dict[str, str]
    stats_by_position: dict[str, list[str]]
    participation_stats: dict[str, list[str]]
    prop_markets: dict[str, str]           # our stat key -> Odds API market key
    core_markets: list[str]                # the cheap default pull
    line_step: dict[str, float]
    min_sd: dict[str, float]

    # Game model scaling.
    total_base: float              # league-average total for the game model
    total_sd: float
    elo_home_advantage: float
    points_per_100_elo: float
    margin_sd: float

    history_source: str = "espn"   # "nflverse" for NFL, "espn" otherwise
    season_spans_years: bool = False   # NHL labels seasons "2025-26"
    supports_props: bool = True

    @property
    def market_to_stat(self) -> dict[str, str]:
        return {v: k for k, v in self.prop_markets.items()}

    def stats_for(self, position: str) -> list[str]:
        return self.stats_by_position.get((position or "").upper(), [])

    def positions(self) -> set[str]:
        return set(self.stats_by_position)

    @property
    def all_markets(self) -> list[str]:
        """Every market this sport can model, in a stable order."""
        return sorted(set(self.prop_markets.values()))

    def markets_for(self, depth: str = "core") -> list[str]:
        """Markets to request. Each one costs a credit per game, so `depth`
        is the dial between a cheap look and full coverage."""
        if depth == "all":
            return self.all_markets
        return [m for m in self.core_markets if m in set(self.prop_markets.values())]


# --------------------------------------------------------------------- NFL
NFL = SportConfig(
    code="nfl", name="NFL", espn_sport="football", espn_league="nfl",
    odds_key="americanfootball_nfl", emoji="🏈",
    stat_labels={
        "passingYards": "Pass Yds", "passingTouchdowns": "Pass TD",
        "completions": "Completions", "passingAttempts": "Pass Att",
        "interceptions": "INT", "longPassing": "Longest Pass",
        "rushingYards": "Rush Yds", "rushingAttempts": "Carries",
        "rushingTouchdowns": "Rush TD", "longRushing": "Longest Rush",
        "receivingYards": "Rec Yds", "receptions": "Receptions",
        "receivingTargets": "Targets", "receivingTouchdowns": "Rec TD",
        "longReception": "Longest Rec",
        # Derived in espn_stats.DERIVED_STATS.
        "rushRecYards": "Rush + Rec Yds",
        "passRushYards": "Pass + Rush Yds",
        "allPurposeYards": "Pass + Rush + Rec Yds",
        "anytimeTouchdown": "Anytime TD",
    },
    stats_by_position={
        "QB": ["passingYards", "passingTouchdowns", "completions", "passingAttempts",
               "interceptions", "longPassing", "rushingYards", "rushingAttempts",
               "passRushYards", "anytimeTouchdown"],
        "RB": ["rushingYards", "rushingAttempts", "receivingYards", "receptions",
               "receivingTargets", "rushingTouchdowns", "longRushing",
               "rushRecYards", "anytimeTouchdown"],
        "WR": ["receivingYards", "receptions", "receivingTargets",
               "receivingTouchdowns", "longReception", "rushingYards",
               "rushRecYards", "anytimeTouchdown"],
        "TE": ["receivingYards", "receptions", "receivingTargets",
               "receivingTouchdowns", "longReception", "anytimeTouchdown"],
        "FB": ["rushingYards", "receptions", "rushRecYards", "anytimeTouchdown"],
    },
    participation_stats={
        "QB": ["passingAttempts", "rushingAttempts"],
        "RB": ["rushingAttempts", "receivingTargets"],
        "WR": ["receivingTargets", "rushingAttempts"],
        "TE": ["receivingTargets"],
        "FB": ["rushingAttempts", "receivingTargets"],
    },
    prop_markets={
        "passingYards": "player_pass_yds", "passingTouchdowns": "player_pass_tds",
        "completions": "player_pass_completions",
        "passingAttempts": "player_pass_attempts",
        "interceptions": "player_pass_interceptions",
        "longPassing": "player_pass_longest_completion",
        "rushingYards": "player_rush_yds", "rushingAttempts": "player_rush_attempts",
        "longRushing": "player_rush_longest",
        "receivingYards": "player_reception_yds", "receptions": "player_receptions",
        "longReception": "player_reception_longest",
        "rushRecYards": "player_rush_reception_yds",
        "passRushYards": "player_pass_rush_yds",
        "allPurposeYards": "player_pass_rush_reception_yds",
        "anytimeTouchdown": "player_anytime_td",
    },
    core_markets=["player_pass_yds", "player_rush_yds", "player_reception_yds",
                  "player_receptions", "player_pass_tds"],
    line_step={"passingYards": 5.0, "rushingYards": 5.0, "receivingYards": 5.0,
               "rushRecYards": 5.0, "passRushYards": 5.0, "allPurposeYards": 5.0,
               "longPassing": 1.0, "longRushing": 1.0, "longReception": 1.0,
               "completions": 1.0, "passingAttempts": 1.0, "receptions": 1.0,
               "receivingTargets": 1.0, "rushingAttempts": 1.0,
               "passingTouchdowns": 1.0, "rushingTouchdowns": 1.0,
               "receivingTouchdowns": 1.0, "interceptions": 1.0,
               "anytimeTouchdown": 1.0},
    min_sd={"passingYards": 45.0, "rushingYards": 22.0, "receivingYards": 22.0,
            "rushRecYards": 26.0, "passRushYards": 48.0, "allPurposeYards": 50.0,
            "longPassing": 9.0, "longRushing": 6.0, "longReception": 7.0,
            "completions": 4.0, "passingAttempts": 5.0, "receptions": 1.4,
            "receivingTargets": 1.8, "rushingAttempts": 3.0,
            "passingTouchdowns": 0.8, "rushingTouchdowns": 0.5,
            "receivingTouchdowns": 0.5, "interceptions": 0.7,
            "anytimeTouchdown": 0.5},
    total_base=43.0, total_sd=12.0,
    elo_home_advantage=60.0, points_per_100_elo=2.75, margin_sd=13.5,
    history_source="nflverse",
)

# --------------------------------------------------------------------- MLB
MLB = SportConfig(
    code="mlb", name="MLB", espn_sport="baseball", espn_league="mlb",
    odds_key="baseball_mlb", emoji="⚾",
    stat_labels={
        "hits": "Hits", "homeRuns": "Home Runs", "RBIs": "RBIs", "runs": "Runs",
        "atBats": "At Bats", "walks": "Walks", "strikeouts": "Strikeouts",
        "stolenBases": "Stolen Bases", "doubles": "Doubles", "triples": "Triples",
        "innings": "Innings", "earnedRuns": "Earned Runs",
        "battersFaced": "Batters Faced", "pitches": "Pitches",
        # Derived in espn_stats.DERIVED_STATS.
        "totalBases": "Total Bases", "singles": "Singles",
        "hitsRunsRbis": "Hits + Runs + RBIs", "outsRecorded": "Outs Recorded",
    },
    stats_by_position={
        pos: ["hits", "totalBases", "runs", "RBIs", "homeRuns", "strikeouts",
         "walks", "stolenBases", "doubles", "triples", "singles",
         "hitsRunsRbis"]
        for pos in ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "DH", "IF", "UT")
    } | {
        "SP": ["strikeouts", "innings", "hits", "earnedRuns", "walks",
          "outsRecorded"],
        "RP": ["strikeouts", "innings", "hits", "earnedRuns", "walks",
          "outsRecorded"],
        "P": ["strikeouts", "innings", "hits", "earnedRuns", "walks",
          "outsRecorded"],
    },
    participation_stats={
        pos: ["atBats"] for pos in
        ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "DH", "IF", "UT")
    } | {"SP": ["battersFaced"], "RP": ["battersFaced"], "P": ["battersFaced"]},
    prop_markets={
        "hits": "batter_hits", "homeRuns": "batter_home_runs",
        "totalBases": "batter_total_bases", "RBIs": "batter_rbis",
        "runs": "batter_runs_scored", "walks": "batter_walks",
        "stolenBases": "batter_stolen_bases", "doubles": "batter_doubles",
        "triples": "batter_triples", "singles": "batter_singles",
        "hitsRunsRbis": "batter_hits_runs_rbis",
        # Pitching markets describe the pitcher's own line.
        "strikeouts": "pitcher_strikeouts", "earnedRuns": "pitcher_earned_runs",
        "outsRecorded": "pitcher_outs",
    },
    core_markets=["batter_hits", "batter_total_bases", "batter_home_runs",
                  "pitcher_strikeouts"],
    line_step={"hits": 1.0, "homeRuns": 1.0, "totalBases": 1.0, "RBIs": 1.0,
               "runs": 1.0, "walks": 1.0, "strikeouts": 1.0, "stolenBases": 1.0,
               "innings": 1.0, "earnedRuns": 1.0, "doubles": 1.0, "triples": 1.0,
               "singles": 1.0, "hitsRunsRbis": 1.0, "outsRecorded": 1.0},
    min_sd={"hits": 0.9, "homeRuns": 0.5, "totalBases": 1.3, "RBIs": 0.9,
            "runs": 0.7, "walks": 0.6, "strikeouts": 1.6, "stolenBases": 0.4,
            "innings": 1.2, "earnedRuns": 1.4, "doubles": 0.6, "triples": 0.3,
            "singles": 0.8, "hitsRunsRbis": 1.4, "outsRecorded": 3.5},
    total_base=8.5, total_sd=4.2,
    elo_home_advantage=24.0, points_per_100_elo=0.55, margin_sd=4.4,
)

# --------------------------------------------------------------------- NHL
NHL = SportConfig(
    code="nhl", name="NHL", espn_sport="hockey", espn_league="nhl",
    odds_key="icehockey_nhl", emoji="🏒",
    stat_labels={
        "goals": "Goals", "assists": "Assists", "points": "Points",
        "shotsTotal": "Shots on Goal", "plusMinus": "+/-",
        "penaltyMinutes": "PIM", "powerPlayGoals": "PP Goals",
        "powerPlayAssists": "PP Assists", "shortHandedGoals": "SH Goals",
        "saves": "Saves", "goalsAgainst": "Goals Against",
        # Derived in espn_stats.DERIVED_STATS.
        "powerPlayPoints": "PP Points",
    },
    stats_by_position={
        pos: ["points", "shotsTotal", "goals", "assists",
         "powerPlayPoints", "penaltyMinutes"]
        for pos in ("C", "LW", "RW", "W", "F", "D")
    } | {"G": ["saves", "goalsAgainst"]},
    participation_stats={
        pos: ["shotsTotal", "points", "penaltyMinutes"]
        for pos in ("C", "LW", "RW", "W", "F", "D")
    } | {"G": ["saves"]},
    prop_markets={
        "points": "player_points", "goals": "player_goals",
        "assists": "player_assists", "shotsTotal": "player_shots_on_goal",
        "powerPlayPoints": "player_power_play_points",
        "saves": "player_total_saves",
    },
    core_markets=["player_points", "player_shots_on_goal", "player_goals",
                  "player_assists"],
    line_step={"points": 1.0, "goals": 1.0, "assists": 1.0, "shotsTotal": 1.0,
               "saves": 1.0, "goalsAgainst": 1.0, "powerPlayPoints": 1.0,
               "penaltyMinutes": 1.0},
    min_sd={"points": 0.8, "goals": 0.5, "assists": 0.7, "shotsTotal": 1.4,
            "saves": 5.5, "goalsAgainst": 1.3, "powerPlayPoints": 0.6,
            "penaltyMinutes": 1.6},
    total_base=6.1, total_sd=2.5,
    elo_home_advantage=35.0, points_per_100_elo=0.45, margin_sd=2.4,
    season_spans_years=True,
)

REGISTRY: dict[str, SportConfig] = {s.code: s for s in (NFL, MLB, NHL)}
DEFAULT_SPORT = "nfl"


def get(code: str | None) -> SportConfig:
    """Look up a sport, falling back to the default rather than raising."""
    return REGISTRY.get((code or DEFAULT_SPORT).lower(), NFL)


def enabled_codes(config: dict | None = None) -> list[str]:
    """Sport codes switched on in settings.yaml, in priority order."""
    if not config:
        from ..db.database import get_config
        config = get_config()
    entries = (config or {}).get("sports", {}) or {}
    active = [(code, meta or {}) for code, meta in entries.items()
              if (meta or {}).get("enabled") and code in REGISTRY]
    active.sort(key=lambda item: item[1].get("priority", 99))
    return [code for code, _ in active] or [DEFAULT_SPORT]


# ----------------------------------------------------------------- imagery
# ESPN serves club logos and player headshots from a predictable CDN path,
# verified 2026-09-17 for all three leagues (including the awkward NFL
# abbreviations WSH and LAR, which resolve correctly).
LOGO_CDN = "https://a.espncdn.com/i/teamlogos/{league}/500/{abbrev}.png"
HEADSHOT_CDN = "https://a.espncdn.com/i/headshots/{league}/players/full/{athlete_id}.png"


def team_logo(abbrev: str, sport: str = "nfl") -> str:
    """Club badge for a team abbreviation, in the sport's own spelling."""
    config = get(sport)
    return LOGO_CDN.format(league=config.espn_league,
                           abbrev=(abbrev or "").strip().lower())


def player_headshot(athlete_id: str | int, sport: str = "nfl") -> str:
    """Player headshot. ESPN 404s for players without one, so the markup
    that uses this needs a fallback for a broken image."""
    config = get(sport)
    return HEADSHOT_CDN.format(league=config.espn_league, athlete_id=athlete_id)
