"""The hit rate grid: every line on the board against the player's own log.

Registered on the app with:

    from .hitrates_bp import bp as hitrates_bp
    app.register_blueprint(hitrates_bp)

The page never waits on ESPN and never spends Odds API credits. It reads the
same background player board as the players page, starts it if it is missing
or stale, and uses only prices already cached on disk.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

from flask import Blueprint, current_app, render_template, request

from ..data import sports as sport_registry
from ..picker import hitrates
from ..picker import players
from .gating import requires

bp = Blueprint("hitrates", __name__)

# Matches the players page, so both read the same cached board.
BOARD_DAYS = 7


def _sport(value: str | None) -> str:
    enabled = sport_registry.enabled_codes()
    return value if value in enabled else enabled[0]


@bp.get("/props")
@requires("hit_rates")
def props():
    path = current_app.config["DB_PATH"]
    sport = _sport(request.args.get("sport"))
    day = dt.date.today()

    entry = players.CACHE.get(sport, BOARD_DAYS, day, None)
    if not entry or not players.CACHE.is_fresh(entry):
        entry = players.CACHE.start(sport, BOARD_DAYS, day, path, team=None,
                                    max_games=players.DEFAULT_MAX_GAMES)
    board = entry.get("board") if entry else None

    history_entry = hitrates.HISTORY.get_or_start(board) if board is not None else None
    history = history_entry.get("history") if history_entry else None
    rows = hitrates.build_rows(board, history)

    stat_counts = Counter(row.stat_label for row in rows)
    teams = sorted({row.team for row in rows})
    board_building = bool(entry and entry.get("status") == "building")
    history_building = bool(history_entry and history_entry.get("status") == "building")

    return render_template(
        "props.html", active="props",
        sport=sport_registry.get(sport), entry=entry, board=board, rows=rows,
        stats=[label for label, _ in stat_counts.most_common()],
        teams=teams, board_building=board_building,
        history_building=history_building,
        history_progress=(history_entry or {}).get("progress", 0.0),
        windows=hitrates.WINDOWS)
