"""Small in-process scheduler for the local dashboard.

It refreshes schedules and cached prices, commits only picks that pass the
automatic rules, snapshots open parlays, and grades completed picks.  The
worker is a daemon thread, so it starts and stops with the local website.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

from ..data import sports as sport_registry
from ..db import database as db
from . import actions, autopick
from .engine import Picker
from .grader import grade_all

_lock = threading.Lock()
_started = False
STATUS_PATH = db.ROOT / "data" / "automation_status.json"


def _schedule_dates(sport: str, today: dt.date, days: int,
                    db_path=db.DEFAULT_DB) -> list[dt.date]:
    """Dates that need refreshing for picks plus the upcoming slate.

    A forward-only refresh strands yesterday's picks with an ``in_progress``
    game forever after midnight.  Revisit every past/current game date that
    still has an ungraded pick so ESPN can supply the final score.
    """
    dates = {today + dt.timedelta(days=offset) for offset in range(days)}
    rows = db.query(db_path, """
        SELECT DISTINCT COALESCE(g.game_date, date(g.start_utc)) AS game_day
        FROM picks p
        JOIN pick_legs l ON l.pick_id=p.id
        JOIN games g ON g.id=l.game_id
        JOIN sports s ON s.id=g.sport_id
        WHERE s.code=? AND p.status != 'graded'
          AND g.status != 'final'
          AND date(COALESCE(g.game_date, g.start_utc)) <= date(?)
    """, (sport, today.isoformat()))
    # Games left "in progress" while the app was closed. Live polling gives up
    # on a game 12 hours after it starts, so only a resync can close them out.
    rows += db.query(db_path, """
        SELECT DISTINCT COALESCE(g.game_date, date(g.start_utc)) AS game_day
        FROM games g JOIN sports s ON s.id=g.sport_id
        WHERE s.code=? AND g.status='in_progress'
          AND date(COALESCE(g.game_date, g.start_utc)) < date(?)
    """, (sport, today.isoformat()))
    for row in rows:
        if row["game_day"]:
            dates.add(dt.date.fromisoformat(row["game_day"]))
    return sorted(dates)


def run_cycle(db_path=db.DEFAULT_DB, config: dict | None = None) -> dict:
    """Run one complete maintenance cycle; safe to call repeatedly."""
    cfg = config or db.get_config()
    settings = cfg.get("automation", {})
    days = max(1, min(14, int(settings.get("days", 7))))
    today = dt.date.today()
    result = {"started_utc": db.utc_now(), "sports": {}, "graded": 0,
              "error": None}
    if not _lock.acquire(blocking=False):
        return {**result, "error": "a refresh is already running"}
    errors = []
    try:
        # One league failing must not stop the leagues after it, or grading.
        for code in sport_registry.enabled_codes(cfg):
            try:
                picker = Picker(code, db_path=db_path)
                games = 0
                for day in _schedule_dates(code, today, days, db_path):
                    # ESPN drops the odd day with a 503; skip it until next hour.
                    try:
                        games += picker.sync_games(day)
                    except Exception as exc:
                        errors.append(f"{code} {day}: {exc}")
                odds = picker.sync_odds(day=today)
                selected = autopick.run(code, days=days, start=today,
                                        db_path=db_path, config=cfg)
                snapshots = actions.snapshot_open_parlays(code, db_path)
                result["sports"][code] = {
                    "games": games, "odds_source": odds["source"],
                    "odds_games": odds["games"], "picks_added": selected["added"],
                    "parlay_snapshots": snapshots,
                }
            except Exception as exc:
                result["sports"][code] = {"error": str(exc)}
                errors.append(f"{code}: {exc}")
        result["graded"] = len(grade_all(db_path))
    except Exception as exc:
        errors.append(str(exc))
    finally:
        result["error"] = "; ".join(errors) or None
        result["finished_utc"] = db.utc_now()
        _lock.release()
        if Path(db_path).resolve() == Path(db.DEFAULT_DB).resolve():
            temp = STATUS_PATH.with_suffix(".json.tmp")
            temp.write_text(json.dumps(result, indent=2), encoding="utf-8")
            temp.replace(STATUS_PATH)
    return result


def start(db_path=db.DEFAULT_DB) -> threading.Thread | None:
    """Start the single background worker used by the dashboard process."""
    global _started
    config = db.get_config()
    settings = config.get("automation", {})
    if _started or not settings.get("enabled", True):
        return None
    _started = True
    interval = max(15, int(settings.get("refresh_minutes", 60))) * 60

    def worker() -> None:
        while True:
            run_cycle(db_path)
            threading.Event().wait(interval)

    thread = threading.Thread(target=worker, name="edgeboard-refresh", daemon=True)
    thread.start()
    return thread
