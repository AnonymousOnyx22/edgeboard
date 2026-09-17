"""Approval-workflow actions shared by the dashboard and CLI.

suggested -> approved / edited / rejected -> content_generated -> scheduled -> graded
"""
from __future__ import annotations

import datetime as dt

from ..db import database as db


def approve_pick(pick_id: int, db_path=db.DEFAULT_DB) -> None:
    """Suggest -> approved. Copies model confidence/units as the final defaults."""
    row = db.query(db_path, "SELECT * FROM picks WHERE id=?", (pick_id,))
    if not row:
        raise ValueError(f"no pick {pick_id}")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.execute(db_path, """
        UPDATE picks SET status='approved', approved_utc=?,
            confidence_final=COALESCE(confidence_final, confidence_model),
            units_final=COALESCE(units_final, units_model)
        WHERE id=?""", (now, pick_id))


def edit_pick(pick_id: int, confidence: float | None = None, units: float | None = None,
              reason: str | None = None, db_path=db.DEFAULT_DB) -> None:
    """Supercede a suggestion with my own judgement -> status 'edited'."""
    row = db.query(db_path, "SELECT * FROM picks WHERE id=?", (pick_id,))
    if not row:
        raise ValueError(f"no pick {pick_id}")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    sets = ["status='edited'", "edited_utc=?"]
    params: list = [now]
    if confidence is not None:
        sets.append("confidence_final=?")
        params.append(float(confidence))
    if units is not None:
        sets.append("units_final=?")
        params.append(float(units))
    if reason is not None:
        sets.append("reason=?")
        params.append(str(reason))
    params.append(pick_id)
    db.execute(db_path, f"UPDATE picks SET {', '.join(sets)} WHERE id=?", tuple(params))


def reject_pick(pick_id: int, db_path=db.DEFAULT_DB) -> None:
    db.execute(db_path, "UPDATE picks SET status='rejected' WHERE id=?", (pick_id,))


def approve_all(suggested_ids: list[int], db_path=db.DEFAULT_DB) -> int:
    n = 0
    for pid in suggested_ids:
        row = db.query(db_path, "SELECT status FROM picks WHERE id=?", (pid,))
        if row and row[0]["status"] == "suggested":
            approve_pick(pid, db_path)
            n += 1
    return n


def build_parlay_from_picks(pick_ids: list[int], db_path=db.DEFAULT_DB,
                            min_legs: int = 2, max_legs: int = 4) -> dict:
    """Combine approved straight picks into a parlay (no same-game legs)."""
    from .edge import american_to_prob
    from .parlay_builder import Leg, ParlayError, build_parlay
    if len(set(pick_ids)) != len(pick_ids):
        return {"ok": False, "error": "duplicate picks are not allowed"}
    legs = []
    sport_ids = set()
    for pid in pick_ids:
        row = db.query(db_path, "SELECT * FROM picks WHERE id=?", (pid,))
        if not row:
            raise ValueError(f"no pick {pid}")
        if row[0]["status"] not in ("approved", "edited"):
            return {"ok": False, "error": f"pick {pid} must be approved first"}
        sport_ids.add(row[0]["sport_id"])
        leg_rows = db.query(db_path, "SELECT * FROM pick_legs WHERE pick_id=?", (pid,))
        if len(leg_rows) != 1:
            raise ValueError(f"pick {pid} is not a straight pick")
        lr = leg_rows[0]
        legs.append(Leg(game_id=lr["game_id"], market=lr["market"], team=lr["team"],
                        line=lr["line"], odds_american=lr["odds_american"],
                        model_prob=lr["model_prob"] or 0.5,
                        reason=lr["reason"] or ""))
    if len(sport_ids) != 1:
        return {"ok": False, "error": "all parlay legs must be from the same sport"}
    try:
        parlay = build_parlay(legs, min_legs=min_legs, max_legs=max_legs)
    except ParlayError as e:
        return {"ok": False, "error": str(e)}

    sport_id = sport_ids.pop()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    cur = db.connect(db_path)
    cur.execute("""INSERT INTO picks (sport_id, nature, source, status, edge, confidence_model,
                 confidence_final, units_model, units_final, reason, created_utc)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (sport_id, "parlay", "model", "approved",
                 parlay.combined_model_prob - parlay.implied_prob,
                 max(1, min(10, round((parlay.combined_model_prob - parlay.implied_prob) * 100))),
                 max(1, min(10, round((parlay.combined_model_prob - parlay.implied_prob) * 100))),
                 1.0, 1.0, "Parlay built from approved picks", now))
    pick_id = cur.lastrowid
    for i, leg in enumerate(legs):
        cur.execute("""INSERT INTO pick_legs (pick_id, ord, game_id, market, team, line,
                     odds_american, model_prob, implied_prob, edge, reason)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (pick_id, i, leg.game_id, leg.market, leg.team, leg.line,
                     leg.odds_american, leg.model_prob,
                     round(american_to_prob(leg.odds_american), 4),
                     round(leg.model_prob - american_to_prob(leg.odds_american), 4), leg.reason))
    cur.commit()
    cur.close()
    return {"ok": True, "pick_id": pick_id, "name": parlay.american_display()}
