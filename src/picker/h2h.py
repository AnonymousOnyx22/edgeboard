"""Head-to-head analysis: how these two teams actually played each other.

Two layers, both built from real completed games:

  team_series()    prior meetings from the nflverse schedule (scores, margins,
                   totals, whether the line was covered)
  player_series()  every skill player's own stat line in those meetings, pulled
                   from ESPN athlete gamelogs

Nothing here invents a number. A player with no history against the opponent
comes back with an empty sample and is reported as such, rather than being
filled in from season averages.
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd

from ..data import models, sports as sport_registry
from ..data.espn_stats import EspnStats

# nflverse and ESPN disagree on exactly two abbreviations.
NFLVERSE_TO_ESPN = {"LA": "LAR", "WAS": "WSH"}

# How many prior meetings any head-to-head view considers. The summary line and
# the per-bet backing panel must use the same window, or the same question gets
# two different answers on one card.
MEETING_WINDOW = 12
ESPN_TO_NFLVERSE = {v: k for k, v in NFLVERSE_TO_ESPN.items()}

# NFL stat tables now live in the sport registry; these shims keep the
# module-level names other code imports.
STAT_LABELS = sport_registry.NFL.stat_labels
STATS_BY_POSITION = sport_registry.NFL.stats_by_position
PARTICIPATION_STATS = sport_registry.NFL.participation_stats


def stat_label(stat_key: str, sport: str = "nfl") -> str:
    return sport_registry.get(sport).stat_labels.get(stat_key, stat_key)


def did_play(game: dict, position: str, sport: str = "nfl") -> bool:
    """Did this player actually take part in that game?

    A player can appear in a gamelog having never touched the ball - a rest
    game, an inactive, a reliever who did not pitch. Those rows are zeroes, and
    leaving them in drags every projection down and inflates its spread.
    """
    keys = sport_registry.get(sport).participation_stats.get((position or "").upper())
    if not keys:
        return True
    return any((game.get(k) or 0) > 0 for k in keys)


def to_espn(abbrev: str) -> str:
    a = (abbrev or "").upper()
    return NFLVERSE_TO_ESPN.get(a, a)


def to_nflverse(abbrev: str) -> str:
    a = (abbrev or "").upper()
    return ESPN_TO_NFLVERSE.get(a, a)


# ------------------------------------------------------------------- teams
@dataclass
class Meeting:
    date: str
    season: int
    week: int
    home: str
    away: str
    home_score: float
    away_score: float
    spread_line: float | None      # nflverse: home-relative, favorite positive
    total_line: float | None
    roof: str = ""

    @property
    def margin(self) -> float:
        """Home margin. Positive = home won."""
        return self.home_score - self.away_score

    @property
    def total(self) -> float:
        return self.home_score + self.away_score

    def winner(self) -> str:
        if self.margin > 0:
            return self.home
        return self.away if self.margin < 0 else "TIE"

    def home_covered(self) -> bool | None:
        if self.spread_line is None:
            return None
        return self.margin > self.spread_line

    def over_hit(self) -> bool | None:
        if self.total_line is None:
            return None
        return self.total > self.total_line

    def margin_for(self, team: str) -> float:
        """Margin from one team's point of view, in either abbreviation style."""
        return self.margin if _same_team(team, self.home) else -self.margin


@dataclass
class TeamH2H:
    team_a: str                       # ESPN abbrevs, as stored in the DB
    team_b: str
    meetings: list[Meeting] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.meetings)

    def record(self) -> tuple[int, int, int]:
        """(team_a wins, team_b wins, ties) across the sampled meetings."""
        wins_a = sum(1 for m in self.meetings if _same_team(self.team_a, m.winner()))
        ties = sum(1 for m in self.meetings if m.winner() == "TIE")
        return wins_a, self.n - wins_a - ties, ties

    def avg_margin_for(self, team: str) -> float | None:
        if not self.meetings:
            return None
        return sum(m.margin_for(team) for m in self.meetings) / self.n

    def avg_total(self) -> float | None:
        if not self.meetings:
            return None
        return sum(m.total for m in self.meetings) / self.n

    def totals_over_rate(self) -> tuple[int, int] | None:
        """(overs, games with a posted total)."""
        graded = [m for m in self.meetings if m.over_hit() is not None]
        if not graded:
            return None
        return sum(1 for m in graded if m.over_hit()), len(graded)

    def cover_rate_for(self, team: str) -> tuple[int, int] | None:
        """(covers, games with a posted spread) for one team."""
        graded = [m for m in self.meetings if m.home_covered() is not None]
        if not graded:
            return None
        covers = sum(1 for m in graded
                     if (m.home_covered() if _same_team(team, m.home)
                         else not m.home_covered()))
        return covers, len(graded)

    def summary_line(self) -> str:
        if not self.meetings:
            return "No prior meetings in the sampled range."
        w, l, t = self.record()
        record = f"{self.team_a} leads {w}-{l}" if w > l else (
            f"{self.team_b} leads {l}-{w}" if l > w else f"Split {w}-{l}")
        if t:
            record += f"-{t}"
        return (f"{record} over {self.n} meetings | "
                f"avg total {self.avg_total():.1f} | "
                f"avg margin {abs(self.avg_margin_for(self.team_a)):.1f}")


def team_series(team_a: str, team_b: str, history: pd.DataFrame | None = None,
                 seasons_back: int = 6, limit: int = MEETING_WINDOW,
                 sport: str = "nfl", as_of: dt.date | None = None) -> TeamH2H:
    """Completed meetings between two teams, newest first.

    `team_a`/`team_b` accept either abbreviation style. `history` defaults to
    that sport's cached schedule frame - nflverse for the NFL (which carries
    closing spreads and totals, so cover history comes with it), ESPN for the
    others (scores only).
    """
    if history is None:
        from ..data import history_store
        frame = history_store.load_history(sport)
    else:
        frame = history
    result = TeamH2H(team_a=to_espn(team_a), team_b=to_espn(team_b))
    if frame is None or frame.empty:
        return result

    a, b = _history_abbrev(team_a, sport), _history_abbrev(team_b, sport)
    reference = as_of or dt.date.today()
    cutoff = reference.year - seasons_back
    df = frame.dropna(subset=["home_score", "away_score"]).copy()
    if "gameday" in df.columns:
        game_dates = pd.to_datetime(df["gameday"], errors="coerce").dt.date
        df = df[game_dates < reference]
    if "season" in df.columns:
        df = df[df["season"] >= cutoff]
    pairing = (((df["home_team"] == a) & (df["away_team"] == b)) |
               ((df["home_team"] == b) & (df["away_team"] == a)))
    df = df[pairing].sort_values("gameday", ascending=False).head(limit)

    for _, row in df.iterrows():
        result.meetings.append(Meeting(
            date=str(row.get("gameday"))[:10],
            season=int(row.get("season", 0) or 0),
            week=int(row.get("week", 0) or 0),
            home=row["home_team"],
            away=row["away_team"],
            home_score=float(row["home_score"]),
            away_score=float(row["away_score"]),
            spread_line=_opt_float(row.get("spread_line")),
            total_line=_opt_float(row.get("total_line")),
            roof=str(row.get("roof") or ""),
        ))
    return result


# ------------------------------------------------------------------ players
@dataclass
class PlayerSample:
    """One player's stat history, split into games against this opponent
    and their recent form everywhere else."""
    athlete_id: str
    name: str
    position: str
    team: str
    opponent: str
    vs_games: list[dict] = field(default_factory=list)     # newest first
    recent_games: list[dict] = field(default_factory=list)  # newest first
    injury_status: str = ""
    sport: str = "nfl"

    def __post_init__(self) -> None:
        # Drop games the player sat out, however the sample was assembled.
        self.vs_games = [g for g in self.vs_games if did_play(g, self.position, self.sport)]
        self.recent_games = [g for g in self.recent_games
                             if did_play(g, self.position, self.sport)]

    def values(self, stat_key: str, vs_only: bool = False) -> list[float]:
        games = self.vs_games if vs_only else self.recent_games
        return [g[stat_key] for g in games
                if g.get(stat_key) is not None]

    def vs_average(self, stat_key: str) -> float | None:
        vals = self.values(stat_key, vs_only=True)
        return sum(vals) / len(vals) if vals else None

    def recent_average(self, stat_key: str) -> float | None:
        vals = self.values(stat_key)
        return sum(vals) / len(vals) if vals else None

    def stat_keys(self) -> list[str]:
        return sport_registry.get(self.sport).stats_for(self.position)


def player_series(team_espn_id: str | int, team_abbrev: str, opponent_abbrev: str,
                  seasons: list[int] | None = None, stats: EspnStats | None = None,
                  recent_games: int = 10, max_players: int = 14,
                  include_injured: bool = False, sport: str = "nfl",
                  progress=None) -> list[PlayerSample]:
    """Every skill player on a team, with their games against this opponent.

    Players are returned in roster order; those with no recorded games at all
    (practice-squad, rookies yet to play) are dropped.
    """
    client = stats or EspnStats(sport)
    sport = client.sport.code
    seasons = seasons or default_seasons()
    opponent = to_espn(opponent_abbrev)
    injuries = client.injured_ids(team_espn_id)

    # ESPN's roster is a list of athlete references. Resolving the entire
    # roster and then pulling three seasons serially made the player page look
    # frozen for minutes. Keep a modest reserve for injured/inactive players,
    # then load the independent histories concurrently.
    roster = client.roster(team_espn_id, limit=max_players * 2)
    eligible = []
    for player in roster:
        if not sport_registry.get(sport).stats_for(player["position"]):
            continue
        status = injuries.get(player["id"], "")
        if status and not include_injured and status.lower() in ("out", "injured reserve"):
            continue
        eligible.append((player, status))

    def load(item):
        player, status = item
        rows = [r for r in client.game_rows(player["id"], seasons)
                if did_play(r, player["position"], sport)]
        if not rows:
            return None
        return PlayerSample(
            athlete_id=player["id"],
            name=player["name"],
            position=player["position"],
            team=to_espn(team_abbrev),
            opponent=opponent,
            vs_games=[r for r in rows if (r.get("opponent") or "").upper() == opponent],
            recent_games=rows[:recent_games],
            injury_status=status,
            sport=sport,
        )

    if not eligible:
        return []
    ordered: list[PlayerSample | None] = [None] * len(eligible)
    workers = min(6, len(eligible))
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix=f"{sport}-player") as pool:
        pending = {pool.submit(load, item): index
                   for index, item in enumerate(eligible)}
        completed = 0
        for future in as_completed(pending):
            index = pending[future]
            try:
                ordered[index] = future.result()
            except Exception:
                # One unavailable athlete feed must not discard both rosters.
                ordered[index] = None
            completed += 1
            if progress:
                progress(completed / len(eligible),
                         f"Loaded {completed}/{len(eligible)} {team_abbrev} player histories...")
    return [sample for sample in ordered if sample is not None][:max_players]


def default_seasons(back: int = 3) -> list[int]:
    """Current season plus the previous ones, newest first."""
    year = dt.date.today().year
    return [year - i for i in range(back)]


def _history_abbrev(abbrev: str, sport: str) -> str:
    """Abbreviation as the history frame spells it."""
    return to_nflverse(abbrev) if sport == "nfl" else (abbrev or "").upper()


def _same_team(a: str, b: str) -> bool:
    """Compare abbreviations across the two spellings NFL sources use."""
    return to_espn(a) == to_espn(b)


def _opt_float(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
