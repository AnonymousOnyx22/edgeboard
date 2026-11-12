"""Historical backing for a specific bet.

The board says "DET +5.5 has a 4.7% edge". This module answers the question a
human actually asks next: *has that bet ever cashed against this opponent?*

It takes the number on offer today and replays it against every stored prior
meeting between the two clubs:

  moneyline   did this club win that game?
  spread      applying today's line to that game's margin, would it have covered?
  total       did that game's combined score land the right side of today's number?

That is a backtest of one number over a handful of games, which is evidence,
not proof - the sample is small by nature, and `Backing.strength` says so
plainly rather than dressing four games up as a trend.

Nothing here invents a result. A meeting with no final score is skipped, and a
sport whose history carries no closing lines still supports moneyline and total
questions, because those only need the score.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..data import history_store
from .h2h import MEETING_WINDOW


@dataclass
class Meeting:
    """One prior game, judged against the bet being considered."""
    date: str
    season: int | None
    home: str
    away: str
    home_score: float
    away_score: float
    hit: bool | None          # did the bet cash in this game? None = unknowable
    detail: str               # "BUF 48-42" plus whatever the bet turned on

    @property
    def total(self) -> float:
        return self.home_score + self.away_score


@dataclass
class Backing:
    """How a bet has fared in prior meetings."""
    market: str                       # h2h / spread / total
    side: str
    line: float | None
    meetings: list[Meeting] = field(default_factory=list)

    @property
    def graded(self) -> list[Meeting]:
        return [m for m in self.meetings if m.hit is not None]

    @property
    def hits(self) -> int:
        return sum(1 for m in self.graded if m.hit)

    @property
    def total(self) -> int:
        return len(self.graded)

    @property
    def rate(self) -> float | None:
        return self.hits / self.total if self.total else None

    @property
    def strength(self) -> str:
        """An honest label for how much this sample is worth."""
        if self.total == 0:
            return "none"
        if self.total < 3:
            return "anecdote"
        if self.total < 6:
            return "thin"
        return "solid"

    @property
    def note(self) -> str:
        """One sentence a human can check the pick against."""
        if not self.total:
            return "No prior meetings with a gradable result."
        label = _describe(self.market, self.side, self.line)
        sentence = f"{label} cashed in {self.hits} of the last {self.total} meetings"
        if self.strength == "anecdote":
            sentence += " (only a couple of games - treat as anecdote)"
        elif self.strength == "thin":
            sentence += " (small sample)"
        return sentence


def _describe(market: str, side: str, line: float | None) -> str:
    if market == "total":
        return f"{side} {line:g}"
    if market == "spread" and line is not None:
        return f"{side} {line:+g}"
    return f"{side} moneyline"


# ------------------------------------------------------------------- lookups
def backing_for(team_a: str, team_b: str, market: str, side: str,
                 line: float | None, sport: str = "nfl", limit: int = MEETING_WINDOW,
                 db_path=None, as_of: dt.date | None = None) -> Backing:
    """Replay one bet across every stored meeting between two clubs."""
    kwargs = {"db_path": db_path} if db_path is not None else {}
    rows = history_store.meetings(team_a, team_b, sport, limit,
                                  before=as_of, **kwargs)
    result = Backing(market=market, side=side, line=line)

    target = history_store.normalise(side)
    for row in rows:
        home, away = row["home_team"], row["away_team"]
        home_score, away_score = row["home_score"], row["away_score"]
        if home_score is None or away_score is None:
            continue

        hit, detail = _judge(market, target, line, home, away, home_score, away_score)
        result.meetings.append(Meeting(
            date=row["game_date"], season=row["season"], home=home, away=away,
            home_score=home_score, away_score=away_score, hit=hit, detail=detail))
    return result


def _judge(market: str, target: str, line: float | None, home: str, away: str,
           home_score: float, away_score: float) -> tuple[bool | None, str]:
    """Would this bet have cashed in that game?"""
    margin_home = home_score - away_score
    score = f"{away} {away_score:g}-{home_score:g} {home}"

    if market == "total":
        if line is None:
            return None, score
        combined = home_score + away_score
        hit = combined > line if target.lower() == "over" else combined < line
        return hit, f"{score} · {combined:g} pts"

    if target not in (home, away):
        return None, score   # the bet is on a club that did not play in this game

    margin = margin_home if target == home else -margin_home

    if market == "h2h":
        if margin == 0:
            return None, f"{score} · tie"
        return margin > 0, f"{score} · {'won' if margin > 0 else 'lost'} by {abs(margin):g}"

    if market == "spread":
        if line is None:
            return None, score
        # The bet covers when the team's margin plus its handicap clears zero.
        adjusted = margin + line
        if adjusted == 0:
            return None, f"{score} · push"
        return adjusted > 0, f"{score} · {margin:+g} vs {line:+g}"

    return None, score


def attach(card, sport: str = "nfl", limit: int = MEETING_WINDOW, db_path=None,
           as_of: dt.date | None = None) -> None:
    """Give every candidate on a game card its historical backing."""
    for candidate in card.candidates:
        candidate.backing = backing_for(
            card.home, card.away, candidate.market, candidate.side,
            candidate.line, sport, limit, db_path, as_of)
