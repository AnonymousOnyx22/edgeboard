"""Live game state for picks that have not been graded yet.

A pick sits ungraded from the moment its game starts until the final score
lands. That window is exactly when someone wants to look at it, and until now
the pages showed nothing at all - a bet on a game in the fourth quarter read
the same as one that had not kicked off.

ESPN's site scoreboard, which returns a whole day in one request, answers 403
from this machine (see data/espn_client), so the state is resolved per game
off the core API: about five requests a game, a third of a second each. That
is cheap enough for the handful of games actually in progress and far too
expensive for a whole schedule, so only games that have started and have not
finished are ever refreshed, and each one at most once per `MIN_INTERVAL`.

Nothing here grades anything. A final score is written to the games table and
the grader picks it up on its own terms; this module only reports.
"""
from __future__ import annotations

import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor

from ..data.espn_client import CORE_BASE, SPORT_LEAGUES, EspnClient
from ..db import database as db

# A game is not refreshed again inside this window. Scores move, but not fast
# enough to be worth five requests a second.
MIN_INTERVAL = 45
# Games refreshed at once. Each one is a handful of small reads, so this is
# bounded by ESPN's latency rather than by anything local.
WORKERS = 8
# How long after the scheduled start a game is still considered worth polling,
# in case ESPN never flips it to final and it would otherwise poll forever.
STALE_AFTER_HOURS = 12
# A game an open pick rides on is still read after that, only less often, so
# a laptop that slept through the game settles it on the first page load.
STALE_INTERVAL = 600


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def due_games(db_path=db.DEFAULT_DB, sport: str | None = None,
              now: dt.datetime | None = None) -> list[dict]:
    """Games that have started, are not final, and are not freshly read."""
    now = now or _utcnow()
    sql = """
        SELECT g.id, g.external_id, g.status, g.start_utc, g.live_utc,
               s.code AS sport,
               EXISTS (SELECT 1 FROM pick_legs l JOIN picks p ON p.id = l.pick_id
                       WHERE l.game_id = g.id AND p.status != 'graded') AS has_pick
        FROM games g JOIN sports s ON s.id = g.sport_id
        WHERE g.status != 'final' AND g.external_id IS NOT NULL"""
    params: list = []
    if sport:
        sql += " AND s.code = ?"
        params.append(sport)
    rows = [dict(row) for row in db.query(db_path, sql, tuple(params))]

    out = []
    for row in rows:
        start = _parse(row["start_utc"])
        if start is None or start > now:
            continue                       # has not started
        stale = (now - start).total_seconds() > STALE_AFTER_HOURS * 3600
        if stale and not row["has_pick"]:
            continue                       # long over; ESPN never closed it
        last = _parse(row["live_utc"])
        wait = STALE_INTERVAL if stale else MIN_INTERVAL
        if last and (now - last).total_seconds() < wait:
            continue                       # read a moment ago
        out.append(row)
    return out


def read_game(row: dict, client: EspnClient | None = None) -> dict | None:
    """One game's current state from ESPN, or None if it cannot be read."""
    client = client or EspnClient()
    slug, league = SPORT_LEAGUES[row["sport"]]
    try:
        event = client._get_json(
            f"{CORE_BASE}/{slug}/leagues/{league}/events/{row['external_id']}",
            cache_name=None)
        comp = client.competition(event)
        status = client.status(comp)
    except Exception:
        return None

    away = home = None
    scores = client.competitor_scores(comp)
    for competitor in comp.get("competitors", []):
        value = scores.get(competitor.get("order", 0))
        if value is None:
            continue
        if competitor.get("homeAway") == "away":
            away = int(value)
        elif competitor.get("homeAway") == "home":
            home = int(value)

    return {
        "game_id": row["id"],
        "state": status.get("state") or row["status"],
        "detail": status.get("detail") or "",
        "away_score": away,
        "home_score": home,
    }


def refresh(db_path=db.DEFAULT_DB, sport: str | None = None,
            limit: int = 30) -> dict:
    """Read every game that is due and write what came back.

    Safe to call on a page load: it does nothing when no game is in progress,
    and never re-reads a game inside `MIN_INTERVAL`.
    """
    due = due_games(db_path, sport)[:limit]
    if not due:
        return {"checked": 0, "updated": 0, "live": 0, "finished": 0}

    client = EspnClient()
    workers = min(WORKERS, len(due))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="live") as pool:
        results = [r for r in pool.map(lambda row: read_game(row, client), due)
                   if r is not None]

    now = _utcnow().isoformat()
    updated = live = finished = 0
    conn = db.connect(db_path)
    try:
        for result in results:
            conn.execute("""
                UPDATE games SET status=?, status_detail=?, live_utc=?,
                    away_score=COALESCE(?, away_score),
                    home_score=COALESCE(?, home_score)
                WHERE id=?""",
                (result["state"], result["detail"], now,
                 result["away_score"], result["home_score"], result["game_id"]))
            updated += 1
            if result["state"] == "in_progress":
                live += 1
            elif result["state"] == "final":
                finished += 1
        conn.commit()
    finally:
        conn.close()

    if updated:
        # Scores and statuses feed the board, so its cache has to let go.
        from . import board as board_mod
        board_mod.clear_board_cache()

    graded = 0
    if finished:
        # A final score is the only thing grading was ever waiting for, so it
        # happens here rather than on the next hourly cycle. The scheduler
        # still grades on its own account; this only shortens the wait, and
        # grade_all skips anything already settled.
        try:
            from .grader import grade_all
            graded = len(grade_all(db_path))
        except Exception:
            graded = 0

    return {"checked": len(due), "updated": updated, "live": live,
            "finished": finished, "graded": graded}


# ------------------------------------------------------------- in the background
_REFRESH_LOCK = threading.Lock()
_REFRESHING: set[str] = set()
_LAST: dict[str, dict] = {}


def refresh_async(db_path=db.DEFAULT_DB, sport: str | None = None) -> bool:
    """Start a refresh off the request thread. True if one was started.

    A page must never wait on ESPN. Reading a dozen live games takes seconds,
    so the page renders whatever the database already holds and the next load
    picks up the newer state. One refresh per sport runs at a time.
    """
    key = f"{db_path}:{sport or '*'}"
    with _REFRESH_LOCK:
        if key in _REFRESHING:
            return False
        if not due_games(db_path, sport):
            return False
        _REFRESHING.add(key)

    def run() -> None:
        try:
            _LAST[key] = refresh(db_path, sport)
        except Exception as exc:
            _LAST[key] = {"error": str(exc)}
        finally:
            with _REFRESH_LOCK:
                _REFRESHING.discard(key)

    threading.Thread(target=run, name="live-refresh", daemon=True).start()
    return True


def last_result(db_path=db.DEFAULT_DB, sport: str | None = None) -> dict:
    """What the most recent background refresh reported."""
    return _LAST.get(f"{db_path}:{sport or '*'}", {})


def is_stale(start_utc, now: dt.datetime | None = None) -> bool:
    """Started so long ago that "in progress" can only be an unclosed record."""
    start = _parse(start_utc)
    now = now or _utcnow()
    return bool(start) and (now - start).total_seconds() > STALE_AFTER_HOURS * 3600


def any_live(db_path=db.DEFAULT_DB, sport: str | None = None) -> bool:
    """Is at least one game in progress? Drives the pages' auto-refresh."""
    sql = """SELECT g.start_utc FROM games g JOIN sports s ON s.id = g.sport_id
             WHERE g.status = 'in_progress'"""
    params: list = []
    if sport:
        sql += " AND s.code = ?"
        params.append(sport)
    return any(not is_stale(row["start_utc"])
               for row in db.query(db_path, sql, tuple(params)))


# ---------------------------------------------------------------- rendering
def describe(row) -> dict | None:
    """How one pick's game should read, or None when there is nothing to say.

    `row` is any mapping carrying game_status, status_detail, the two scores
    and the two club abbreviations - which is what the pick queries already
    select.
    """
    get = row.get if hasattr(row, "get") else (
        lambda k, d=None: row[k] if k in row.keys() else d)
    state = get("game_status") or get("status")
    if state not in ("in_progress", "final"):
        return None
    if state == "in_progress" and is_stale(get("start_utc")):
        return None

    away, home = get("away_score"), get("home_score")
    score = ""
    if away is not None and home is not None:
        score = f"{get('away')} {int(away)} - {int(home)} {get('home')}"

    if state == "final":
        return {"live": False, "label": "Final", "detail": "", "score": score}
    return {"live": True, "label": "Live",
            "detail": get("status_detail") or "", "score": score}
