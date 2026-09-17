"""Episode system: per-type counters, draft-overwrite semantics, statuses.

Rules:
  - each episode type has its own counter; numbers never repeat or skip
  - a new episode always gets last_number+1
  - regenerating an existing DRAFT for the same type+date does NOT consume a
    new number - it overwrites the previous draft in place
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..db import database as db

DRAFT_TRANSITIONS = {
    "draft": {"generated", "scheduled", "posted", "draft"},
    "generated": {"generated", "scheduled", "posted", "draft"},
    "scheduled": {"posted", "draft"},
    "posted": set(),
}


@dataclass
class Episode:
    id: int
    number: int
    ep_type: str
    date: str
    status: str
    slate: str | None = None
    sport: str | None = None
    template_key: str | None = None
    hook_key: str | None = None
    caption_text: str | None = None
    alt_text: str | None = None
    hashtags: str | None = None
    output_dir: str | None = None
    created_utc: str = ""
    linked_pick_ids: list[int] = field(default_factory=list)


def next_number(ep_type: str, db_path=db.DEFAULT_DB) -> int:
    """Return next episode number for type (last_number + 1)."""
    rows = db.query(db_path, "SELECT last_number FROM counters WHERE ep_type=?", (ep_type,))
    last = rows[0]["last_number"] if rows else 0
    return last + 1


def _bump_counter(ep_type: str, number: int, db_path=db.DEFAULT_DB) -> None:
    db.execute(db_path, """INSERT INTO counters (ep_type, last_number) VALUES (?,?)
                           ON CONFLICT(ep_type) DO UPDATE SET last_number=excluded.last_number""",
               (ep_type, number))


def create_or_regenerate(ep_type: str, date: str | None = None, slate: str | None = None,
                         sport: str | None = None, db_path=db.DEFAULT_DB) -> Episode:
    """Create a new draft, or reuse the draft already open for the same
    type + date + slate (overwrite instead of renumber)."""
    date = date or dt.date.today().isoformat()
    conn = db.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("""
            SELECT * FROM episodes
            WHERE ep_type=? AND date=? AND status='draft'
              AND (? IS NULL OR slate=? OR slate IS NULL)
            ORDER BY CASE WHEN slate=? THEN 0 ELSE 1 END, id DESC LIMIT 1
        """, (ep_type, date, slate, slate, slate)).fetchone()
        if existing:
            conn.commit()
            return _to_episode(existing, db_path)
        row = conn.execute(
            "SELECT last_number FROM counters WHERE ep_type=?", (ep_type,)
        ).fetchone()
        number = (row["last_number"] if row else 0) + 1
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO episodes (number, ep_type, date, slate, sport, status, created_utc)
               VALUES (?,?,?,?,?, 'draft', ?)""",
            (number, ep_type, date, slate, sport, now),
        )
        ep_id = cur.lastrowid
        conn.execute("""INSERT INTO counters (ep_type, last_number) VALUES (?,?)
                        ON CONFLICT(ep_type) DO UPDATE SET last_number=excluded.last_number""",
                     (ep_type, number))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return Episode(id=ep_id, number=number, ep_type=ep_type, date=date,
                   status="draft", slate=slate, sport=sport, created_utc=now)


def _to_episode(row, db_path=db.DEFAULT_DB) -> Episode:
    pick_rows = db.query(db_path, """
        SELECT pick_id FROM episode_picks WHERE episode_id=?
        UNION SELECT id AS pick_id FROM picks WHERE episode_id=?
        ORDER BY pick_id""", (row["id"], row["id"])) if row["id"] else []
    return Episode(
        id=row["id"], number=row["number"], ep_type=row["ep_type"], date=row["date"],
        status=row["status"], slate=row["slate"], sport=row["sport"],
        template_key=row["template_key"], hook_key=row["hook_key"],
        caption_text=row["caption_text"], alt_text=row["alt_text"],
        hashtags=row["hashtags"], output_dir=row["output_dir"], created_utc=row["created_utc"],
        linked_pick_ids=[r["pick_id"] for r in pick_rows],
    )


def get_episode(episode_id: int, db_path=db.DEFAULT_DB) -> Episode | None:
    rows = db.query(db_path, "SELECT * FROM episodes WHERE id=?", (episode_id,))
    return _to_episode(rows[0], db_path) if rows else None


def list_episodes(ep_type: str | None = None, sport: str | None = None,
                  date_from: str | None = None, db_path=db.DEFAULT_DB) -> list[Episode]:
    sql = "SELECT * FROM episodes WHERE 1=1"
    params = []
    if ep_type:
        sql += " AND ep_type=?"
        params.append(ep_type)
    if sport:
        sql += " AND sport=?"
        params.append(sport)
    if date_from:
        sql += " AND date>=?"
        params.append(date_from)
    sql += " ORDER BY date DESC, number DESC"
    return [_to_episode(r, db_path) for r in db.query(db_path, sql, tuple(params))]


def set_status(episode_id: int, status: str, db_path=db.DEFAULT_DB) -> None:
    row = db.query(db_path, "SELECT status FROM episodes WHERE id=?", (episode_id,))
    if not row:
        raise ValueError(f"no episode {episode_id}")
    current = row[0]["status"]
    allowed = DRAFT_TRANSITIONS.get(current, set())
    if status not in allowed and current != status:
        raise ValueError(f"cannot move episode from {current} -> {status}")
    db.execute(db_path, "UPDATE episodes SET status=? WHERE id=?", (status, episode_id))


def link_picks(episode_id: int, pick_ids: list[int], db_path=db.DEFAULT_DB) -> None:
    db.execute(db_path, "DELETE FROM episode_picks WHERE episode_id=?", (episode_id,))
    for pid in pick_ids:
        if not db.query(db_path, "SELECT id FROM picks WHERE id=?", (pid,)):
            raise ValueError(f"no pick {pid}")
        db.execute(db_path, "INSERT OR IGNORE INTO episode_picks (episode_id, pick_id) VALUES (?,?)",
                   (episode_id, pid))
        db.execute(db_path, "UPDATE picks SET episode_id=? WHERE id=? AND status NOT IN ('graded','rejected')",
                   (episode_id, pid))


def find_draft(ep_type: str, date: str, db_path=db.DEFAULT_DB) -> Episode | None:
    rows = db.query(db_path,
                    "SELECT * FROM episodes WHERE ep_type=? AND date=? AND status='draft' ORDER BY id DESC LIMIT 1",
                    (ep_type, date))
    return _to_episode(rows[0], db_path) if rows else None
