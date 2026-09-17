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
"""
from __future__ import annotations

import datetime as dt

from ..db import database as db
from . import board as board_mod

DEFAULTS = {
    "enabled": True,
    "min_confidence": 6,
    "min_edge": 0.04,
    "min_backing_rate": None,
    "min_backing_games": 0,
    "max_per_day": 5,
    "status": "approved",
}


def settings(config: dict | None = None) -> dict:
    cfg = dict(DEFAULTS)
    block = (config or db.get_config()).get("auto_pick") or {}
    cfg.update({k: v for k, v in block.items() if k in DEFAULTS})
    return cfg


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
def already_tracked(game_id: int, market: str, side: str, line, db_path) -> bool:
    """Has this exact bet already been written? Never log the same pick twice."""
    rows = db.query(db_path, """
        SELECT p.id FROM picks p JOIN pick_legs l ON l.pick_id = p.id
        WHERE l.game_id = ? AND l.market = ? AND LOWER(l.team) = LOWER(?)
          AND COALESCE(l.line, -99999) = COALESCE(?, -99999)
          AND p.status != 'rejected'
        LIMIT 1""", (game_id, market, side, line))
    return bool(rows)


def commit(card, candidate, rules: dict, db_path=db.DEFAULT_DB) -> int | None:
    """Write one pick to the ledger. Returns its id, or None if it was skipped.

    Refuses anything already under way. The board should never hand one over,
    but this is the last gate before a bet enters the tracked record, and a
    live price recorded as a pregame pick would quietly corrupt it.
    """
    if card.started:
        return None
    if already_tracked(candidate.game_id, candidate.market, candidate.side,
                       candidate.line, db_path):
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
                odds_american, model_prob, implied_prob, edge, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (pick_id, 0, candidate.game_id, candidate.market, candidate.side,
             candidate.line, candidate.odds, candidate.model_prob,
             candidate.implied_prob, candidate.edge, reason))
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
    return " · ".join(parts)


# ------------------------------------------------------------------- the run
def run(sport: str = "nfl", days: int = 3, start: dt.date | None = None,
        db_path=db.DEFAULT_DB, config: dict | None = None,
        dry_run: bool = False) -> dict:
    """Scan upcoming games and commit everything confident enough.

    Safe to run repeatedly - a bet already in the ledger is skipped, so this
    can sit behind a refresh button or a scheduled job.
    """
    rules = settings(config)
    if not rules["enabled"] and not dry_run:
        return {"sport": sport, "enabled": False, "added": 0, "picks": []}

    cards = board_mod.upcoming_board(sport, days=days, start=start, db_path=db_path)
    chosen = select(cards, rules)

    added, skipped = [], 0
    for card, candidate in chosen:
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
            "reason": _reason(candidate),
        } for card, candidate, pick_id in added],
    }


def run_all(days: int = 3, start: dt.date | None = None, db_path=db.DEFAULT_DB,
            config: dict | None = None, dry_run: bool = False) -> list[dict]:
    """Auto-pick across every enabled sport."""
    from ..data import sports as sport_registry
    return [run(code, days, start, db_path, config, dry_run)
            for code in sport_registry.enabled_codes(config)]
