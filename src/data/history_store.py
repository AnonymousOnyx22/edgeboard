"""Local store of completed games and their closing lines.

The picker's whole claim is that a bet has actually cashed before against this
opponent. That needs real finished games with the number the book hung at the
time, kept somewhere durable rather than refetched on every page load.

  backfill(sport, seasons)   pull completed games into the game_history table
  meetings(a, b, sport)      every prior meeting between two clubs
  history_frame(sport)       the whole store as a DataFrame, for the Elo trainer

Sources, by sport:

  NFL   nflverse schedules, which carry `spread_line` (home-relative, favourite
        positive) and `total_line` for every completed game back to 1999. That
        is what makes "would this spread have covered?" answerable.
  MLB   ESPN team schedules. Scores only - ESPN's schedule feed carries no
        closing line, so spread_line and total_line stay NULL and anything that
        depends on them reports "not available" rather than guessing.
  NHL   as MLB.

Abbreviations are normalised to ESPN's spelling on the way in, so a pairing
query never has to know which source a row came from.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from ..db import database as db
from . import sports as sport_registry

# nflverse and ESPN disagree on exactly two NFL clubs.
NFLVERSE_TO_ESPN = {"LA": "LAR", "WAS": "WSH"}

DEFAULT_SEASONS = 6


class HistoryUnavailableError(RuntimeError):
    """Raised when no completed-game history could be fetched or built.

    The picker's edge and confidence both come from real prior games. When the
    store is empty and every source failed, silently modelling on nothing would
    price every fixture from a default 1500-rating Elo as if it were signal.
    Callers who need to bet must see and propagate this, not swallow it.
    """


def normalise(abbrev: str) -> str:
    a = (abbrev or "").strip().upper()
    return NFLVERSE_TO_ESPN.get(a, a)


# ------------------------------------------------------------------ backfill
def backfill(sport: str = "nfl", seasons: int = DEFAULT_SEASONS,
             db_path=db.DEFAULT_DB, progress=None) -> dict:
    """Load `seasons` seasons of completed games into game_history.

    Re-running is safe: rows are keyed on (sport, game_key) and upserted, so a
    season part-way through simply gains its new results.
    """
    config = sport_registry.get(sport)
    this_year = dt.date.today().year
    years = list(range(this_year - seasons + 1, this_year + 1))

    def step(fraction, message):
        if progress:
            progress(fraction, message)

    step(0.05, f"Fetching {config.name} {years[0]}-{years[-1]}...")
    if config.history_source == "nflverse":
        rows = _nflverse_rows(years)
    else:
        rows = _espn_rows(config.code, years, step)

    if not rows:
        raise HistoryUnavailableError(
            f"No {config.name} games fetched for {years[0]}-{years[-1]}: the "
            "source returned nothing (offline, or the feed is unreachable).")

    step(0.9, f"Writing {len(rows)} games...")
    written = _write(rows, db_path)
    step(1.0, "Done.")
    return {"sport": config.code, "seasons": years,
            "fetched": len(rows), "written": written}


def _nflverse_rows(years: list[int]) -> list[dict]:
    from . import stats_nfl
    frame = stats_nfl.schedule_dataframe(seasons=years)
    if frame is None or frame.empty:
        return []
    frame = frame.dropna(subset=["home_score", "away_score"])
    rows = []
    for row in frame.itertuples(index=False):
        rows.append({
            "sport": "nfl",
            "game_key": str(getattr(row, "game_id", "") or
                            f"{getattr(row, 'gameday', '')}-{row.home_team}-{row.away_team}"),
            "season": _int(getattr(row, "season", None)),
            "week": _int(getattr(row, "week", None)),
            "game_date": str(getattr(row, "gameday", ""))[:10],
            "home_team": normalise(row.home_team),
            "away_team": normalise(row.away_team),
            "home_score": float(row.home_score),
            "away_score": float(row.away_score),
            "spread_line": _float(getattr(row, "spread_line", None)),
            "total_line": _float(getattr(row, "total_line", None)),
            "source": "nflverse",
        })
    return rows


def _espn_rows(sport: str, years: list[int], step) -> list[dict]:
    from .espn_schedule import EspnSchedule
    client = EspnSchedule(sport)
    teams = client.team_ids()
    seen: set[str] = set()
    rows: list[dict] = []
    total = max(1, len(teams) * len(years))
    done = 0
    for year in years:
        for team_id in teams:
            try:
                for row in client.team_schedule(team_id, year):
                    key = row["game_id"]
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    rows.append({
                        "sport": sport,
                        "game_key": key,
                        "season": row["season"],
                        "week": row["week"],
                        "game_date": row["gameday"],
                        "home_team": normalise(row["home_team"]),
                        "away_team": normalise(row["away_team"]),
                        "home_score": row["home_score"],
                        "away_score": row["away_score"],
                        # ESPN's schedule feed carries no closing line.
                        "spread_line": None,
                        "total_line": None,
                        "source": "espn",
                    })
            except Exception:
                pass  # one unreachable team should not abort a whole backfill
            done += 1
            if done % 5 == 0:
                step(0.05 + 0.85 * done / total, f"{year}: {done}/{total} teams")
    return rows


def _write(rows: list[dict], db_path) -> int:
    if not rows:
        return 0
    conn = db.connect(db_path)
    try:
        conn.executemany("""
            INSERT INTO game_history
                (sport, game_key, season, week, game_date, home_team, away_team,
                 home_score, away_score, spread_line, total_line, source)
            VALUES (:sport, :game_key, :season, :week, :game_date, :home_team,
                    :away_team, :home_score, :away_score, :spread_line,
                    :total_line, :source)
            ON CONFLICT(sport, game_key) DO UPDATE SET
                home_score=excluded.home_score,
                away_score=excluded.away_score,
                spread_line=COALESCE(excluded.spread_line, game_history.spread_line),
                total_line=COALESCE(excluded.total_line, game_history.total_line)
        """, rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


# --------------------------------------------------------------------- reads
def coverage(db_path=db.DEFAULT_DB) -> list[dict]:
    """What is actually in the store, per sport. Drives the Settings page."""
    rows = db.query(db_path, """
        SELECT sport, COUNT(*) AS games,
               MIN(season) AS first_season, MAX(season) AS last_season,
               SUM(CASE WHEN spread_line IS NOT NULL THEN 1 ELSE 0 END) AS with_lines
        FROM game_history GROUP BY sport ORDER BY sport""")
    return [dict(r) for r in rows]


def meetings(team_a: str, team_b: str, sport: str = "nfl", limit: int = 12,
             db_path=db.DEFAULT_DB, before: dt.date | str | None = None) -> list[dict]:
    """Every stored meeting between two clubs, newest first."""
    a, b = normalise(team_a), normalise(team_b)
    sql = """
        SELECT * FROM game_history
        WHERE sport=?
          AND ((home_team=? AND away_team=?) OR (home_team=? AND away_team=?))
    """
    params: list = [sport, a, b, b, a]
    if before is not None:
        sql += " AND date(game_date) < date(?)"
        params.append(before.isoformat() if isinstance(before, dt.date) else str(before))
    sql += " ORDER BY game_date DESC LIMIT ?"
    params.append(limit)
    rows = db.query(db_path, sql, tuple(params))
    return [dict(r) for r in rows]


def history_frame(sport: str = "nfl", db_path=db.DEFAULT_DB) -> pd.DataFrame:
    """The store as the schedule frame the Elo trainer expects."""
    rows = db.query(db_path, """
        SELECT season, week, game_date AS gameday, home_team, away_team,
               home_score, away_score, spread_line, total_line
        FROM game_history WHERE sport=? ORDER BY game_date""", (sport,))
    return pd.DataFrame([dict(r) for r in rows],
                        columns=["season", "week", "gameday", "home_team",
                                 "away_team", "home_score", "away_score",
                                 "spread_line", "total_line"])


def is_populated(sport: str = "nfl", db_path=db.DEFAULT_DB) -> bool:
    rows = db.query(db_path, "SELECT COUNT(*) AS n FROM game_history WHERE sport=?", (sport,))
    return bool(rows and rows[0]["n"])


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_history(sport: str = "nfl", db_path=db.DEFAULT_DB,
                 auto_backfill: bool = True, seasons: int = DEFAULT_SEASONS):
    """The history the model should train and reason on.

    Prefers the local store, because that is the deep one the user controls.
    Falls back to a live pull (and seeds the store with it) the first time a
    sport is used, so nothing is ever silently modelled on no history at all.

    Raises :class:`HistoryUnavailableError` when the store is empty and every
    source produced nothing, so a default-1500 Elo is never treated as signal.
    """
    frame = history_frame(sport, db_path)
    if not frame.empty:
        return frame
    if not auto_backfill:
        return frame
    error: Exception | None = None
    try:
        backfill(sport, seasons=seasons, db_path=db_path)
    except Exception as exc:
        error = exc
    frame = history_frame(sport, db_path)
    if not frame.empty:
        return frame
    try:
        # Offline, or the primary source is down. Fall back to whatever cache exists.
        from . import espn_schedule
        fallback = espn_schedule.load_or_build_history(sport)
    except Exception as exc:
        if error is None:
            error = exc
        fallback = None
    if fallback is not None and not getattr(fallback, "empty", True):
        return fallback
    raise HistoryUnavailableError(
        f"No {sport} history could be built: the local store is empty and the "
        "source returned no completed games.") from error


def seasons_stored(sport: str = "nfl", db_path=db.DEFAULT_DB) -> list[int]:
    rows = db.query(db_path, """
        SELECT DISTINCT season FROM game_history
        WHERE sport=? AND season IS NOT NULL ORDER BY season""", (sport,))
    return [int(r["season"]) for r in rows]
