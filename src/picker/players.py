"""Player-centric read layer: whose props are most likely to hit.

The board reads a slate and prices games. This reads it the other way around -
one row per player, built from the same data the matchup view already pulls:

  build_player_board()   every skill player on the upcoming slate, with their
                         best bets, ranked by how likely they are to cash
  build_player_view()    one player: their best bets and their game log
  build_team_view()      one club's players, best first, top to bottom

Nothing here writes a pick. It is the research surface; automatic selection is
still the only path into the ledger.

Building across a slate is slow the first time - it pulls rosters and game logs
from ESPN - so the dashboard drives it through `BoardCache`, which runs the
build on a background thread and hands the UI its progress rather than blocking
a request on dozens of network calls. Everything ESPN returns is cached on
disk, so a second build is mostly local.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass, field

from ..data import sports as sport_registry
from ..db import database as db
from . import board as board_mod
from . import h2h as h2h_mod
from . import props as props_mod
from .h2h import PlayerSample

# A bet below this is not "likely to hit" in any useful sense.
PROP_FLOOR = 0.55
# How many recent games a player page and the board keep for form.
RECENT_WINDOW = 10
# Seasons of gamelogs to sample, matching config/settings.yaml head_to_head.
PLAYER_SEASONS = 3
# How many upcoming games the board will pull player data for by default. Every
# game is ~24 rosters of gamelogs on a cold cache, so this is the dial.
DEFAULT_MAX_GAMES = 6


@dataclass
class PlayerView:
    """One player on the upcoming slate, with the bets the model likes."""
    athlete_id: str
    name: str
    position: str
    team: str
    opponent: str
    sport: str
    game_id: int = 0
    game: str = ""
    kickoff: str = ""
    status: str = ""
    at_home: bool = False
    injury_status: str = ""
    recent: list[dict] = field(default_factory=list)
    vs_games: list[dict] = field(default_factory=list)
    bets: list[props_mod.PropCandidate] = field(default_factory=list)
    stat_keys: list[str] = field(default_factory=list)

    @property
    def best(self) -> props_mod.PropCandidate | None:
        return self.bets[0] if self.bets else None

    @property
    def games(self) -> list[dict]:
        """The recent-appearance log the player page tables read."""
        return self.recent

    @property
    def priced(self) -> bool:
        return any(bet.priced for bet in self.bets)

    def recent_form(self, stat_key: str, window: int = 5) -> str:
        """The last `window` values of one stat, newest first, as a string."""
        return self._form(self.recent, stat_key, window)

    def h2h_form(self, stat_key: str, window: int = 5) -> str:
        """The same, but only games against the opponent they are about to play.

        This is the head-to-head read the whole tool is built around: what a
        player has actually done against *this* defence, not against the
        league at large. Empty when they have never faced them in the sample,
        which the caller shows rather than quietly substituting overall form.
        """
        return self._form(self.vs_games, stat_key, window)

    def h2h_count(self, stat_key: str) -> int:
        """How many prior meetings recorded this stat at all."""
        return sum(1 for game in self.vs_games if game.get(stat_key) is not None)

    @staticmethod
    def _form(games: list[dict], stat_key: str, window: int) -> str:
        values = [game.get(stat_key) for game in games[:window]
                  if game.get(stat_key) is not None]
        return " · ".join(f"{v:g}" for v in values)


@dataclass
class PlayerBoard:
    sport: str
    days: int
    start: str
    players: list[PlayerView] = field(default_factory=list)
    games_scanned: int = 0
    games_total: int = 0
    priced: bool = False
    note: str = ""
    built_utc: str = ""

    def by_athlete(self, athlete_id: str) -> PlayerView | None:
        return next((p for p in self.players
                     if str(p.athlete_id) == str(athlete_id)), None)

    def by_team(self, abbrev: str) -> list[PlayerView]:
        target = (abbrev or "").upper()
        return [p for p in self.players if p.team.upper() == target]


# --------------------------------------------------------------- team helpers
def team_row(sport: str, abbrev: str, db_path=db.DEFAULT_DB) -> dict | None:
    """The DB record for one club: its ESPN id and full name."""
    rows = db.query(db_path, """
        SELECT t.abbrev, t.name, t.league_key
        FROM teams t
        WHERE t.abbrev = ? COLLATE NOCASE
          AND t.sport_id = (SELECT id FROM sports WHERE code=?)
        LIMIT 1""", (abbrev, sport))
    return dict(rows[0]) if rows else None


def team_options(sport: str, db_path=db.DEFAULT_DB) -> list[dict]:
    """Every club in a league, for the team filter on the players page."""
    rows = db.query(db_path, """
        SELECT t.abbrev, t.name FROM teams t
        WHERE t.sport_id = (SELECT id FROM sports WHERE code=?)
        ORDER BY t.name""", (sport,))
    return [dict(r) for r in rows]


def next_game(sport: str, abbrev: str, db_path=db.DEFAULT_DB) -> dict | None:
    """A club's soonest scheduled game, or None."""
    rows = db.query(db_path, """
        SELECT g.id, ht.abbrev AS home, at.abbrev AS away,
               g.start_utc, g.status, g.slate
        FROM games g
        JOIN teams ht ON ht.id = g.home_team
        JOIN teams at ON at.id = g.away_team
        WHERE g.status = 'scheduled'
          AND g.sport_id = (SELECT id FROM sports WHERE code=?)
          AND (ht.abbrev = ? COLLATE NOCASE OR at.abbrev = ? COLLATE NOCASE)
          AND datetime(g.start_utc) > datetime('now')
        ORDER BY g.start_utc
        LIMIT 1""", (sport, abbrev, abbrev))
    return dict(rows[0]) if rows else None


def _opponent_of(game: dict, abbrev: str) -> str:
    target = (abbrev or "").upper()
    if (game.get("home") or "").upper() == target:
        return game.get("away") or ""
    return game.get("home") or ""


# ------------------------------------------------------------ sample building
def _sample_for(client, athlete: dict, team_abbrev: str, opponent_abbrev: str,
                seasons: list[int] | None = None,
                injury_status: str = "") -> PlayerSample:
    """One player's games, split into form and games against this opponent."""
    sport = client.sport.code
    seasons = seasons or _seasons()
    rows = [r for r in client.game_rows(athlete["id"], seasons)
            if h2h_mod.did_play(r, athlete["position"], sport)]
    opponent = h2h_mod.to_espn(opponent_abbrev) if opponent_abbrev else ""
    return PlayerSample(
        athlete_id=str(athlete["id"]),
        name=athlete["name"],
        position=athlete["position"],
        team=h2h_mod.to_espn(team_abbrev),
        opponent=opponent,
        vs_games=[r for r in rows
                  if opponent and (r.get("opponent") or "").upper() == opponent],
        recent_games=rows[:RECENT_WINDOW],
        injury_status=injury_status,
        sport=sport,
    )


def _seasons() -> list[int]:
    back = int((db.get_config().get("head_to_head", {}) or {})
               .get("player_seasons", PLAYER_SEASONS))
    year = dt.date.today().year
    return [year - i for i in range(max(1, back))]


def _bets_for(sample: PlayerSample,
              priced: list[props_mod.PropCandidate]) -> list[props_mod.PropCandidate]:
    """Best bets for one player: model lines, upgraded by any real price.

    The model-only pass guarantees every player has a view even with no book
    price. Where a priced candidate exists for the same stat and side it wins,
    because it carries the actual number and the edge against it.
    """
    bets: dict[tuple[str, str], props_mod.PropCandidate] = {}
    for candidate in props_mod.candidates_for_player(
            sample, target=0.72, min_probability=PROP_FLOOR):
        bets[(candidate.stat_key, candidate.side)] = candidate
    for candidate in priced:
        if str(candidate.athlete_id) == str(sample.athlete_id):
            bets[(candidate.stat_key, candidate.side)] = candidate
    out = list(bets.values())
    out.sort(key=lambda c: (c.probability, c.projection.n_eff), reverse=True)
    return out


def _view_for(sample: PlayerSample, card, priced) -> PlayerView:
    return PlayerView(
        athlete_id=str(sample.athlete_id), name=sample.name,
        position=sample.position, team=sample.team, opponent=sample.opponent,
        sport=sample.sport, game_id=card.game_id, game=card.matchup,
        kickoff=card.kickoff_local(), status=card.status,
        at_home=(sample.team.upper() == card.home.upper()),
        injury_status=sample.injury_status,
        recent=sample.recent_games, vs_games=sample.vs_games,
        bets=_bets_for(sample, priced), stat_keys=sample.stat_keys())


# ------------------------------------------------------------------ the board
def build_player_board(sport: str = "nfl", days: int = 2,
                       start: dt.date | None = None, db_path=db.DEFAULT_DB,
                       max_games: int = DEFAULT_MAX_GAMES,
                       fetch_prices: bool = False,
                       team: str | None = None,
                       progress=None) -> PlayerBoard:
    """Every player on the upcoming slate, best bets first.

    `team` narrows the scan to one club's next game, which is the cheap path
    the team filter takes. `fetch_prices` spends Odds API credits; left off,
    the board is model-only unless prices are already cached.
    """
    start = start or dt.date.today()
    board = PlayerBoard(sport=sport, days=days, start=start.isoformat(),
                        built_utc=db.utc_now())

    def step(fraction, message):
        if progress:
            progress(fraction, message)

    step(0.02, f"Reading the {sport_registry.get(sport).name} schedule...")
    cards = board_mod.upcoming_board(sport, days=days, start=start,
                                     db_path=db_path)
    if team:
        target = team.upper()
        cards = [c for c in cards if target in (c.home.upper(), c.away.upper())]
    board.games_total = len(cards)
    cards = cards[:max_games]

    if not cards:
        board.note = "No upcoming games to build from."
        step(1.0, "Nothing to build.")
        return board

    views: list[PlayerView] = []
    priced_any = False
    for index, card in enumerate(cards):
        base = 0.05 + 0.9 * index / len(cards)

        def game_step(fraction, message, base=base):
            step(min(0.98, base + 0.9 * fraction / len(cards)), message)

        try:
            deep = board_mod.deep_matchup(
                card, fetch_prices=fetch_prices, progress=game_step)
        except Exception as exc:  # one bad game must not sink the whole board
            board.note = f"{card.matchup}: {exc}"
            continue
        if deep.priced:
            priced_any = True
        elif deep.props_note and not board.note:
            board.note = deep.props_note
        for sample in deep.players():
            views.append(_view_for(sample, card, deep.props))
        board.games_scanned += 1

    views.sort(key=lambda p: (p.best.probability if p.best else 0.0), reverse=True)
    board.players = views
    board.priced = priced_any
    step(1.0, f"Read {len(views)} players from {board.games_scanned} games.")
    return board


def build_player_view(sport: str, athlete_id: str, team_abbrev: str,
                      db_path=db.DEFAULT_DB,
                      fetch_prices: bool = False) -> PlayerView | None:
    """One player's best bets and game log, for the player page.

    Does not need the full board: it pulls the one roster and one gamelog. If a
    board is already cached the caller can pass it in instead, but this stands
    on its own so a shared link always resolves.
    """
    from ..data.espn_stats import EspnStats
    team = team_row(sport, team_abbrev, db_path)
    if not team:
        return None
    client = EspnStats(sport)
    roster = client.roster(team["league_key"])
    athlete = next((p for p in roster if str(p["id"]) == str(athlete_id)), None)
    if athlete is None:
        return None

    game = next_game(sport, team["abbrev"], db_path)
    opponent = _opponent_of(game, team["abbrev"]) if game else ""
    sample = _sample_for(client, athlete, team["abbrev"], opponent)

    priced: list[props_mod.PropCandidate] = []
    card = _card_for(sport, game, db_path) if game else None
    if card is not None and fetch_prices:
        try:
            deep = board_mod.deep_matchup(card, fetch_prices=True)
            priced = deep.props
        except Exception:
            priced = []

    return _view_for(sample, card, priced) if card else PlayerView(
        athlete_id=str(sample.athlete_id), name=sample.name,
        position=sample.position, team=sample.team, opponent=sample.opponent,
        sport=sample.sport, injury_status=sample.injury_status,
        recent=sample.recent_games, vs_games=sample.vs_games,
        bets=_bets_for(sample, priced), stat_keys=sample.stat_keys())


def build_team_view(sport: str, abbrev: str, days: int = 7,
                    db_path=db.DEFAULT_DB, fetch_prices: bool = False,
                    players: list[PlayerView] | None = None) -> dict | None:
    """One club: its players ranked, plus the game lines on its next fixture.

    `players` lets the caller hand over already-built views from a cached
    board, so opening a team from the players page does not repeat the ESPN
    pulls the board just did.
    """
    team = team_row(sport, abbrev, db_path)
    if not team:
        return None
    game = next_game(sport, team["abbrev"], db_path)
    opponent = _opponent_of(game, team["abbrev"]) if game else ""

    card = _card_for(sport, game, db_path) if game else None
    note = ""
    if players is None:
        players = []
        if card is not None:
            try:
                deep = board_mod.deep_matchup(card, fetch_prices=fetch_prices)
                priced = deep.props
                side = (deep.home_players
                        if card.home.upper() == team["abbrev"].upper()
                        else deep.away_players)
                for sample in side:
                    players.append(_view_for(sample, card, priced))
            except Exception as exc:
                note = str(exc)
        else:
            from ..data.espn_stats import EspnStats
            client = EspnStats(sport)
            for athlete in client.roster(team["league_key"]):
                sample = _sample_for(client, athlete, team["abbrev"], "")
                if not sample.recent_games:
                    continue
                players.append(PlayerView(
                    athlete_id=str(sample.athlete_id), name=sample.name,
                    position=sample.position, team=sample.team, opponent="",
                    sport=sample.sport, injury_status=sample.injury_status,
                    recent=sample.recent_games, vs_games=sample.vs_games,
                    bets=_bets_for(sample, []), stat_keys=sample.stat_keys()))

    players = list(players)
    players.sort(key=lambda p: (p.best.probability if p.best else 0.0), reverse=True)
    picks = []
    if card is not None:
        picks = [c for c in card.candidates
                 if c.side.upper() == team["abbrev"].upper() or c.market == "total"]
        picks.sort(key=lambda c: c.edge, reverse=True)
    return {
        "sport": sport,
        "abbrev": team["abbrev"],
        "name": team["name"],
        "game": game,
        "opponent": opponent,
        "at_home": bool(game and (game.get("home") or "").upper() == team["abbrev"].upper()),
        "card": card,
        "players": players,
        "picks": picks,
        "priced": bool(picks),
        "note": note,
    }


def _card_for(sport: str, game: dict | None, db_path):
    """The full GameCard for a DB game row, priced and modelled."""
    if not game:
        return None
    card = board_mod.day_board(_game_date(game), sport, db_path)
    target = game.get("id")
    return next((c for c in card if c.game_id == target), None)


def _game_date(game: dict) -> dt.date:
    start = game.get("start_utc") or ""
    try:
        return dt.datetime.fromisoformat(
            start.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=-4))).date()
    except (ValueError, AttributeError):
        return dt.date.today()


# --------------------------------------------------------------- background
class BoardCache:
    """Runs one player-board build at a time, off the request thread.

    The UI calls `start()` and then polls `get()`; while a build is running the
    entry reports `building` with the latest progress message instead of
    blocking a waitress worker on a cold ESPN cache.
    """

    def __init__(self, ttl: int = 900):
        self._lock = threading.Lock()
        self._entries: dict[tuple, dict] = {}
        self.ttl = ttl

    def _key(self, sport: str, days: int, start: dt.date, team: str | None) -> tuple:
        return (sport, days, start.isoformat(), (team or "").upper())

    def get(self, sport: str, days: int, start: dt.date,
            team: str | None = None) -> dict | None:
        with self._lock:
            entry = self._entries.get(self._key(sport, days, start, team))
            return dict(entry) if entry else None

    def is_fresh(self, entry: dict | None, ttl: int | None = None) -> bool:
        if not entry or entry.get("status") != "ready":
            return False
        return time.time() - entry.get("at", 0) < (ttl or self.ttl)

    def start(self, sport: str, days: int, start: dt.date,
              db_path=db.DEFAULT_DB, team: str | None = None,
              fetch_prices: bool = False, max_games: int = DEFAULT_MAX_GAMES,
              force: bool = False) -> dict:
        key = self._key(sport, days, start, team)
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry.get("status") == "building":
                return dict(entry)
            if entry and entry.get("status") == "ready" and not force \
                    and self.is_fresh(entry):
                return dict(entry)
            self._entries[key] = {"status": "building", "at": time.time(),
                                  "message": "Starting...", "board": None,
                                  "progress": 0.0}

        def report(fraction, message):
            with self._lock:
                current = self._entries.get(key)
                if current and current.get("status") == "building":
                    current["progress"] = round(float(fraction), 3)
                    current["message"] = message

        def run():
            try:
                board = build_player_board(
                    sport, days=days, start=start, db_path=db_path,
                    max_games=max_games, fetch_prices=fetch_prices,
                    team=team, progress=report)
                with self._lock:
                    self._entries[key] = {
                        "status": "ready", "at": time.time(),
                        "message": f"{len(board.players)} players",
                        "board": board, "progress": 1.0}
            except Exception as exc:
                with self._lock:
                    self._entries[key] = {"status": "error", "at": time.time(),
                                          "message": str(exc), "board": None,
                                          "progress": 0.0}

        thread = threading.Thread(target=run, name=f"player-board-{sport}",
                                  daemon=True)
        thread.start()
        with self._lock:
            return dict(self._entries[key])

    def find_player(self, sport: str, athlete_id: str) -> PlayerView | None:
        """A player already built into any ready board, if there is one."""
        with self._lock:
            for key, entry in self._entries.items():
                if key[0] != sport or entry.get("status") != "ready":
                    continue
                board = entry.get("board")
                view = board.by_athlete(athlete_id) if board else None
                if view is not None:
                    return view
        return None

    def find_team(self, sport: str, abbrev: str) -> list[PlayerView] | None:
        """A club's players from any ready board, best first."""
        with self._lock:
            for key, entry in self._entries.items():
                if key[0] != sport or entry.get("status") != "ready":
                    continue
                board = entry.get("board")
                if not board:
                    continue
                players = board.by_team(abbrev)
                if players:
                    return players
        return None

    def clear(self, sport: str | None = None) -> None:
        with self._lock:
            if sport is None:
                self._entries.clear()
            else:
                for key in [k for k in self._entries if k[0] == sport]:
                    del self._entries[key]


CACHE = BoardCache()
