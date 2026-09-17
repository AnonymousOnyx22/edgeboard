"""The board: every game on a slate, priced, modelled and ranked.

This is the read-only analysis layer the dashboard renders. It never writes
picks - saving a slip is a separate, deliberate action.

Two depths, because they cost very different amounts of time:

  slate_board()   fast. Elo + market prices + team head-to-head history, all
                  from the local DB and the cached schedule frame.
  deep_matchup()  slow. Adds every skill player's record against this exact
                  opponent, pulled live from ESPN gamelogs.

The model is Elo, nudged by how these two teams have actually played each
other. The nudge is deliberately small and scales with the number of prior
meetings, because four games is evidence, not proof.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..data import models, sports as sport_registry
from ..db import database as db
from . import confidence as conf
from . import h2h as h2h_mod
from . import props as props_mod
from .edge import american_to_prob, cover_probability, edge, over_probability, remove_vig

# How far Elo can be pulled toward the head-to-head record, per prior meeting.
H2H_WEIGHT_PER_MEETING = 0.06
H2H_WEIGHT_CAP = 0.30


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
    slate: str
    status: str
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
               g.slate, g.start_utc, g.status, g.home_score, g.away_score
        FROM games g
        JOIN teams ht ON ht.id = g.home_team
        JOIN teams at ON at.id = g.away_team
        WHERE g.sport_id=(SELECT id FROM sports WHERE code=?)
          AND COALESCE(g.game_date, date(g.start_utc))=date(?)
        ORDER BY g.start_utc""", (sport, day.isoformat()))
    return [dict(r) for r in rows]


def slate_board(day: dt.date | None = None, sport: str = "nfl",
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
        from ..data import espn_schedule
        history = espn_schedule.load_or_build_history(sport)
    elo = elo or models.train_elo(history, until_date=day, sport=sport)
    rates = scoring_rates(history, sport)

    cards: list[GameCard] = []
    for row in games_on(day, sport, db_path):
        series = h2h_mod.team_series(row["home"], row["away"], history, sport=sport)
        prediction = blended_prediction(row["home"], row["away"], elo, series)
        card = GameCard(
            game_id=row["id"], home=row["home"], away=row["away"],
            start_utc=row["start_utc"], slate=row["slate"] or "",
            status=row["status"], sport=sport,
            home_espn_id=str(row["home_espn_id"] or ""),
            away_espn_id=str(row["away_espn_id"] or ""),
            home_name=str(row["home_name"] or ""),
            away_name=str(row["away_name"] or ""),
            prediction=prediction, h2h=series,
        )
        if card.bettable or include_started:
            card.candidates = line_candidates(card, prediction, elo, threshold,
                                              db_path, rates)
        cards.append(card)
    return cards


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
    rows = db.query(db_path, f"""
        SELECT a.price AS side_price, a.line AS side_line,
               b.price AS opp_price, a.bookmaker
        FROM odds a JOIN odds b
          ON b.game_id=a.game_id AND b.market=a.market AND b.bookmaker=a.bookmaker
        WHERE a.game_id=? AND a.market=?
          AND LOWER(a.side)=LOWER(?) AND LOWER(b.side)=LOWER(?)
          AND {line_match}
        ORDER BY a.price DESC, a.captured_utc DESC
        LIMIT 1""", (game_id, market, side, opponent))
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
        value = edge(model_prob, implied)
        if value < threshold:
            return
        breakdown = conf.compute_confidence(
            value, model_prob, quality, cfg["confidence"],
            cfg["edge"].get("max_edge_display", cfg["edge"]["target"]))
        out.append(LineCandidate(
            game_id=card.game_id, game=card.matchup, market=market, side=side,
            line=line, odds=odds, model_prob=round(model_prob, 4),
            implied_prob=round(implied, 4), edge=round(value, 4),
            confidence=breakdown, units=conf.units_from_confidence(breakdown["score"], cfg),
            bookmaker=pair["bookmaker"] or "", h2h_note=note))

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
    """Abbreviation as the history frame and the Elo table spell it."""
    return h2h_mod.to_nflverse(abbrev) if sport == "nfl" else (abbrev or "").upper()


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
