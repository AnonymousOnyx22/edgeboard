"""Automatic pick selection.

The board shows everything that clears a low edge threshold, which is a
research view. This is the opinionated one: when the model is confident enough
in a number, the pick is written to the ledger by itself, so the record builds
up without anyone deciding which bets to remember.

That last part matters. A tracked record is only honest if the picks were
committed *before* the games were played and without hindsight about which ones
to log. Everything here runs against upcoming games only, and a pick is saved
once and never rewritten.

What "confident enough" means lives in config/settings.yaml:

    auto_pick:
      enabled: true
      min_confidence: 6        # of 10
      min_edge: 0.04           # model probability over the vig-free price
      min_backing_rate: ~      # optionally also require a head-to-head record
      max_per_day: 5           # per sport
      stake: model             # "model" uses the confidence tiers
      props:
        enabled: true
        min_confidence: 6
        min_edge: 0.04
        max_per_day: 3         # per sport, on top of the game-line cap
        max_games: 4           # games to pull player data for
        max_players: 12        # per team
        fetch_prices: false    # true spends Odds API credits
        require_h2h: false     # only props with a prior meeting on record

Player props ride the same path as game lines. A prop is only ever committed
when a book actually priced it - an unpriced model-only projection has no edge
to measure, and a tracked record built on the model marking its own homework
would be worthless.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..db import database as db
from . import board as board_mod
from . import confidence as conf

# Props share the `market` column with game lines, so they carry a prefix that
# no game market can collide with. The suffix is the ESPN stat key the model
# and the grader both speak (passingYards, receivingYards, hits, ...).
PROP_MARKET_PREFIX = "prop:"

DEFAULTS = {
    "enabled": True,
    "min_confidence": 6,
    "min_edge": 0.04,
    "min_backing_rate": None,
    "min_backing_games": 0,
    "max_per_day": 5,
    "status": "approved",
}

PROP_DEFAULTS = {
    "enabled": True,
    "min_confidence": 6,
    "min_edge": 0.04,
    "max_per_day": 3,
    "max_games": 4,
    "max_players": 12,
    # A prop is only committable when a book priced it, so the picker has to
    # be willing to buy prices. One credit per market per game, bounded by
    # max_games; PropsOddsClient still refuses to spend below its credit floor.
    "fetch_prices": True,
    "require_h2h": False,
    "min_probability": 0.55,
}


def settings(config: dict | None = None) -> dict:
    cfg = dict(DEFAULTS)
    block = (config or db.get_config()).get("auto_pick") or {}
    cfg.update({k: v for k, v in block.items() if k in DEFAULTS})
    return cfg


def prop_settings(config: dict | None = None) -> dict:
    """The `auto_pick.props` block, defaulted."""
    cfg = dict(PROP_DEFAULTS)
    block = ((config or db.get_config()).get("auto_pick") or {}).get("props") or {}
    cfg.update({k: v for k, v in block.items() if k in PROP_DEFAULTS})
    return cfg


# -------------------------------------------------------------------- props
def prop_market(stat_key: str) -> str:
    """The `market` value a prop leg is stored under."""
    return f"{PROP_MARKET_PREFIX}{stat_key}"


def is_prop_market(market: str | None) -> bool:
    return (market or "").startswith(PROP_MARKET_PREFIX)


def stat_key_of(market: str | None) -> str | None:
    """The ESPN stat key behind a prop market, or None for a game line."""
    if not is_prop_market(market):
        return None
    return market[len(PROP_MARKET_PREFIX):]


@dataclass
class PropPick:
    """One player prop wearing the same face as a game line.

    The rules, the dedupe check and the write are shared between the two, so a
    prop has to answer everything `qualifies()` and `commit()` ask of a
    `LineCandidate`: a market, a side, a price, an edge and a confidence score.
    `backing` stays None - a prop's evidence is the player's own game log, not
    a record between two clubs, and it is carried in `note` instead.
    """
    game_id: int
    market: str
    side: str                  # "Over" / "Under", as stored
    line: float
    odds: int
    model_prob: float
    implied_prob: float
    edge: float
    confidence: dict
    units: float
    athlete_id: str
    player: str
    headline: str
    note: str = ""
    backing: object | None = None
    warnings: list = field(default_factory=list)


def prop_quality(candidate) -> dict:
    """Data-quality flags for a prop, in the shape confidence.py expects."""
    projection = candidate.projection
    return {
        "games_of_history": projection.n_games,
        "no_head_to_head": projection.n_vs == 0,
        "missing_stat_feed": projection.n_games == 0,
        "missing_injury_data": not candidate.injury_status,
    }


def to_prop_pick(candidate) -> PropPick | None:
    """Adapt a priced `props.PropCandidate` into a committable pick.

    Returns None for anything unpriced: with no book price there is no
    vig-free number to measure an edge against, and a projection graded
    against a line the model chose itself is not a record of anything.
    """
    if not candidate.priced or candidate.edge is None:
        return None
    score = conf.compute_confidence(candidate.edge, candidate.probability,
                                    prop_quality(candidate))
    return PropPick(
        game_id=int(candidate.game_id or 0),
        market=prop_market(candidate.stat_key),
        side=candidate.side.capitalize(),
        line=float(candidate.line),
        odds=int(candidate.odds),
        model_prob=float(candidate.probability),
        implied_prob=float(candidate.implied_prob),
        edge=float(candidate.edge),
        confidence=score,
        units=conf.units_from_confidence(score["score"]),
        athlete_id=str(candidate.athlete_id),
        player=candidate.player,
        headline=candidate.headline,
        note=candidate.evidence(),
        warnings=list(candidate.warnings),
    )


def prop_candidates(cards, rules: dict, progress=None) -> list[tuple]:
    """[(card, PropPick)] across the upcoming slate.

    Pulling player data is the slow part - a cold matchup is both rosters'
    game logs - so only the first `max_games` bettable games are read, and
    prices come from cache unless `fetch_prices` is on.
    """
    playable = [card for card in cards if card.bettable][: rules["max_games"]]
    out: list[tuple] = []
    for i, card in enumerate(playable):
        if progress:
            progress(i / max(1, len(playable)), f"Props for {card.matchup}")
        try:
            matchup = board_mod.deep_matchup(
                card,
                min_probability=rules["min_probability"],
                min_edge=rules["min_edge"],
                require_h2h=rules["require_h2h"],
                max_players=rules["max_players"],
                fetch_prices=rules["fetch_prices"],
            )
        except Exception:
            # One unreachable matchup should not stop the rest of the slate.
            continue
        if not matchup.priced:
            continue
        for candidate in matchup.props:
            pick = to_prop_pick(candidate)
            if pick is not None:
                out.append((card, pick))
    return out


def prop_qualifies(pick: PropPick, rules: dict) -> tuple[bool, str]:
    """Does this prop clear the bar? Returns (ok, why not)."""
    if pick.warnings:
        return False, "; ".join(pick.warnings)
    score = (pick.confidence or {}).get("score", 0)
    if score < rules["min_confidence"]:
        return False, f"confidence {score} below {rules['min_confidence']}"
    if pick.edge < rules["min_edge"]:
        return False, f"edge {pick.edge:.1%} below {rules['min_edge']:.1%}"
    return True, ""


def select_props(pairs, rules: dict) -> list:
    """The props worth committing, best first, capped per sport."""
    chosen = [(card, pick) for card, pick in pairs
              if prop_qualifies(pick, rules)[0]]
    chosen.sort(key=lambda pair: (pair[1].confidence.get("score", 0), pair[1].edge),
                reverse=True)
    return chosen[: rules["max_per_day"]]


# ------------------------------------------------------------------ selection
def qualifies(candidate, rules: dict) -> tuple[bool, str]:
    """Does this candidate clear the bar? Returns (ok, why not)."""
    score = (candidate.confidence or {}).get("score", 0)
    if score < rules["min_confidence"]:
        return False, f"confidence {score} below {rules['min_confidence']}"
    if candidate.edge < rules["min_edge"]:
        return False, f"edge {candidate.edge:.1%} below {rules['min_edge']:.1%}"

    backing = getattr(candidate, "backing", None)
    needed_games = rules.get("min_backing_games") or 0
    if needed_games:
        if backing is None or backing.total < needed_games:
            have = backing.total if backing else 0
            return False, f"only {have} prior meetings, needs {needed_games}"

    needed_rate = rules.get("min_backing_rate")
    if needed_rate is not None:
        if backing is None or backing.rate is None:
            return False, "no head-to-head record to check"
        if backing.rate < needed_rate:
            return False, f"cashed {backing.rate:.0%} historically, needs {needed_rate:.0%}"
    return True, ""


def select(cards, rules: dict) -> list:
    """The candidates worth committing, best first, capped per sport."""
    chosen = []
    for card in cards:
        if not card.bettable:
            continue
        for candidate in card.candidates:
            ok, _ = qualifies(candidate, rules)
            if ok:
                chosen.append((card, candidate))
    chosen.sort(key=lambda pair: (pair[1].confidence.get("score", 0), pair[1].edge),
                reverse=True)
    return chosen[: rules["max_per_day"]]


# ----------------------------------------------------------------- persistence
def already_tracked(game_id: int, market: str, side: str, line, db_path,
                    athlete_id: str | None = None) -> bool:
    """Has this exact bet already been written? Never log the same pick twice.

    Two players in one game can hold the same market, side and line, so a prop
    is only the same bet when it is the same athlete.
    """
    rows = db.query(db_path, """
        SELECT p.id FROM picks p JOIN pick_legs l ON l.pick_id = p.id
        WHERE l.game_id = ? AND l.market = ? AND LOWER(l.team) = LOWER(?)
          AND COALESCE(l.line, -99999) = COALESCE(?, -99999)
          AND COALESCE(l.athlete_id, '') = COALESCE(?, '')
          AND p.status != 'rejected'
        LIMIT 1""", (game_id, market, side, line, athlete_id or ""))
    return bool(rows)


def commit(card, candidate, rules: dict, db_path=db.DEFAULT_DB) -> int | None:
    """Write one pick to the ledger. Returns its id, or None if it was skipped.

    Refuses anything already under way. The board should never hand one over,
    but this is the last gate before a bet enters the tracked record, and a
    live price recorded as a pregame pick would quietly corrupt it.
    """
    if card.started:
        return None
    athlete_id = getattr(candidate, "athlete_id", None)
    if already_tracked(candidate.game_id, candidate.market, candidate.side,
                       candidate.line, db_path, athlete_id):
        return None

    sport_rows = db.query(db_path, "SELECT id FROM sports WHERE code=?", (card.sport,))
    if not sport_rows:
        return None

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    score = candidate.confidence.get("score")
    reason = _reason(candidate)

    conn = db.connect(db_path)
    try:
        cursor = conn.execute("""
            INSERT INTO picks (sport_id, nature, source, status, edge,
                confidence_model, confidence_final, units_model, units_final,
                reason, created_utc, approved_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sport_rows[0]["id"], "straight", "model", rules["status"],
             candidate.edge, score, score, candidate.units, candidate.units,
             reason, now, now if rules["status"] == "approved" else None))
        pick_id = cursor.lastrowid
        conn.execute("""
            INSERT INTO pick_legs (pick_id, ord, game_id, market, team, line,
                odds_american, model_prob, implied_prob, edge, reason,
                athlete_id, player)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pick_id, 0, candidate.game_id, candidate.market, candidate.side,
             candidate.line, candidate.odds, candidate.model_prob,
             candidate.implied_prob, candidate.edge, reason,
             athlete_id, getattr(candidate, "player", None)))
        conn.commit()
        return pick_id
    finally:
        conn.close()


def _reason(candidate) -> str:
    """Why this was taken, in one line, saved with the pick."""
    parts = [f"model {candidate.model_prob:.1%} vs {candidate.implied_prob:.1%} implied",
             f"edge {candidate.edge:+.1%}",
             f"confidence {candidate.confidence.get('score')}/10"]
    backing = getattr(candidate, "backing", None)
    if backing is not None and backing.total:
        parts.append(f"{backing.hits}/{backing.total} in prior meetings")
    note = getattr(candidate, "note", "")
    if note:
        parts.append(note)
    return " · ".join(parts)


# ------------------------------------------------------------------- the run
def run(sport: str = "nfl", days: int = 3, start: dt.date | None = None,
        db_path=db.DEFAULT_DB, config: dict | None = None,
        dry_run: bool = False, props: bool = True) -> dict:
    """Scan upcoming games and commit everything confident enough.

    Safe to run repeatedly - a bet already in the ledger is skipped, so this
    can sit behind a refresh button or a scheduled job.
    """
    rules = settings(config)
    if not rules["enabled"] and not dry_run:
        return {"sport": sport, "enabled": False, "added": 0, "picks": []}

    cards = board_mod.upcoming_board(sport, days=days, start=start, db_path=db_path)
    chosen = select(cards, rules)

    prop_rules = prop_settings(config)
    prop_pairs, prop_chosen = [], []
    if props and prop_rules["enabled"]:
        prop_pairs = prop_candidates(cards, prop_rules)
        prop_chosen = select_props(prop_pairs, prop_rules)

    added, skipped = [], 0
    for card, candidate in list(chosen) + list(prop_chosen):
        if dry_run:
            added.append((card, candidate, None))
            continue
        pick_id = commit(card, candidate, rules, db_path)
        if pick_id is None:
            skipped += 1
        else:
            added.append((card, candidate, pick_id))

    return {
        "sport": sport,
        "enabled": rules["enabled"],
        "considered": sum(len(c.candidates) for c in cards if c.bettable),
        "props_considered": len(prop_pairs),
        "props_added": sum(1 for _, c, _ in added if is_prop_market(c.market)),
        "added": len(added),
        "already_tracked": skipped,
        "picks": [{
            "pick_id": pick_id,
            "game": card.matchup,
            "kickoff": card.kickoff_local(),
            "headline": candidate.headline,
            "odds": candidate.odds,
            "edge": candidate.edge,
            "confidence": candidate.confidence.get("score"),
            "units": candidate.units,
            "kind": "prop" if is_prop_market(candidate.market) else "line",
            "player": getattr(candidate, "player", None),
            "reason": _reason(candidate),
        } for card, candidate, pick_id in added],
    }


def run_all(days: int = 3, start: dt.date | None = None, db_path=db.DEFAULT_DB,
            config: dict | None = None, dry_run: bool = False,
            props: bool = True) -> list[dict]:
    """Auto-pick across every enabled sport."""
    from ..data import sports as sport_registry
    return [run(code, days, start, db_path, config, dry_run, props)
            for code in sport_registry.enabled_codes(config)]
