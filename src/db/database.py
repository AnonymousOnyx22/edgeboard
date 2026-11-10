"""SQLite access layer. One connection per operation (simple, thread-safe enough)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
# A hosted copy keeps its database on a persistent disk, named by EDGEBOARD_DB.
DEFAULT_DB = Path(os.getenv("EDGEBOARD_DB") or ROOT / "data" / "db.sqlite")


def get_config() -> dict:
    with open(ROOT / "config" / "settings.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_brand() -> dict:
    with open(ROOT / "config" / "brand.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # The hourly cycle, the live score refresh and page loads all write at
    # once; the timeout makes a writer wait its turn instead of failing with
    # "database is locked". init_db switches the file to WAL as well.
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path = DEFAULT_DB) -> None:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    conn = connect(db_path)
    try:
        # WAL lets page reads run beside the cycle's writes. The setting sticks
        # to the file, so if another process holds it open now, a later start
        # will switch it.
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    conn.executescript(schema)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
    if "game_date" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN game_date TEXT")
    # Player props name an athlete; game lines leave both columns NULL.
    leg_columns = {row[1] for row in conn.execute("PRAGMA table_info(pick_legs)")}
    for column in ("athlete_id", "player", "bookmaker"):
        if column not in leg_columns:
            conn.execute(f"ALTER TABLE pick_legs ADD COLUMN {column} TEXT")
    # Where the game is played, for the fixture cards.
    for column in ("venue", "venue_city", "status_detail", "live_utc"):
        if column not in columns:
            conn.execute(f"ALTER TABLE games ADD COLUMN {column} TEXT")
    # Repair rows written by the original ESPN adapter. It stored raw states
    # and assumed competitor order 0/1 meant away/home (ESPN uses home/away).
    conn.execute("""UPDATE games SET away_score=home_score, home_score=away_score
                    WHERE status IN ('in','post')""")
    conn.execute("""UPDATE games SET status=CASE status
                        WHEN 'pre' THEN 'scheduled'
                        WHEN 'in' THEN 'in_progress'
                        WHEN 'post' THEN 'final'
                        ELSE status END
                    WHERE status IN ('pre','in','post')""")
    eastern = ZoneInfo("America/New_York")
    for game_id, start_utc in conn.execute(
            "SELECT id, start_utc FROM games WHERE game_date IS NULL"):
        try:
            local_date = datetime.datetime.fromisoformat(
                start_utc.replace("Z", "+00:00")
            ).astimezone(eastern).date().isoformat()
        except (AttributeError, TypeError, ValueError):
            local_date = None
        conn.execute("UPDATE games SET game_date=? WHERE id=?", (local_date, game_id))
    conn.commit()
    conn.close()


def query(db_path: str | Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def execute(db_path: str | Path, sql: str, params: tuple = ()) -> int:
    conn = connect(db_path)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


import datetime  # noqa: E402


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


if __name__ == "__main__":
    init_db()
    print("DB initialized at", DEFAULT_DB)
