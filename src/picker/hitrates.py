"""Prop hit rates: how often each line on the board has actually come in.

The players board already knows every player on the upcoming games and the
lines the model and the books put on them. This turns that into the grid a
prop bettor scans first: for each line, how many of the player's last 5, 10
and 20 games cleared it, how many this season, and how many against tonight's
opponent.

Two rules keep the counts honest:

  a game where the stat was not recorded is no game at all, not a miss
  (a rest day or a box score ESPN never filled in says nothing about a line)

  head to head only counts prior meetings with this exact opponent

The board keeps only the last 10 games per player, which is enough for L5 and
L10 but not L20 or a season. `HistoryCache` reads the full game logs once per
board build, on a background thread, from the ESPN disk cache the board just
filled; until it lands, L20 and season read as pending rather than as a
number computed from too few games.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from ..data import sports as sport_registry
from . import h2h as h2h_mod
from . import players as players_mod

WINDOWS = (5, 10, 20)
# Hit rates at or above STRONG read green, below WEAK read red.
STRONG = 0.70
WEAK = 0.50
# Fewer games than this and a rate is shown but not graded: 2 of 2 is noise.
GRADE_FLOOR = 3


@dataclass
class Rate:
    hits: int
    games: int

    @property
    def pct(self) -> float | None:
        return self.hits / self.games if self.games else None

    @property
    def grade(self) -> str:
        """strong / middling / weak / thin / none, for the cell colour."""
        if not self.games:
            return "none"
        if self.games < GRADE_FLOOR:
            return "thin"
        pct = self.pct
        if pct >= STRONG:
            return "strong"
        if pct >= WEAK:
            return "middling"
        return "weak"

    @property
    def text(self) -> str:
        return f"{self.hits}/{self.games}" if self.games else ""


# ----------------------------------------------------------------- the maths
def cleared(value: float, line: float, side: str) -> bool:
    """Did one game's number land the bet? Over is at or above the line,
    matching props.count_hits; under is below it."""
    return value < line if side == "under" else value >= line


def recorded(games: list[dict], stat_key: str) -> list[float]:
    """The stat's values, newest first, skipping games that never recorded it."""
    return [float(g[stat_key]) for g in games if g.get(stat_key) is not None]


def rate(values: list[float], line: float, side: str) -> Rate:
    return Rate(sum(1 for v in values if cleared(v, line, side)), len(values))


def window_rate(games: list[dict], stat_key: str, line: float, side: str,
                window: int) -> Rate:
    """The last `window` games that recorded the stat."""
    return rate(recorded(games, stat_key)[:window], line, side)


def season_rate(games: list[dict], stat_key: str, line: float, side: str) -> Rate:
    """Every game this season: the season of the newest game, regular and
    postseason alike."""
    if not games:
        return Rate(0, 0)
    season = games[0].get("season")
    this = [g for g in games if g.get("season") == season]
    return rate(recorded(this, stat_key), line, side)


def h2h_rate(games: list[dict], stat_key: str, line: float, side: str,
             opponent: str) -> Rate:
    """Prior meetings with this exact opponent only."""
    target = h2h_mod.to_espn(opponent) if opponent else ""
    if not target:
        return Rate(0, 0)
    meetings = [g for g in games if (g.get("opponent") or "").upper() == target]
    return rate(recorded(meetings, stat_key), line, side)


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def merge_games(*logs: list[dict]) -> list[dict]:
    """One newest first log from several overlapping ones, deduplicated."""
    seen: set[str] = set()
    out: list[dict] = []
    for log in logs:
        for game in log or []:
            key = str(game.get("event_id") or id(game))
            if key in seen:
                continue
            seen.add(key)
            out.append(game)
    out.sort(key=lambda g: (g.get("date") or ""), reverse=True)
    return out


# ------------------------------------------------------------------- the grid
@dataclass
class HitRow:
    sport: str
    athlete_id: str
    player: str
    position: str
    team: str
    opponent: str
    at_home: bool
    kickoff: str
    game_id: int
    injury_status: str
    stat_key: str
    stat_label: str
    side: str
    line: float
    priced: bool
    odds: int | None
    bookmaker: str
    probability: float
    edge: float | None
    l5: Rate
    l10: Rate
    l20: Rate | None        # None until the full game log has been read
    season: Rate | None
    h2h: Rate
    avg10: float | None
    games_logged: int

    @property
    def bet(self) -> str:
        """The bet without the player's name: 'Over 24.5 Points'."""
        return f"{self.side.capitalize()} {self.line:g} {self.stat_label}"


def build_rows(board, history: dict[str, list[dict]] | None = None) -> list[HitRow]:
    """One row per line the board carries for a player.

    `history` maps athlete id to their full newest first game log. Without
    it, L5, L10 and head to head come from the board's own sample and L20 and
    season stay pending.
    """
    rows: list[HitRow] = []
    if board is None:
        return rows
    labels = sport_registry.get(board.sport).stat_labels
    for view in board.players:
        full = (history or {}).get(str(view.athlete_id))
        games = merge_games(full or [], view.recent, view.vs_games)
        for bet in view.bets:
            key, line, side = bet.stat_key, float(bet.line), bet.side or "over"
            values = recorded(games, key)
            rows.append(HitRow(
                sport=view.sport, athlete_id=str(view.athlete_id),
                player=view.name, position=view.position, team=view.team,
                opponent=view.opponent, at_home=view.at_home,
                kickoff=view.kickoff, game_id=view.game_id,
                injury_status=view.injury_status,
                stat_key=key, stat_label=labels.get(key, key),
                side=side, line=line, priced=bet.priced, odds=bet.odds,
                bookmaker=bet.bookmaker or "", probability=bet.probability,
                edge=bet.edge,
                l5=window_rate(games, key, line, side, 5),
                l10=window_rate(games, key, line, side, 10),
                l20=window_rate(games, key, line, side, 20) if full is not None else None,
                season=season_rate(games, key, line, side) if full is not None else None,
                h2h=h2h_rate(games, key, line, side, view.opponent),
                avg10=average(values[:10]),
                games_logged=len(values),
            ))
    rows.sort(key=lambda r: (r.l10.pct or 0, r.l10.games, r.probability), reverse=True)
    return rows


# ------------------------------------------------------------ full game logs
def load_history(board, progress=None) -> dict[str, list[dict]]:
    """Every board player's full game log, newest first, games they played.

    Reads through EspnStats, whose disk cache the board build has just filled,
    so this is mostly local file reads.
    """
    from ..data.espn_stats import EspnStats

    client = EspnStats(board.sport)
    seasons = players_mod._seasons()
    out: dict[str, list[dict]] = {}
    views = {str(v.athlete_id): v for v in board.players}
    for i, (athlete_id, view) in enumerate(views.items()):
        try:
            games = [g for g in client.game_rows(athlete_id, seasons)
                     if h2h_mod.did_play(g, view.position, board.sport)]
        except Exception:
            games = []
        out[athlete_id] = games
        if progress:
            progress((i + 1) / max(1, len(views)))
    return out


class HistoryCache:
    """One full history read per board build, off the request thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[tuple, dict] = {}

    def get_or_start(self, board) -> dict:
        key = (board.sport, board.start, board.days, board.built_utc)
        with self._lock:
            entry = self._entries.get(key)
            if entry:
                return dict(entry)
            # A newer board for the sport replaces the old history.
            for old in [k for k in self._entries if k[0] == board.sport]:
                del self._entries[old]
            self._entries[key] = {"status": "building", "progress": 0.0,
                                  "history": None, "at": time.time()}

        def report(fraction):
            with self._lock:
                current = self._entries.get(key)
                if current:
                    current["progress"] = round(fraction, 3)

        def run():
            try:
                history = load_history(board, report)
                status = {"status": "ready", "history": history}
            except Exception as exc:  # the grid still works without it
                status = {"status": "error", "history": None, "message": str(exc)}
            with self._lock:
                if key in self._entries:
                    self._entries[key].update(status, progress=1.0)

        threading.Thread(target=run, name=f"hitrates-{board.sport}", daemon=True).start()
        with self._lock:
            return dict(self._entries[key])


HISTORY = HistoryCache()
