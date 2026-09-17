"""Persistence and price snapshots for model-selected parlays."""
from __future__ import annotations

import datetime as dt

from ..db import database as db

def save_model_parlay(parlay, sport: str = "nfl", db_path=db.DEFAULT_DB) -> dict:
    """Persist a server-generated parlay and its opening-price snapshot."""
    from .edge import american_to_prob
    from .parlay_builder import build_parlay

    checked = build_parlay(list(parlay.legs), min_legs=2, max_legs=6)
    game_ids = [int(leg.game_id) for leg in checked.legs]
    placeholders = ",".join("?" for _ in game_ids)
    games = db.query(db_path, f"""
        SELECT id,status,start_utc FROM games WHERE id IN ({placeholders})
    """, tuple(game_ids))
    if len(games) != len(set(game_ids)):
        raise ValueError("one or more parlay games no longer exist")
    now = dt.datetime.now(dt.timezone.utc)
    for game in games:
        try:
            start = dt.datetime.fromisoformat(game["start_utc"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("a parlay game has an invalid start time") from exc
        if game["status"] != "scheduled" or start <= now:
            raise ValueError("parlays can only be tracked before every game starts")

    signature = _leg_signature(checked.legs)
    for row in db.query(db_path, """
            SELECT id FROM picks
            WHERE nature='parlay' AND status IN ('approved','edited','scheduled')
            ORDER BY id DESC"""):
        existing = db.query(db_path, """
            SELECT game_id,market,team,line FROM pick_legs
            WHERE pick_id=? ORDER BY ord""", (row["id"],))
        if _row_signature(existing) == signature:
            return {"ok": True, "created": False, "pick_id": row["id"],
                    "name": checked.american_display()}

    sport_rows = db.query(db_path, "SELECT id FROM sports WHERE code=?", (sport,))
    if not sport_rows:
        raise ValueError(f"unsupported sport: {sport}")
    sport_id = sport_rows[0]["id"]
    confidence = max(1, min(10, round(checked.combined_model_prob * 10)))
    created = now.isoformat()
    conn = db.connect(db_path)
    try:
        cur = conn.execute("""INSERT INTO picks
            (sport_id,nature,source,status,edge,confidence_model,
             confidence_final,units_model,units_final,reason,created_utc,approved_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sport_id, "parlay", "model", "approved", checked.edge,
             confidence, confidence, 1.0, 1.0,
             "Tracked from Model Picks", created, created))
        pick_id = cur.lastrowid
        for order, leg in enumerate(checked.legs):
            implied = american_to_prob(leg.odds_american)
            conn.execute("""INSERT INTO pick_legs
                (pick_id,ord,game_id,market,team,line,odds_american,
                 model_prob,implied_prob,edge,support_stat,reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pick_id, order, int(leg.game_id), leg.market, leg.team,
                 leg.line, int(leg.odds_american), leg.model_prob, implied,
                 leg.model_prob - implied, leg.support_stat, leg.reason))
        _insert_snapshot(conn, pick_id, checked.combined_odds,
                         checked.combined_model_prob, created)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "created": True, "pick_id": pick_id,
            "name": checked.american_display()}


def snapshot_open_parlays(sport: str | None = None,
                          db_path=db.DEFAULT_DB) -> int:
    """Reprice tracked parlays using exact current quotes for every leg."""
    from .edge import decimal_from_american

    sql = """SELECT p.id FROM picks p JOIN sports s ON s.id=p.sport_id
             WHERE p.nature='parlay'
               AND p.status IN ('approved','edited','scheduled')"""
    params: tuple = ()
    if sport:
        sql += " AND s.code=?"
        params = (sport,)
    written = 0
    for pick in db.query(db_path, sql, params):
        legs = db.query(db_path, """
            SELECT game_id,market,team,line,model_prob FROM pick_legs
            WHERE pick_id=? ORDER BY ord""", (pick["id"],))
        decimal_odds = 1.0
        model_prob = 1.0
        complete = bool(legs)
        for leg in legs:
            line_sql = "line IS NULL" if leg["line"] is None else "ABS(line-?) < 0.0001"
            quote_params = [leg["game_id"], leg["market"], leg["team"]]
            if leg["line"] is not None:
                quote_params.append(leg["line"])
            quotes = db.query(db_path, f"""
                SELECT price FROM odds
                WHERE game_id=? AND market=? AND LOWER(side)=LOWER(?)
                  AND {line_sql}
                ORDER BY price DESC,captured_utc DESC LIMIT 1""", tuple(quote_params))
            if not quotes:
                complete = False
                break
            decimal_odds *= decimal_from_american(quotes[0]["price"])
            model_prob *= float(leg["model_prob"] or 0.0)
        if not complete:
            continue
        conn = db.connect(db_path)
        try:
            _insert_snapshot(conn, pick["id"], decimal_odds, model_prob,
                             dt.datetime.now(dt.timezone.utc).isoformat())
            conn.commit()
            written += 1
        finally:
            conn.close()
    return written


def tracked_parlays(sport: str | None = None,
                     db_path=db.DEFAULT_DB) -> list[dict]:
    """Return watched parlays with all legs and their complete price series."""
    sql = """SELECT p.id,p.status,p.edge,p.confidence_final,p.units_final,
                    p.created_utc,s.code AS sport,r.result,r.units_won
             FROM picks p JOIN sports s ON s.id=p.sport_id
             LEFT JOIN results r ON r.pick_id=p.id
             WHERE p.nature='parlay'"""
    params: tuple = ()
    if sport:
        sql += " AND s.code=?"
        params = (sport,)
    sql += " ORDER BY p.created_utc DESC,p.id DESC"
    out = []
    for row in db.query(db_path, sql, params):
        item = dict(row)
        item["legs"] = [dict(leg) for leg in db.query(db_path, """
            SELECT l.ord,l.game_id,l.market,l.team,l.line,l.odds_american,
                   l.model_prob,l.implied_prob,l.edge,
                   at.abbrev AS away,ht.abbrev AS home,g.start_utc,g.status AS game_status
            FROM pick_legs l JOIN games g ON g.id=l.game_id
            JOIN teams at ON at.id=g.away_team JOIN teams ht ON ht.id=g.home_team
            WHERE l.pick_id=? ORDER BY l.ord""", (row["id"],))]
        item["snapshots"] = [dict(snapshot) for snapshot in db.query(db_path, """
            SELECT captured_utc,decimal_odds,implied_prob,model_prob,expected_value
            FROM pick_snapshots WHERE pick_id=? ORDER BY captured_utc,id""",
            (row["id"],))]
        out.append(item)
    return out


def _insert_snapshot(conn, pick_id: int, decimal_odds: float,
                     model_prob: float, captured_utc: str) -> None:
    implied = 1.0 / decimal_odds
    conn.execute("""INSERT INTO pick_snapshots
        (pick_id,captured_utc,decimal_odds,implied_prob,model_prob,expected_value)
        VALUES (?,?,?,?,?,?)""",
        (pick_id, captured_utc, round(decimal_odds, 6), round(implied, 6),
         round(model_prob, 6), round(model_prob * decimal_odds - 1.0, 6)))


def _leg_signature(legs) -> tuple:
    return tuple(sorted((int(leg.game_id), leg.market.lower(), leg.team.lower(),
                         None if leg.line is None else round(float(leg.line), 4))
                        for leg in legs))


def _row_signature(rows) -> tuple:
    return tuple(sorted((int(row["game_id"]), row["market"].lower(),
                         row["team"].lower(),
                         None if row["line"] is None else round(float(row["line"]), 4))
                        for row in rows))
