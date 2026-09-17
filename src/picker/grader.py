"""Grading: final scores -> Win/Loss/Push, units, records.

Pure grading math is unit-tested in tests/test_grader.py.
"""
from __future__ import annotations

import datetime as dt

from ..db import database as db
from .edge import payout_multiplier


def grade_straight(market: str, team: str, line: float | None,
                   away_score: int, home_score: int,
                   away_abbrev: str, home_abbrev: str) -> str:
    """Return W / L / P for one straight bet.

    - h2h:    team (abbrev) must win outright.
    - spread: line is signed for `team` (favorite negative, e.g. -6.5).
              Cover if margin + line > 0 (margin = team_score - opp_score).
    - total:  team in {Over, Under}; Over if sum > line.
    """
    market = (market or "").lower()
    margin = (home_score - away_score) if team == home_abbrev else (away_score - home_score)

    if market == "h2h":
        return "W" if margin > 0 else ("L" if margin < 0 else "P")
    if market == "spread":
        diff = margin + (float(line) if line is not None else 0.0)
        if diff > 0:
            return "W"
        if diff < 0:
            return "L"
        return "P"
    if market == "total":
        total = home_score + away_score
        o = team.strip().lower()
        if o == "over":
            return "W" if total > line else ("L" if total < line else "P")
        if o == "under":
            return "W" if total < line else ("L" if total > line else "P")
        raise ValueError(f"unknown total side: {team}")
    # market aliases used by The Odds API payloads -> normalize before grading
    raise ValueError(f"unsupported market: {market}")


def units_won(result: str, units: float, payout_mult: float) -> float:
    """Profit (stake returned on push). W -> units*payout_mult, L -> -units, P -> 0."""
    if result == "W":
        return round(units * payout_mult, 4)
    if result == "L":
        return -round(units, 4)
    return 0.0


def grade_pick(pick_id: int, db_path=db.DEFAULT_DB) -> dict:
    """Grade a pick from the database using stored leg lines + final scores.
    Returns {'result': W/L/P, 'units_won': float} and persists it."""
    pick = db.query(db_path, "SELECT * FROM picks WHERE id=?", (pick_id,))
    if not pick:
        raise ValueError(f"no pick {pick_id}")
    pick = pick[0]
    legs = db.query(db_path,
                    "SELECT * FROM pick_legs WHERE pick_id=? ORDER BY ord", (pick_id,))
    if not legs:
        raise ValueError(f"pick {pick_id} has no legs")
    games = {}
    for leg in legs:
        game_rows = db.query(db_path, "SELECT * FROM games WHERE id=?", (leg["game_id"],))
        if not game_rows:
            raise ValueError(f"pick {pick_id} references a missing game")
        g = game_rows[0]
        if g["status"] != "final":
            raise ValueError(f"game {g['id']} is not final")
        if g["away_score"] is None or g["home_score"] is None:
            raise ValueError(f"game {g['id']} is final but has no complete score")
        games[int(leg["game_id"])] = g

    if pick["nature"] == "straight":
        leg = legs[0]
        g = games[int(leg["game_id"])]
        away = db.query(db_path, "SELECT abbrev FROM teams WHERE id=?", (g["away_team"],))[0]["abbrev"]
        home = db.query(db_path, "SELECT abbrev FROM teams WHERE id=?", (g["home_team"],))[0]["abbrev"]
        result = grade_straight(leg["market"], leg["team"], leg["line"],
                                g["away_score"], g["home_score"], away, home)
        units = pick["units_final"] or pick["units_model"] or 1.0
        won = units_won(result, units, payout_multiplier(leg["odds_american"]))

    else:  # parlay
        leg_results = []
        for leg in legs:
            g = games[int(leg["game_id"])]
            away = db.query(db_path, "SELECT abbrev FROM teams WHERE id=?", (g["away_team"],))[0]["abbrev"]
            home = db.query(db_path, "SELECT abbrev FROM teams WHERE id=?", (g["home_team"],))[0]["abbrev"]
            leg_result = grade_straight(leg["market"], leg["team"], leg["line"],
                                        g["away_score"], g["home_score"], away, home)
            leg_results.append((leg, leg_result))
        units = pick["units_final"] or pick["units_model"] or 1.0
        if any(r == "L" for _, r in leg_results):
            result = "L"
            won = -units
        elif all(r == "P" for _, r in leg_results):
            result = "P"
            won = 0.0
        else:
            # Pushed legs are removed and the remaining winning legs pay out.
            result = "W"
            dec = 1.0
            for leg, leg_result in leg_results:
                if leg_result == "P":
                    continue
                from .edge import decimal_from_american
                dec *= decimal_from_american(leg["odds_american"])
            won = units_won("W", units, dec - 1.0)

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn = db.connect(db_path)
    try:
        conn.execute("""INSERT OR REPLACE INTO results
                     (pick_id,result,units_won,graded_utc) VALUES (?,?,?,?)""",
                     (pick_id, result, won, now))
        conn.execute("UPDATE picks SET status='graded' WHERE id=?", (pick_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"result": result, "units_won": won}


def ungraded_picks(db_path=db.DEFAULT_DB, game_date: str | None = None) -> list:
    """Picks whose games are all final but not yet graded."""
    sql = """
        SELECT p.id FROM picks p
        WHERE p.status != 'graded'
    """
    params: tuple = ()
    if game_date:
        sql += """ AND EXISTS (
            SELECT 1 FROM pick_legs dl JOIN games dg ON dg.id=dl.game_id
            WHERE dl.pick_id=p.id
              AND COALESCE(dg.game_date, date(dg.start_utc))=date(?)
        )"""
        params = (game_date,)
    sql += " GROUP BY p.id"
    rows = db.query(db_path, sql, params)
    out = []
    for row in rows:
        legs = db.query(db_path, "SELECT game_id FROM pick_legs WHERE pick_id=?", (row["id"],))
        if not legs:
            continue
        games = [db.query(db_path,
                          "SELECT status,away_score,home_score FROM games WHERE id=?",
                          (leg["game_id"],)) for leg in legs]
        if all(rows and rows[0]["status"] == "final"
               and rows[0]["away_score"] is not None
               and rows[0]["home_score"] is not None for rows in games):
            out.append(row["id"])
    return out


def grade_all(db_path=db.DEFAULT_DB, game_date: str | None = None) -> list[dict]:
    graded = []
    for pick_id in ungraded_picks(db_path, game_date):
        graded.append(grade_pick(pick_id, db_path))
    return graded
