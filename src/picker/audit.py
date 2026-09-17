"""Integrity checks on the tracked record.

A picks ledger is only worth keeping if every row in it was a real pregame
commitment at a price a book was genuinely offering. Two things can break that:

  live prices     a game already under way is still quoted, and those numbers
                  look like enormous value to a model that does not know the
                  score. A 9-2 blowout shows up as "+3300 on the home side,
                  24% model probability, +21% edge".
  stale models    a pick written before a modelling fix keeps the edge the old
                  model believed at the time.

The first is corrupt data and should be removed. The second is a real bet
recorded with a since-corrected estimate: it is left alone, because deleting
losers a model talked itself into is how a record starts lying.
"""
from __future__ import annotations

import datetime as dt

from ..db import database as db

# A price this long on a pregame side is almost always an in-play quote.
EXTREME_PRICE = 1000
# Nothing survives a sane pregame model with this much edge.
IMPLAUSIBLE_EDGE = 0.20


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_invalid_picks(db_path=db.DEFAULT_DB) -> list[dict]:
    """Picks that were priced off a game already in progress."""
    rows = db.query(db_path, """
        SELECT p.id, p.status, p.source, p.created_utc, p.edge,
               l.market, l.team, l.line, l.odds_american,
               g.start_utc, g.status AS game_status,
               at.abbrev AS away, ht.abbrev AS home, s.code AS sport
        FROM picks p
        JOIN pick_legs l ON l.pick_id = p.id
        JOIN games g ON g.id = l.game_id
        JOIN sports s ON s.id = p.sport_id
        JOIN teams at ON at.id = g.away_team
        JOIN teams ht ON ht.id = g.home_team
        ORDER BY p.id""")

    flagged = []
    for row in rows:
        start = _parse(row["start_utc"])
        created = _parse(row["created_utc"])
        reasons = []

        if start and created and created >= start:
            late = (created - start).total_seconds() / 60
            reasons.append(f"committed {late:.0f} min after kickoff")
        if abs(row["odds_american"] or 0) >= EXTREME_PRICE:
            reasons.append(f"price {row['odds_american']:+d} is an in-play number")

        if reasons:
            flagged.append({
                "id": row["id"],
                "sport": row["sport"],
                "game": f"{row['away']} @ {row['home']}",
                "pick": f"{row['team']} {row['market']}"
                        + (f" {row['line']:+g}" if row["line"] is not None else ""),
                "odds": row["odds_american"],
                "edge": row["edge"],
                "status": row["status"],
                "reasons": reasons,
            })
    return flagged


def find_suspect_edges(db_path=db.DEFAULT_DB) -> list[dict]:
    """Pregame picks whose recorded edge is too large to believe.

    These are reported, not removed: the bet was real, the estimate was not.
    """
    rows = db.query(db_path, """
        SELECT p.id, p.edge, p.created_utc, l.market, l.team, l.line, l.odds_american,
               g.start_utc, at.abbrev AS away, ht.abbrev AS home
        FROM picks p
        JOIN pick_legs l ON l.pick_id = p.id
        JOIN games g ON g.id = l.game_id
        JOIN teams at ON at.id = g.away_team
        JOIN teams ht ON ht.id = g.home_team
        WHERE p.edge >= ?
        ORDER BY p.edge DESC""", (IMPLAUSIBLE_EDGE,))
    out = []
    for row in rows:
        start, created = _parse(row["start_utc"]), _parse(row["created_utc"])
        if start and created and created >= start:
            continue      # already covered by find_invalid_picks
        out.append({
            "id": row["id"],
            "game": f"{row['away']} @ {row['home']}",
            "pick": f"{row['team']} {row['market']}"
                    + (f" {row['line']:+g}" if row["line"] is not None else ""),
            "odds": row["odds_american"],
            "edge": row["edge"],
        })
    return out


def find_live_odds(db_path=db.DEFAULT_DB) -> int:
    """How many stored prices were captured after their game started."""
    rows = db.query(db_path, """
        SELECT COUNT(*) AS n FROM odds o JOIN games g ON g.id = o.game_id
        WHERE o.captured_utc >= replace(g.start_utc, 'Z', '+00:00')""")
    return int(rows[0]["n"]) if rows else 0


def purge_live_odds(db_path=db.DEFAULT_DB) -> int:
    """Delete prices captured after kickoff. They are never usable pregame."""
    conn = db.connect(db_path)
    try:
        cursor = conn.execute("""
            DELETE FROM odds WHERE id IN (
                SELECT o.id FROM odds o JOIN games g ON g.id = o.game_id
                WHERE o.captured_utc >= replace(g.start_utc, 'Z', '+00:00'))""")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def delete_picks(pick_ids: list[int], db_path=db.DEFAULT_DB) -> int:
    """Remove picks and everything hanging off them."""
    if not pick_ids:
        return 0
    marks = ",".join("?" * len(pick_ids))
    conn = db.connect(db_path)
    try:
        conn.execute(f"DELETE FROM results WHERE pick_id IN ({marks})", pick_ids)
        conn.execute(f"DELETE FROM pick_legs WHERE pick_id IN ({marks})", pick_ids)
        cursor = conn.execute(f"DELETE FROM picks WHERE id IN ({marks})", pick_ids)
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def report(db_path=db.DEFAULT_DB) -> dict:
    return {
        "invalid_picks": find_invalid_picks(db_path),
        "suspect_edges": find_suspect_edges(db_path),
        "live_odds_rows": find_live_odds(db_path),
    }
