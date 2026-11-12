"""Parlay construction and search.

`build_parlay` prices one specific combination. `optimize` searches the whole
candidate pool for the combinations worth playing, under correlation rules:

  - no two legs from the same game (unless same-game parlays are enabled)
  - never two legs on the same player
  - never two legs on the same player-and-stat

Combined probability multiplies the legs, which assumes they are independent.
That holds up across different games; inside one game it does not, and
same-game parlays are penalised accordingly rather than being sold at face
value.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .edge import american_from_prob, american_to_prob, decimal_from_american

# A same-game parlay's legs move together, so the naive product overstates it.
SAME_GAME_PENALTY = 0.85


class ParlayError(ValueError):
    pass


@dataclass
class Leg:
    game_id: int | str
    market: str            # h2h / spread / total / prop
    team: str              # team label, Over/Under, or player name
    line: float | None
    odds_american: int | float
    model_prob: float
    reason: str = ""
    support_stat: str = ""
    # Set for prop legs so the optimizer can keep one player out of a parlay twice.
    athlete_id: str = ""
    stat_key: str = ""
    label: str = ""
    priced: bool = True    # False = odds_american is an assumption, not a quote

    def display(self) -> str:
        if self.label:
            return self.label
        line = f" {self.line:+g}" if self.line is not None and self.market != "prop" else ""
        return f"{self.team} {self.market}{line}".strip()


@dataclass
class Parlay:
    legs: list[Leg]
    combined_odds: float
    combined_model_prob: float
    implied_prob: float
    payout_per_unit: float
    correlated: bool = False
    notes: list[str] = field(default_factory=list)

    def american_display(self) -> str:
        if self.combined_odds >= 2.0:
            return f"+{round((self.combined_odds - 1) * 100)}"
        return f"{american_from_prob(1.0 / self.combined_odds):.0f}"

    @property
    def edge(self) -> float:
        """Model probability minus the price's implied probability."""
        return self.combined_model_prob - self.implied_prob

    @property
    def expected_value(self) -> float:
        """Profit per 1 unit staked, at the model's probability."""
        return self.combined_model_prob * self.combined_odds - 1.0

    @property
    def fully_priced(self) -> bool:
        return all(leg.priced for leg in self.legs)


def build_parlay(legs: list[Leg], min_legs: int = 2, max_legs: int = 4,
                 allow_same_game: bool = False) -> Parlay:
    if not (min_legs <= len(legs) <= max_legs):
        raise ParlayError(f"parlay needs {min_legs}-{max_legs} legs, got {len(legs)}")

    game_ids = [str(leg.game_id) for leg in legs]
    correlated = len(set(game_ids)) != len(game_ids)
    if correlated and not allow_same_game:
        raise ParlayError("correlated same-game legs are not allowed in a parlay")

    athletes = [leg.athlete_id for leg in legs if leg.athlete_id]
    if len(set(athletes)) != len(athletes):
        raise ParlayError("the same player cannot appear on two legs")

    dec = 1.0
    model_prob = 1.0
    implied = 1.0
    for leg in legs:
        dec *= decimal_from_american(leg.odds_american)
        model_prob *= max(0.0, min(1.0, leg.model_prob))
        implied *= american_to_prob(leg.odds_american)

    notes: list[str] = []
    if correlated:
        model_prob *= SAME_GAME_PENALTY
        notes.append("Same-game legs: probability discounted for correlation.")
    if any(not leg.priced for leg in legs):
        notes.append("Some legs use an assumed price, not a real quote.")

    return Parlay(
        legs=legs,
        combined_odds=round(dec, 4),
        combined_model_prob=round(model_prob, 5),
        implied_prob=round(implied, 5),
        payout_per_unit=round(dec - 1.0, 4),
        correlated=correlated,
        notes=notes,
    )


# ------------------------------------------------------------------ search
def _compatible(combo: tuple[Leg, ...], allow_same_game: bool) -> bool:
    games = [str(leg.game_id) for leg in combo]
    if not allow_same_game and len(set(games)) != len(games):
        return False
    athletes = [leg.athlete_id for leg in combo if leg.athlete_id]
    if len(set(athletes)) != len(athletes):
        return False
    keyed = [(leg.athlete_id, leg.stat_key) for leg in combo if leg.stat_key]
    return len(set(keyed)) == len(keyed)


def optimize(candidates: list[Leg], min_legs: int = 2, max_legs: int = 4,
             allow_same_game: bool = False, max_pool: int = 18,
             limit: int = 10, rank_by: str = "probability",
             min_probability: float = 0.0) -> list[Parlay]:
    """Search the candidate pool for the best parlays.

    rank_by:
      "probability" - most likely to hit (what a safety-first slip wants)
      "value"       - highest expected value at the model's probabilities
    """
    if min_legs > max_legs:
        raise ParlayError("min_legs cannot exceed max_legs")

    # Trim to the strongest candidates first; the search is combinatorial.
    pool = sorted(candidates, key=lambda l: l.model_prob, reverse=True)[:max_pool]
    if len(pool) < min_legs:
        return []

    built: list[Parlay] = []
    for size in range(min_legs, min(max_legs, len(pool)) + 1):
        for combo in itertools.combinations(pool, size):
            if not _compatible(combo, allow_same_game):
                continue
            try:
                parlay = build_parlay(list(combo), min_legs, max_legs, allow_same_game)
            except ParlayError:
                continue
            if parlay.combined_model_prob < min_probability:
                continue
            built.append(parlay)

    key = ((lambda p: (p.expected_value, p.combined_model_prob))
           if rank_by == "value"
           else (lambda p: (p.combined_model_prob, p.expected_value)))
    built.sort(key=key, reverse=True)
    return built[:limit]


def prop_leg(candidate, odds_american: int = -110, priced: bool = False) -> Leg:
    """Turn a props.PropCandidate into a parlay leg.

    Props have no free market price, so `odds_american` is an assumption used
    only to show a payout. `priced=False` keeps that visible downstream.
    """
    return Leg(
        game_id=candidate.game_id if hasattr(candidate, "game_id") else f"prop:{candidate.athlete_id}",
        market="prop",
        team=candidate.player,
        line=candidate.line,
        odds_american=odds_american,
        model_prob=candidate.probability,
        reason=candidate.evidence(),
        support_stat=candidate.label,
        athlete_id=candidate.athlete_id,
        stat_key=candidate.stat_key,
        label=candidate.headline,
        priced=priced,
    )
