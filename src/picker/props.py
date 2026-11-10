"""Player-prop projection model built on head-to-head and recent form.

The core output is a *hit probability*: given how this player has actually
performed against this opponent and lately, how often does a line of X come in?

Two ways to use it:

  rank_candidates()         model-only. Picks its own line and reports how
                            likely it is to hit. Needs no market data.
  rank_market_candidates()  scores the lines books are actually offering and
                            turns the projection into a real edge against the
                            vig-free price. Needs prop odds (see data/props_odds).

The model, per player and stat:

  1. Weight every sampled game.  weight = recency x opponent
       recency  = 0.5 ** (games_ago / half_life)     newer games count more
       opponent = OPPONENT_WEIGHT for games against this exact opponent
  2. Weighted mean and standard deviation -> a normal estimate of P(X >= line).
  3. Weighted empirical hit rate -> how often it actually cleared that line.
  4. Blend the two, trusting the empirical rate more as the sample grows, then
     shrink toward a coin flip when the sample is thin.

Step 4 is what stops "3 for 3 in a tiny sample" from being sold as a lock.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..data import sports as sport_registry
from .confidence import market_trust, max_raw_edge
from .h2h import PlayerSample

# Books price props sharply and know things this model cannot see - a snap
# share, a depth-chart change, a coach's plan. So the market price is used as a
# prior (`edge.market_trust`), and a disagreement wider than `edge.max_raw_edge`
# is flagged rather than sold: it almost always means missing information.

OPPONENT_WEIGHT = 2.5      # a game against this opponent counts 2.5x
RECENCY_HALF_LIFE = 6.0    # games until a game's weight halves
EMPIRICAL_K = 5.0          # weighted games before the empirical rate dominates
SHRINK_STRENGTH = 2.0      # pseudo-games pulling thin samples toward 50%
# Per-stat spread floors and line increments live in the sport registry.


def _min_sd(stat_key: str, sport: str) -> float:
    """A floor on the spread, so one flat sample is never sold as certain."""
    return sport_registry.get(sport).min_sd.get(stat_key, 1.0)


def _line_step(stat_key: str, sport: str) -> float:
    """The increment books price this stat in."""
    return sport_registry.get(sport).line_step.get(stat_key, 1.0)


@dataclass
class Projection:
    stat_key: str
    mean: float
    sd: float
    n_eff: float            # weighted sample size
    n_games: int            # raw games used
    n_vs: int               # of those, games against this opponent
    vs_average: float | None
    recent_average: float | None
    sport: str = "nfl"

    @property
    def label(self) -> str:
        return sport_registry.get(self.sport).stat_labels.get(self.stat_key, self.stat_key)


@dataclass
class PropCandidate:
    athlete_id: str
    player: str
    position: str
    team: str
    opponent: str
    stat_key: str
    line: float
    probability: float
    projection: Projection
    hit_vs_opponent: tuple[int, int]     # (cleared, games against this opponent)
    hit_recent: tuple[int, int]          # (cleared, recent games)
    injury_status: str = ""
    game_id: int | str = 0               # set by board.deep_matchup
    sport: str = "nfl"
    # Market fields, populated only when a real book price was found.
    side: str = "over"                   # over / under
    odds: int | None = None
    implied_prob: float | None = None
    edge: float | None = None
    bookmaker: str = ""
    raw_probability: float | None = None  # before the market prior was applied
    warnings: list[str] = field(default_factory=list)

    @property
    def priced(self) -> bool:
        return self.odds is not None

    @property
    def trustworthy(self) -> bool:
        """No warning that would make a sane person skip this."""
        return not self.warnings

    @property
    def label(self) -> str:
        return sport_registry.get(self.sport).stat_labels.get(self.stat_key, self.stat_key)

    @property
    def headline(self) -> str:
        if self.side == "under":
            return f"{self.player} under {self.line:g} {self.label}"
        return f"{self.player} {self.line:g}+ {self.label}"

    def evidence(self) -> str:
        """One plain line a human can sanity-check the pick against.

        Counts are always reported as "cleared the line", so an Under's
        record is shown from that side.
        """
        parts = []
        cleared, total = self.hit_vs_opponent
        if total:
            hits = total - cleared if self.side == "under" else cleared
            avg = self.projection.vs_average
            parts.append(f"{hits}/{total} vs {self.opponent}"
                         + (f" (avg {avg:.1f})" if avg is not None else ""))
        cleared_r, total_r = self.hit_recent
        if total_r:
            hits_r = total_r - cleared_r if self.side == "under" else cleared_r
            parts.append(f"{hits_r}/{total_r} in last {total_r}")
        parts.append(f"proj {self.projection.mean:.1f}")
        return " · ".join(parts)


# ----------------------------------------------------------------- weighting
def _weights(sample: PlayerSample, stat_key: str) -> list[tuple[float, float]]:
    """[(value, weight)] over the player's sampled games for one stat.

    Games against this opponent are included even when they fall outside the
    recent-form window, which is the whole point of the head-to-head view.
    """
    games = list(sample.recent_games)
    known = {g["event_id"] for g in games}
    games += [g for g in sample.vs_games if g["event_id"] not in known]
    games.sort(key=lambda g: (g.get("date") or ""), reverse=True)

    out: list[tuple[float, float]] = []
    for i, game in enumerate(games):
        value = game.get(stat_key)
        if value is None:
            continue
        weight = 0.5 ** (i / RECENCY_HALF_LIFE)
        if (game.get("opponent") or "").upper() == sample.opponent:
            weight *= OPPONENT_WEIGHT
        out.append((float(value), weight))
    return out


def project(sample: PlayerSample, stat_key: str) -> Projection | None:
    """Weighted mean/sd for one player-stat, or None with no usable games."""
    weighted = _weights(sample, stat_key)
    if not weighted:
        return None
    total_w = sum(w for _, w in weighted)
    mean = sum(v * w for v, w in weighted) / total_w
    if len(weighted) > 1:
        var = sum(w * (v - mean) ** 2 for v, w in weighted) / total_w
        # Bessel-style correction for weighted samples.
        n_eff = total_w ** 2 / sum(w ** 2 for _, w in weighted)
        if n_eff > 1:
            var *= n_eff / (n_eff - 1)
        sd = math.sqrt(max(var, 0.0))
    else:
        sd = 0.0
    sd = max(sd, _min_sd(stat_key, sample.sport))
    return Projection(
        stat_key=stat_key,
        mean=mean,
        sd=sd,
        n_eff=total_w,
        n_games=len(weighted),
        n_vs=len(sample.values(stat_key, vs_only=True)),
        vs_average=sample.vs_average(stat_key),
        recent_average=sample.recent_average(stat_key),
        sport=sample.sport,
    )


# --------------------------------------------------------------- probability
def _normal_at_least(mean: float, sd: float, line: float) -> float:
    """P(X >= line) for a normal. Lines are x.5, so no continuity correction."""
    if sd <= 0:
        return 1.0 if mean >= line else 0.0
    z = (line - mean) / sd
    return 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def hit_probability(sample: PlayerSample, stat_key: str, line: float,
                    projection: Projection | None = None) -> float | None:
    """Blended probability that the player clears `line`."""
    proj = projection or project(sample, stat_key)
    if proj is None:
        return None
    weighted = _weights(sample, stat_key)
    total_w = sum(w for _, w in weighted)
    if total_w <= 0:
        return None

    p_empirical = sum(w for v, w in weighted if v >= line) / total_w
    p_normal = _normal_at_least(proj.mean, proj.sd, line)

    trust = total_w / (total_w + EMPIRICAL_K)
    blended = trust * p_empirical + (1 - trust) * p_normal
    # Thin samples get pulled toward a coin flip.
    shrunk = ((blended * total_w + 0.5 * SHRINK_STRENGTH)
              / (total_w + SHRINK_STRENGTH))
    return max(0.01, min(0.99, shrunk))


def count_hits(games: list[dict], stat_key: str, line: float) -> tuple[int, int]:
    """(games clearing the line, games with the stat recorded)."""
    values = [g[stat_key] for g in games if g.get(stat_key) is not None]
    return sum(1 for v in values if v >= line), len(values)


# --------------------------------------------------------------- line search
def candidate_lines(projection: Projection) -> list[float]:
    """Plausible x.5 lines at or below the projection, strongest first.

    The walk has to span enough ground to reach a line the model actually
    likes: for yardage that means dropping well below the projection, so the
    range scales with the stat's own spread rather than a fixed step count.
    """
    step = _line_step(projection.stat_key, projection.sport)
    if projection.mean <= 0:
        return []
    span = max(2.0 * projection.sd, step * 4)
    steps = min(24, max(6, int(span / step) + 1))
    top = math.floor(projection.mean / step) * step + 0.5
    lines = [top - i * step for i in range(0, steps)]
    return [round(x, 1) for x in lines if x > 0]


def best_line(sample: PlayerSample, stat_key: str, target: float = 0.72,
              projection: Projection | None = None) -> tuple[float, float] | None:
    """Highest line still clearing `target` probability -> (line, probability).

    Walking down from the projection means the returned line is the most
    demanding one the model still likes, not the safest one available.
    """
    proj = projection or project(sample, stat_key)
    if proj is None:
        return None
    best: tuple[float, float] | None = None
    for line in candidate_lines(proj):
        p = hit_probability(sample, stat_key, line, proj)
        if p is None:
            continue
        if p >= target:
            return line, p
        if best is None or p > best[1]:
            best = (line, p)
    return best


def candidates_for_player(sample: PlayerSample, target: float = 0.72,
                          min_probability: float = 0.70,
                          min_games: int = 4) -> list[PropCandidate]:
    """Every stat for one player that clears the probability bar."""
    out: list[PropCandidate] = []
    for stat_key in sample.stat_keys():
        proj = project(sample, stat_key)
        if proj is None or proj.n_games < min_games or proj.mean <= 0:
            continue
        found = best_line(sample, stat_key, target, proj)
        if not found:
            continue
        line, probability = found
        if probability < min_probability:
            continue
        out.append(PropCandidate(
            athlete_id=sample.athlete_id,
            player=sample.name,
            position=sample.position,
            team=sample.team,
            opponent=sample.opponent,
            stat_key=stat_key,
            line=line,
            probability=probability,
            projection=proj,
            hit_vs_opponent=count_hits(sample.vs_games, stat_key, line),
            hit_recent=count_hits(sample.recent_games, stat_key, line),
            injury_status=sample.injury_status,
            sport=sample.sport,
        ))
    return out


# ------------------------------------------------------- market-priced props
def market_candidates(sample: PlayerSample, book, min_probability: float = 0.60,
                      min_edge: float = 0.03, min_games: int = 4,
                      both_sides: bool = True) -> list[PropCandidate]:
    """Score a player against the lines books are actually offering.

    For every offered line the model produces P(over). Comparing that with the
    vig-free price on each side turns the projection into a real edge, which is
    what separates a bet worth placing from a stat that merely looks pretty.
    """
    from .edge import american_to_prob, remove_vig

    from ..data.odds_client import preferred_book
    favourite = preferred_book()
    trust = market_trust()

    out: list[PropCandidate] = []
    for stat_key in sample.stat_keys():
        offers = book.lines_for(sample.name, stat_key)
        if not offers:
            continue
        # Quote the preferred book's line when it has one, rather than a
        # better-priced line at a book you are not betting with.
        offers = [o for o in offers if o.get("bookmaker") == favourite] or offers
        proj = project(sample, stat_key)
        if proj is None or proj.n_games < min_games:
            continue
        for offer in offers:
            line = offer["line"]
            p_over = hit_probability(sample, stat_key, line, proj)
            if p_over is None:
                continue
            over_price, under_price = offer.get("over"), offer.get("under")
            cleared_vs, total_vs = count_hits(sample.vs_games, stat_key, line)
            cleared_recent, total_recent = count_hits(sample.recent_games, stat_key, line)

            sides = [("over", p_over, over_price, under_price)]
            if both_sides:
                sides.append(("under", 1 - p_over, under_price, over_price))
            for side, model_prob, price, other_price in sides:
                if price is None or model_prob < min_probability:
                    continue
                try:
                    implied = remove_vig(american_to_prob(other_price),
                                         american_to_prob(price))[1] if other_price else \
                        american_to_prob(price)
                except ValueError:
                    implied = american_to_prob(price)
                # Pull the model toward the market before calling it an edge.
                blended = ((1 - trust) * model_prob + trust * implied)
                value = blended - implied
                if value < min_edge:
                    continue
                notes = _warnings(sample, proj, model_prob - implied,
                                  model_prob, implied)
                out.append(PropCandidate(
                    athlete_id=sample.athlete_id, player=sample.name,
                    position=sample.position, team=sample.team,
                    opponent=sample.opponent, stat_key=stat_key, line=line,
                    probability=round(blended, 4), projection=proj,
                    hit_vs_opponent=(cleared_vs, total_vs),
                    hit_recent=(cleared_recent, total_recent),
                    injury_status=sample.injury_status, side=side,
                    odds=int(price), implied_prob=round(implied, 4),
                    edge=round(value, 4), bookmaker=offer.get("bookmaker", ""),
                    sport=sample.sport,
                    raw_probability=round(model_prob, 4), warnings=notes))

    # One entry per player-stat-side: keep the best-priced version of each.
    best: dict[tuple[str, str, str], PropCandidate] = {}
    for candidate in out:
        key = (candidate.athlete_id, candidate.stat_key, candidate.side)
        if key not in best or (candidate.edge or 0) > (best[key].edge or 0):
            best[key] = candidate
    return sorted(best.values(), key=lambda c: (c.edge or 0), reverse=True)


def _warnings(sample: PlayerSample, projection: Projection, value: float,
              model_prob: float, implied: float) -> list[str]:
    """Reasons a human should hesitate, in plain words."""
    notes: list[str] = []
    if value >= max_raw_edge():
        notes.append(
            f"Model is {abs(model_prob - implied):.0%} away from the market. "
            "That usually means a role change or injury the model cannot see.")
    if sample.injury_status:
        notes.append(f"Listed {sample.injury_status}.")
    if projection.n_games < 6:
        notes.append(f"Only {projection.n_games} games in the sample.")
    if projection.n_vs == 0:
        notes.append("No prior game against this opponent.")
    return notes


def rank_market_candidates(samples: list[PlayerSample], book,
                           min_probability: float = 0.60, min_edge: float = 0.03,
                           require_h2h: bool = False, limit: int = 40,
                           rank_by: str = "edge") -> list[PropCandidate]:
    """Market-priced props across a whole matchup, best first."""
    out: list[PropCandidate] = []
    for sample in samples:
        out += market_candidates(sample, book, min_probability, min_edge)
    if require_h2h:
        out = [c for c in out if c.hit_vs_opponent[1] > 0]
    key = ((lambda c: c.probability) if rank_by == "probability"
           else (lambda c: (c.edge or 0)))
    out.sort(key=key, reverse=True)
    return out[:limit]


def rank_candidates(samples: list[PlayerSample], target: float = 0.72,
                    min_probability: float = 0.70, require_h2h: bool = False,
                    limit: int = 25) -> list[PropCandidate]:
    """All players' candidates, best first.

    `require_h2h=True` keeps only props with at least one prior game against
    this opponent - the strict reading of "head to head".
    """
    out: list[PropCandidate] = []
    for sample in samples:
        out += candidates_for_player(sample, target, min_probability)
    if require_h2h:
        out = [c for c in out if c.hit_vs_opponent[1] > 0]
    out.sort(key=lambda c: (c.probability, c.projection.n_eff), reverse=True)
    return out[:limit]
