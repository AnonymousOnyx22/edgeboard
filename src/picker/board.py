"""The board: every game on a given day, priced, modelled and ranked.

This is the read-only analysis layer the dashboard renders. It never writes
picks - saving a slip is a separate, deliberate action.

Two depths, because they cost very different amounts of time:

  day_board()     fast. Elo + market prices + team head-to-head history, all
                  from the local DB and the cached schedule frame.
  deep_matchup()  slow. Adds every skill player's record against this exact
                  opponent, pulled live from ESPN gamelogs.

The model is Elo, nudged by how these two teams have actually played each
other. The nudge is deliberately small and scales with the number of prior
meetings, because four games is evidence, not proof.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass, field

from ..data import models, sports as sport_registry
from ..db import database as db
from . import confidence as conf
from . import evidence
from . import h2h as h2h_mod
from . import props as props_mod
from .edge import american_to_prob, cover_probability, edge, over_probability, remove_vig

logger = logging.getLogger(__name__)

# How far Elo can be pulled toward the head-to-head record, per prior meeting.
H2H_WEIGHT_PER_MEETING = 0.06
H2H_WEIGHT_CAP = 0.30

# The closing line is a sharp prior: every probability is blended toward the
# price by `edge.market_trust`, and a raw gap past `edge.max_raw_edge` is
# flagged and never auto-picked (see confidence.py for the record behind both).


@dataclass
class LineCandidate:
    """One bettable game line with the model's view of it."""
    game_id: int
    game: str
    market: str            # h2h / spread / total
    side: str              # team abbrev, or Over/Under
    line: float | None
    odds: int
    model_prob: float
    implied_prob: float
    edge: float
    confidence: dict
    units: float
    bookmaker: str = ""
    h2h_note: str = ""
    backing: object | None = None   # evidence.Backing, attached by day_board
    raw_model_prob: float | None = None   # before the market prior was applied
    warnings: list = field(default_factory=list)

    @property
    def headline(self) -> str:
        if self.market == "total":
            return f"{self.side} {self.line:g}"
        if self.market == "spread":
            return f"{self.side} {self.line:+g}"
        return f"{self.side} ML"


@dataclass
class GameCard:
    game_id: int
    home: str
    away: str
    start_utc: str
    kickoff_window: str      # NFL only: thursday, sunday_early, sunday_night...
    status: str
    # The league-local calendar date. A game that starts after midnight UTC
    # still belongs to the previous evening's card, so callers that group or
    # cut off by day read this rather than slicing `start_utc`.
    game_date: str = ""
    venue: str = ""          # stadium or park
    venue_city: str = ""     # "St. Petersburg, Florida"
    sport: str = "nfl"
    home_espn_id: str = ""
    away_espn_id: str = ""
    # Full club names, e.g. "Buffalo Bills". The Odds API keys events on these,
    # so matching on abbreviations alone never finds the event.
    home_name: str = ""
    away_name: str = ""
    prediction: dict = field(default_factory=dict)
    h2h: h2h_mod.TeamH2H | None = None
    candidates: list[LineCandidate] = field(default_factory=list)

    @property
    def matchup(self) -> str:
        return f"{self.away} @ {self.home}"

    @property
    def best(self) -> LineCandidate | None:
        return max(self.candidates, key=lambda c: c.edge, default=None)

    @property
    def started(self) -> bool:
        """Has this game kicked off?

        Books keep quoting a game once it is under way, and those in-play
        numbers look like enormous edges to a model that does not know the
        score - a 7.5-run baseball line at -10000 is a live price, not value.
        """
        if self.status in ("in_progress", "final", "postponed"):
            return True
        try:
            start = dt.datetime.fromisoformat(self.start_utc.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return False
        return start <= dt.datetime.now(dt.timezone.utc)

    @property
    def bettable(self) -> bool:
        return not self.started

    def kickoff_day(self, tz="America/New_York") -> str:
        """The weekday and date a ticket prints, e.g. "Thu, Sep 18"."""
        moment = self._local(tz)
        return f"{moment.strftime('%a')}, {moment.strftime('%b')} "                f"{moment.strftime('%d').lstrip('0')}" if moment else ""

    def kickoff_time(self, tz="America/New_York") -> str:
        """Just the clock, e.g. "7:05 pm ET"."""
        moment = self._local(tz)
        if not moment:
            return ""
        return f"{moment.strftime('%I').lstrip('0')}"                f"{moment.strftime(':%M %p').replace('AM', 'am').replace('PM', 'pm')} ET"

    def _local(self, tz="America/New_York"):
        from zoneinfo import ZoneInfo
        try:
            moment = dt.datetime.fromisoformat(self.start_utc.replace("Z", "+00:00"))
            return moment.astimezone(ZoneInfo(tz))
        except (ValueError, AttributeError):
            return None

    def kickoff_local(self, tz="America/New_York") -> str:
        from zoneinfo import ZoneInfo
        try:
            moment = dt.datetime.fromisoformat(self.start_utc.replace("Z", "+00:00"))
            local = moment.astimezone(ZoneInfo(tz))
        except (ValueError, AttributeError):
            return ""
        # %-I is not portable to Windows, so strip the pad by hand.
        return f"{local.strftime('%a')} {local.strftime('%I').lstrip('0')}" \
               f"{local.strftime(':%M %p').replace('AM', 'am').replace('PM', 'pm')} ET"


@dataclass
class DeepMatchup:
    card: GameCard
    home_players: list[h2h_mod.PlayerSample] = field(default_factory=list)
    away_players: list[h2h_mod.PlayerSample] = field(default_factory=list)
    props: list[props_mod.PropCandidate] = field(default_factory=list)
    priced: bool = False          # True = props carry real book prices
    props_note: str = ""          # why prices are missing, when they are

    def players(self) -> list[h2h_mod.PlayerSample]:
        return self.home_players + self.away_players


# ---------------------------------------------------------------- the board
def games_on(day: dt.date, sport: str = "nfl", db_path=db.DEFAULT_DB) -> list[dict]:
    rows = db.query(db_path, """
        SELECT g.id, ht.abbrev AS home, at.abbrev AS away,
               ht.name AS home_name, at.name AS away_name,
               ht.league_key AS home_espn_id, at.league_key AS away_espn_id,
               g.slate, g.start_utc, g.game_date, g.status,
               g.home_score, g.away_score, g.venue, g.venue_city
        FROM games g
        JOIN teams ht ON ht.id = g.home_team
        JOIN teams at ON at.id = g.away_team
        WHERE g.sport_id=(SELECT id FROM sports WHERE code=?)
          AND COALESCE(g.game_date, date(g.start_utc))=date(?)
        ORDER BY g.start_utc""", (sport, day.isoformat()))
    return [dict(r) for r in rows]


def day_board(day: dt.date | None = None, sport: str = "nfl",
                db_path=db.DEFAULT_DB, min_edge: float | None = None,
                history=None, elo=None,
                include_started: bool = False) -> list[GameCard]:
    """Every game that day, modelled and priced. Fast: no live player pulls.

    Games already under way are still returned so the UI can show them, but
    they carry no candidates unless `include_started` is set: their prices are
    live, and a pregame model reads those as false value.
    """
    day = day or dt.date.today()
    cfg = db.get_config()
    threshold = min_edge if min_edge is not None else cfg["edge"]["target"]
    if history is None:
        # The local store is the deep history the user controls; it is what the
        # Elo trainer, the scoring rates and the head-to-head record all read.
        from ..data import history_store
        try:
            history = history_store.load_history(sport, db_path)
        except history_store.HistoryUnavailableError as exc:
            # No history means no model: a board priced from a default 1500 Elo
            # would report every underdog as value. Show the fixtures with no
            # candidates instead, and say so instead of pretending.
            logger.error("No history for %s: %s", sport, exc)
            return []
    # Every input to a historical board must be information that existed before
    # that date.  Filtering only Elo would still leak future scoring rates and
    # head-to-head results into old picks.
    prior_history = history_before(history, day)
    elo = elo or models.train_elo(prior_history, until_date=day, sport=sport)
    rates = scoring_rates(prior_history, sport)

    cards: list[GameCard] = []
    for row in games_on(day, sport, db_path):
        series = h2h_mod.team_series(row["home"], row["away"], prior_history,
                                     sport=sport, as_of=day)
        prediction = blended_prediction(row["home"], row["away"], elo, series)
        card = GameCard(
            game_id=row["id"], home=row["home"], away=row["away"],
            start_utc=row["start_utc"], kickoff_window=row["slate"] or "",
            status=row["status"], sport=sport,
            game_date=str(row.get("game_date") or ""),
            venue=str(row.get("venue") or ""),
            venue_city=str(row.get("venue_city") or ""),
            home_espn_id=str(row["home_espn_id"] or ""),
            away_espn_id=str(row["away_espn_id"] or ""),
            home_name=str(row["home_name"] or ""),
            away_name=str(row["away_name"] or ""),
            prediction=prediction, h2h=series,
        )
        if card.bettable or include_started:
            card.candidates = line_candidates(card, prediction, elo, threshold,
                                              db_path, rates)
            # Replay each offered number against the stored prior meetings.
            evidence.attach(card, sport, db_path=db_path, as_of=day)
        cards.append(card)
    return cards


def history_before(history, day: dt.date):
    """Return completed history strictly before ``day`` without mutating it."""
    if history is None or getattr(history, "empty", True):
        return history
    if "gameday" not in history.columns:
        return history.copy()
    import pandas as pd
    dates = pd.to_datetime(history["gameday"], errors="coerce").dt.date
    return history.loc[dates < day].copy()


# Building a board loads the season's history, trains Elo and prices every
# fixture - seconds of work. Every page needs one, and several need the same
# one twice in a request, so a built board is kept until the data behind it
# moves. The revision token is what makes that safe: it changes as soon as a
# game or a price does, so a stale board is never served.
_BOARD_LOCK = threading.Lock()
_BOARD_CACHE: dict[tuple, tuple[tuple, list]] = {}


def _board_revision(db_path) -> tuple:
    """A cheap token that changes whenever anything a board reads changes."""
    row = db.query(db_path, """
        SELECT (SELECT COUNT(*) FROM games)            AS games,
               (SELECT MAX(id) FROM games)             AS last_game,
               (SELECT COUNT(*) FROM odds)             AS odds,
               (SELECT MAX(captured_utc) FROM odds)    AS last_price,
               (SELECT MAX(COALESCE(home_score, -1) + COALESCE(away_score, -1))
                  FROM games)                          AS scores""")[0]
    return tuple(row[key] for key in
                 ("games", "last_game", "odds", "last_price", "scores"))


def clear_board_cache() -> None:
    """Drop every cached board. For tests and after a bulk import."""
    with _BOARD_LOCK:
        _BOARD_CACHE.clear()


def upcoming_board(sport: str = "nfl", days: int = 7, start: dt.date | None = None,
                   db_path=db.DEFAULT_DB, min_edge: float | None = None,
                   include_started: bool = False,
                   limit: int | None = None) -> list[GameCard]:
    """Every game from `start` through the next `days`, modelled and priced.

    The expensive setup - loading history, training Elo, computing scoring
    rates - is done once and reused across the whole range, rather than per
    day, which is what `day_board` would do on its own.

    The result is cached until the games or prices behind it change, because
    a page that shows fixtures and a league table would otherwise pay for the
    same board twice in one request.

    Games already under way are dropped by default, so this is a forward-
    looking card: the next games worth betting, not a diary.
    """
    start = start or dt.date.today()
    key = (sport, days, start.isoformat(), str(db_path), min_edge,
           include_started, limit)
    revision = _board_revision(db_path)
    with _BOARD_LOCK:
        hit = _BOARD_CACHE.get(key)
        if hit is not None and hit[0] == revision:
            return list(hit[1])

    from ..data import history_store
    try:
        history = history_store.load_history(sport, db_path)
    except history_store.HistoryUnavailableError as exc:
        logger.error("No history for %s: %s", sport, exc)
        return []
    elo = models.train_elo(history, until_date=start, sport=sport)

    cards: list[GameCard] = []
    for offset in range(max(1, days)):
        cards += day_board(start + dt.timedelta(days=offset), sport, db_path,
                           min_edge, history=history, elo=elo,
                           include_started=include_started)

    if not include_started:
        cards = [c for c in cards if c.bettable]
    cards.sort(key=lambda c: c.start_utc or "")
    cards = cards[:limit] if limit else cards
    with _BOARD_LOCK:
        _BOARD_CACHE[key] = (revision, cards)
    return list(cards)


def next_games(sport: str = "nfl", count: int = 5, days: int = 14,
               db_path=db.DEFAULT_DB, min_edge: float | None = None) -> list[GameCard]:
    """The next `count` games for a sport, whenever they happen to be.

    Useful between rounds, when a league's next fixtures are a week out and
    "today" is empty.
    """
    return upcoming_board(sport, days=days, db_path=db_path,
                          min_edge=min_edge, limit=count)


# Older name, kept so nothing breaks mid-rename.
slate_board = day_board


def blended_prediction(home: str, away: str, elo: models.EloSystem,
                       series: h2h_mod.TeamH2H | None) -> dict:
    """Elo, pulled toward how these two have actually played each other."""
    base = elo.predict(home, away)
    out = dict(base)
    out["elo_margin"] = base["margin_mean"]
    out["h2h_margin"] = None
    out["h2h_total"] = None
    out["h2h_weight"] = 0.0
    if not series or not series.meetings:
        return out

    weight = min(H2H_WEIGHT_CAP, H2H_WEIGHT_PER_MEETING * series.n)
    h2h_margin = series.avg_margin_for(home)
    if h2h_margin is not None:
        blended = (1 - weight) * base["margin_mean"] + weight * h2h_margin
        out["margin_mean"] = blended
        out["h2h_margin"] = h2h_margin
        # Keep the win probability consistent with the adjusted margin.
        out["home_win_prob"] = _margin_to_win_prob(blended, base["margin_sd"])
    out["h2h_total"] = series.avg_total()
    out["h2h_weight"] = weight
    return out


def _margin_to_win_prob(margin_mean: float, margin_sd: float) -> float:
    """P(margin > 0) under the model's normal margin."""
    p = cover_probability(margin_mean, margin_sd, 0.0)
    return max(0.02, min(0.98, p))


def scoring_rates(history, sport: str = "nfl", window: int = 20,
                  shrink: float = 6.0) -> dict:
    """Points scored and allowed per game, per team, from recent results.

    A flat league-average total is not good enough to bet into: it disagrees
    with the market by a touchdown on any high-scoring matchup and then reports
    that disagreement as a huge edge. These rates give each team its own
    offensive and defensive level, shrunk toward the league mean so that a team
    with three games played is not treated as a known quantity.

    Returns {"league": mean_points_per_team, "teams": {abbrev: {pf, pa, n}}}.
    """
    config = sport_registry.get(sport)
    league_default = config.total_base / 2.0
    if history is None or getattr(history, "empty", True):
        return {"league": league_default, "teams": {}}

    frame = history.dropna(subset=["home_score", "away_score"])
    if frame.empty:
        return {"league": league_default, "teams": {}}
    if "gameday" in frame.columns:
        frame = frame.sort_values("gameday")

    scored: dict[str, list[float]] = {}
    allowed: dict[str, list[float]] = {}
    for row in frame.itertuples(index=False):
        home, away = getattr(row, "home_team"), getattr(row, "away_team")
        hs, as_ = float(getattr(row, "home_score")), float(getattr(row, "away_score"))
        scored.setdefault(home, []).append(hs)
        allowed.setdefault(home, []).append(as_)
        scored.setdefault(away, []).append(as_)
        allowed.setdefault(away, []).append(hs)

    every = [v for values in scored.values() for v in values]
    league = sum(every) / len(every) if every else league_default

    teams: dict[str, dict] = {}
    for team, values in scored.items():
        recent_pf = values[-window:]
        recent_pa = allowed.get(team, [])[-window:]
        n = len(recent_pf)
        if not n:
            continue
        weight = n / (n + shrink)
        pf = weight * (sum(recent_pf) / n) + (1 - weight) * league
        pa = (weight * (sum(recent_pa) / len(recent_pa)) + (1 - weight) * league
              if recent_pa else league)
        teams[team] = {"pf": pf, "pa": pa, "n": n}
    return {"league": league, "teams": teams}


def total_model(prediction: dict, sport: str = "nfl", card: "GameCard | None" = None,
                rates: dict | None = None) -> tuple[float, float]:
    """Model mean/sd for the game total.

    Built from the two teams' own scoring and concession rates where they are
    known, falling back to the configured league base when they are not, then
    nudged by how much these two have actually scored against each other.
    """
    config = sport_registry.get(sport)
    settings = (db.get_config().get("total_model", {}) or {}).get(sport, {})
    base = float(settings.get("base", config.total_base))
    sd = float(settings.get("sd", config.total_sd))

    if rates and card:
        teams = rates.get("teams", {})
        home = teams.get(_history_key(card.home, sport))
        away = teams.get(_history_key(card.away, sport))
        if home and away:
            # Each side's expected points: its offence against the other's defence.
            home_points = (home["pf"] + away["pa"]) / 2.0
            away_points = (away["pf"] + home["pa"]) / 2.0
            base = home_points + away_points

    h2h_total, weight = prediction.get("h2h_total"), prediction.get("h2h_weight", 0.0)
    if h2h_total is not None and weight:
        base = (1 - weight) * base + weight * float(h2h_total)
    return base, sd


# ------------------------------------------------------------ line pricing
def best_pair(game_id: int, market: str, side: str, opponent: str,
              db_path=db.DEFAULT_DB):
    """Best price for a side, with the opposing price from the same book."""
    line_match = "1=1"
    if market == "spread":
        line_match = "ABS(a.line + b.line) < 0.0001"
    elif market == "total":
        line_match = "ABS(a.line - b.line) < 0.0001"
    from ..data.odds_client import allowed_books
    books = allowed_books()
    book_filter, book_params = "", []
    if books:
        book_filter = (" AND LOWER(a.bookmaker) IN ("
                       + ",".join("?" * len(books)) + ")")
        book_params = books

    rows = db.query(db_path, f"""
        SELECT a.price AS side_price, a.line AS side_line,
               b.price AS opp_price, a.bookmaker
        FROM odds a JOIN odds b
          ON b.game_id=a.game_id AND b.market=a.market AND b.bookmaker=a.bookmaker
        WHERE a.game_id=? AND a.market=?
          AND LOWER(a.side)=LOWER(?) AND LOWER(b.side)=LOWER(?)
          AND {line_match}{book_filter}
        ORDER BY a.price DESC, a.captured_utc DESC
        LIMIT 1""", (game_id, market, side, opponent, *book_params))
    return rows[0] if rows else None


def line_candidates(card: GameCard, prediction: dict, elo: models.EloSystem,
                    threshold: float, db_path=db.DEFAULT_DB,
                    rates: dict | None = None) -> list[LineCandidate]:
    """Every priced line on this game the model rates above `threshold`."""
    cfg = db.get_config()
    quality = quality_flags(card, elo)
    out: list[LineCandidate] = []

    def add(market, side, line, pair, model_prob, note=""):
        odds = int(pair["side_price"])
        try:
            implied = remove_vig(american_to_prob(pair["opp_price"]),
                                 american_to_prob(odds))[1]
        except ValueError:
            implied = american_to_prob(odds)

        # The market is a sharp prior, exactly as it is for props. An Elo and
        # scoring-rate model that disagrees with a closing line by twenty
        # points of probability has almost always missed something the book
        # knows - a starter out, weather, a short week - rather than found
        # value. Blending toward the price keeps the ranking while stopping
        # those from being sold, and staked, as the best bets on the board.
        raw_prob = model_prob
        trust = conf.market_trust(cfg)
        blended = (1 - trust) * model_prob + trust * implied
        value = edge(blended, implied)
        if value < threshold:
            return

        warnings: list[str] = []
        if raw_prob - implied >= conf.max_raw_edge(cfg):
            warnings.append(
                f"Model is {abs(blended - implied):.0%} away from the market. "
                "That usually means information the model cannot see.")

        breakdown = conf.compute_confidence(
            value, blended, quality, cfg["confidence"],
            cfg["edge"].get("max_edge_display", cfg["edge"]["target"]))
        out.append(LineCandidate(
            game_id=card.game_id, game=card.matchup, market=market, side=side,
            line=line, odds=odds, model_prob=round(blended, 4),
            implied_prob=round(implied, 4), edge=round(value, 4),
            confidence=breakdown, units=conf.units_from_confidence(breakdown["score"], cfg),
            bookmaker=pair["bookmaker"] or "", h2h_note=note,
            raw_model_prob=round(raw_prob, 4), warnings=warnings))

    home_prob = prediction["home_win_prob"]
    for side, opponent, prob in ((card.home, card.away, home_prob),
                                 (card.away, card.home, 1 - home_prob)):
        pair = best_pair(card.game_id, "h2h", side, opponent, db_path)
        if pair:
            add("h2h", side, None, pair, prob, h2h_note(card, side))

    for side, opponent in ((card.home, card.away), (card.away, card.home)):
        pair = best_pair(card.game_id, "spread", side, opponent, db_path)
        if pair and pair["side_line"] is not None:
            line = float(pair["side_line"])
            mean = (prediction["margin_mean"] if side == card.home
                    else -prediction["margin_mean"])
            prob = cover_probability(mean, prediction["margin_sd"], line)
            add("spread", side, line, pair, prob, h2h_note(card, side))

    base, sd = total_model(prediction, card.sport, card, rates)
    for side, opponent in (("Over", "Under"), ("Under", "Over")):
        pair = best_pair(card.game_id, "total", side, opponent, db_path)
        if pair and pair["side_line"] is not None:
            line = float(pair["side_line"])
            p_over = over_probability(base, sd, line)
            prob = p_over if side == "Over" else 1 - p_over
            add("total", side, line, pair, prob, total_note(card, side, line))
    return out


def _history_key(abbrev: str, sport: str) -> str:
    """Abbreviation as the history frame and the Elo table spell it.

    The store normalises every row to ESPN spelling, Elo trains on that same
    frame, and the board's scoring-rate keys are those same labels, so a lookup
    must normalise to ESPN too - the old nflverse translation meant the Rams
    and Commanders never matched anything."""
    return h2h_mod.to_espn(abbrev) if sport == "nfl" else (abbrev or "").upper()


def h2h_note(card: GameCard, team: str) -> str:
    """What the head-to-head record says about this side."""
    if not card.h2h or not card.h2h.meetings:
        return ""
    covers = card.h2h.cover_rate_for(team)
    wins = sum(1 for m in card.h2h.meetings
               if m.winner() == _history_key(team, card.sport))
    note = f"{team} won {wins} of the last {card.h2h.n} meetings"
    if covers and covers[1]:
        note += f", covered {covers[0]}/{covers[1]}"
    return note


def total_note(card: GameCard, side: str, line: float) -> str:
    if not card.h2h or not card.h2h.meetings:
        return ""
    hits = sum(1 for m in card.h2h.meetings
               if (m.total > line if side == "Over" else m.total < line))
    average = card.h2h.avg_total()
    return (f"{side} hit in {hits}/{card.h2h.n} meetings at this number"
            + (f" (avg {average:.1f})" if average is not None else ""))


def quality_flags(card: GameCard, elo: models.EloSystem) -> dict:
    """What the confidence model should dock this pick for."""
    return {
        "games_of_history": min(elo.games(_history_key(card.home, card.sport)),
                                elo.games(_history_key(card.away, card.sport))),
        "missing_starter_confirmation": False,   # ESPN depth/injury feed is wired
        "missing_injury_data": False,
        "missing_stat_feed": False,
        "extreme_weather": False,
        "no_head_to_head": not (card.h2h and card.h2h.meetings),
    }


# ------------------------------------------------------------ deep matchup
def deep_matchup(card: GameCard, seasons: list[int] | None = None,
                 stats=None, min_probability: float = 0.60,
                 min_edge: float = 0.03, require_h2h: bool = False,
                 max_players: int = 12, fetch_prices: bool = True,
                 progress=None) -> DeepMatchup:
    """Pull both rosters' gamelogs, price their props, and rank them.

    This makes a lot of live requests the first time a matchup is opened;
    everything is cached on disk afterwards. `progress` is an optional
    callable(fraction, message) for a UI progress bar.

    Prop prices cost Odds API credits, one per market per game. With
    `fetch_prices=False` only already-cached prices are used, and if there are
    none the props fall back to the unpriced model-only watchlist.
    """
    from ..data.espn_stats import EspnStats
    client = stats or EspnStats(card.sport)
    seasons = seasons or h2h_mod.default_seasons()

    def step(fraction, message):
        if progress:
            progress(fraction, message)

    step(0.05, f"Loading {card.away} roster and game logs...")
    away_players = h2h_mod.player_series(card.away_espn_id, card.away, card.home,
                                         seasons, client, max_players=max_players,
                                         sport=card.sport)
    step(0.45, f"Loading {card.home} roster and game logs...")
    home_players = h2h_mod.player_series(card.home_espn_id, card.home, card.away,
                                         seasons, client, max_players=max_players,
                                         sport=card.sport)
    samples = away_players + home_players

    step(0.8, "Fetching player prop prices...")
    book, note = _prop_book(card, fetch_prices)

    step(0.9, "Scoring props against the matchup...")
    if book is not None and not book.empty:
        ranked = props_mod.rank_market_candidates(
            samples, book, min_probability=min_probability, min_edge=min_edge,
            require_h2h=require_h2h, limit=40)
        priced = True
    else:
        ranked = props_mod.rank_candidates(
            samples, min_probability=max(min_probability, 0.70),
            require_h2h=require_h2h, limit=40)
        priced = False
    for candidate in ranked:
        # Tag props with the game so the parlay optimizer can spot same-game legs.
        candidate.game_id = card.game_id
    step(1.0, "Done.")
    return DeepMatchup(card=card, home_players=home_players, away_players=away_players,
                       props=ranked, priced=priced, props_note=note)


def _prop_book(card: GameCard, fetch_prices: bool):
    """(PropBook | None, explanatory note). Never raises on a pricing failure."""
    from ..data.props_odds import OddsAPIError, PropBook, PropsOddsClient
    if card.started:
        # Books keep quoting props in-play, and those live numbers read as
        # enormous edges to a model that does not see the score. Neither a live
        # fetch nor a stale cache belongs in a pregame read.
        return None, "Game already started - in-play prop prices are not modelled."
    try:
        client = PropsOddsClient(card.sport)
        event_id = client.find_event_id(card.home_name or card.home,
                                        card.away_name or card.away,
                                        card.start_utc)
        if not event_id:
            return None, "No matching Odds API event, so props are model-only."
        cached = client.cached_event_props(event_id)
        if cached is not None:
            return PropBook(cached, card.sport), ""
        if not fetch_prices:
            return None, "Prop prices not cached. Fetch them to price these props."
        return PropBook(client.event_props(event_id), card.sport), ""
    except OddsAPIError as exc:
        return None, str(exc)
    except Exception as exc:  # network trouble should not kill the matchup view
        return None, f"Prop prices unavailable: {exc}"


def parlay_pool(cards: list[GameCard], props: list[props_mod.PropCandidate] | None = None,
                min_probability: float = 0.55):
    """Every line and prop worth considering, as parlay legs.

    Games that have already started are skipped: you cannot back a pregame
    read into a live price.
    """
    from .parlay_builder import Leg, prop_leg
    pool: list[Leg] = []
    for card in cards:
        if not card.bettable:
            continue
        for candidate in card.candidates:
            if candidate.model_prob < min_probability:
                continue
            pool.append(Leg(
                game_id=candidate.game_id, market=candidate.market,
                team=candidate.side, line=candidate.line,
                odds_american=candidate.odds, model_prob=candidate.model_prob,
                reason=candidate.h2h_note, label=f"{candidate.headline}  ({card.matchup})",
                priced=True))
    for candidate in props or []:
        if candidate.probability >= min_probability:
            pool.append(prop_leg(candidate))
    return pool


def card_for_game(sport: str, game_id: int, db_path=db.DEFAULT_DB,
                  days: int = 21) -> GameCard | None:
    """One fixture's card by id, priced and scored like any other.

    Built through `day_board` for that game's own date so the card carries the
    same prediction and candidates the slate view shows, rather than a second,
    quietly different read of the same game.
    """
    rows = db.query(db_path, """
        SELECT COALESCE(g.game_date, date(g.start_utc)) AS day
        FROM games g
        WHERE g.id=? AND g.sport_id=(SELECT id FROM sports WHERE code=?)""",
        (game_id, sport))
    if not rows or not rows[0]["day"]:
        return None
    try:
        day = dt.date.fromisoformat(rows[0]["day"])
    except ValueError:
        return None
    cards = day_board(day, sport, db_path=db_path, include_started=True)
    return next((card for card in cards if card.game_id == game_id), None)


def next_game_by_team(sport: str = "nfl", days: int = 14,
                      start: dt.date | None = None,
                      db_path=db.DEFAULT_DB,
                      cards: list[GameCard] | None = None) -> list[dict]:
    """Every club in the league with its next fixture and best available pick.

    A club with no scheduled game in the window is still listed, with `card`
    left as None, so the page shows the whole league rather than only the
    teams that happen to be playing soon.

    `cards` lets a caller hand over a board it has already built. Reading and
    pricing a slate is the expensive part, and a page that shows both the
    fixtures and the league table would otherwise pay for it twice.
    """
    if cards is None:
        cards = upcoming_board(sport, days=days, start=start, db_path=db_path)

    first: dict[str, GameCard] = {}
    for card in cards:                      # already sorted by kickoff
        first.setdefault(card.home, card)
        first.setdefault(card.away, card)

    teams = db.query(db_path, """
        SELECT t.abbrev, t.name FROM teams t
        WHERE t.sport_id = (SELECT id FROM sports WHERE code=?)
        ORDER BY t.abbrev""", (sport,))

    out = []
    for team in teams:
        abbrev = team["abbrev"]
        card = first.get(abbrev)
        picks = []
        if card is not None:
            # Only the bets on this club, plus the game total, which belongs
            # to both sides of the fixture.
            picks = [c for c in card.candidates
                     if c.side == abbrev or c.market == "total"]
            picks.sort(key=lambda c: c.edge, reverse=True)
        out.append({
            "abbrev": abbrev,
            "name": team["name"],
            "card": card,
            "opponent": (card.away if card and card.home == abbrev
                         else card.home if card else None),
            "at_home": bool(card and card.home == abbrev),
            "picks": picks,
            "best": picks[0] if picks else None,
        })
    return out
