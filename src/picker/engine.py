"""Picker engine: syncs games and game-line prices, and persists model picks.

`Picker` works for any sport in the registry; `NflPicker` is the NFL-bound
alias the older call sites use. Reading and ranking a day's games lives in
picker/board.py - this module is the write side: syncing, and saving picks.

Nothing is posted anywhere. Approval happens in the dashboard.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from ..data import espn_client, models, odds_client, sports as sport_registry
from ..db import database as db
from . import confidence as conf
from .edge import american_to_prob, cover_probability, edge, over_probability, remove_vig

ODDS_MARKETS = ["h2h", "spreads", "totals"]


class Picker:
    """Sync and persistence for one sport."""

    def __init__(self, sport: str = "nfl", db_path=db.DEFAULT_DB,
                 odds_key: str | None = None, eta_groups: Optional[dict] = None):
        self.sport = sport_registry.get(sport)
        self.db_path = db_path
        self.espn = espn_client.EspnClient()
        self.odds = odds_client.OddsClient(api_key=odds_key)
        self.eta_groups = eta_groups or {"h2h": 0, "spreads": 0, "totals": 0}

    # ------------------------------------------------------------- sync
    def sync_games(self, day: dt.date | None = None) -> int:
        day = day or dt.date.today()
        return self.espn.upsert_games(self.sport.code, day, db_path=self.db_path)

    def sync_upcoming(self, days: int = 7, start: dt.date | None = None,
                      progress=None) -> dict:
        """Sync schedules for a range of days, then prices once.

        The Odds API returns every upcoming event for a sport in a single
        request, so prices cost the same whether one day or ten is being
        looked at. Schedules are per-day, so those are fetched in a loop.
        """
        start = start or dt.date.today()
        games = 0
        for offset in range(max(1, days)):
            day = start + dt.timedelta(days=offset)
            if progress:
                progress(offset / max(1, days), f"Schedule for {day.isoformat()}")
            games += self.sync_games(day)
        if progress:
            progress(0.9, "Fetching prices")
        odds = self.sync_odds(force=True, day=start)
        return {"sport": self.sport.code, "days": days, "games": games,
                "odds_source": odds["source"], "odds_games": odds["games"]}

    def sync_odds(self, force: bool = False, day: dt.date | None = None) -> dict:
        """Sync this sport's current prices into the odds table.
        Returns {'source': 'odds_api'|'espn_backup'|'none', 'games': n}."""
        try:
            payloads = self.odds.fetch_odds(self.sport.odds_key, force=force)
            n = self._store_odds_api(payloads)
            from .actions import snapshot_open_parlays
            snapshot_open_parlays(self.sport.code, self.db_path)
            return {"source": "odds_api", "games": n}
        except odds_client.OddsAPIError as e:
            self.eta_groups = {"h2h": 0, "spreads": 0, "totals": 1}
            n = self._store_espn_backup(day or dt.date.today())
            from .actions import snapshot_open_parlays
            snapshot_open_parlays(self.sport.code, self.db_path)
            return {"source": "espn_backup", "games": n, "note": str(e)}

    def _game_key(self, home_name: str, away_name: str,
                  start_utc: str | None = None) -> Optional[int]:
        rows = db.query(self.db_path, """
            SELECT g.id FROM games g
            JOIN teams ht ON ht.id = g.home_team
            JOIN teams at ON at.id = g.away_team
            JOIN sports s ON s.id = g.sport_id
            WHERE s.code=? AND (
                (LOWER(ht.name) = LOWER(?) AND LOWER(at.name) = LOWER(?))
                OR (LOWER(ht.abbrev) = LOWER(?) AND LOWER(at.abbrev) = LOWER(?))
            )
            ORDER BY CASE WHEN ? IS NULL THEN 0
                          ELSE ABS(julianday(g.start_utc) - julianday(?)) END,
                     g.start_utc DESC
            LIMIT 1
        """, (self.sport.code, home_name, away_name, home_name, away_name,
              start_utc, start_utc))
        return int(rows[0]["id"]) if rows else None

    def _store_odds_api(self, payload: list[dict]) -> int:
        n = 0
        conn = db.connect(self.db_path)
        try:
            for event in payload:
                home, away = event.get("home_team", ""), event.get("away_team", "")
                game_id = self._game_key(home, away, event.get("commence_time"))
                if game_id is None:
                    continue
                # Replace a game's quote set atomically. A malformed provider
                # payload can no longer leave half a market in the database.
                conn.execute("DELETE FROM odds WHERE game_id=?", (game_id,))
                for bk in event.get("bookmakers", []):
                    for mkt in bk.get("markets", []):
                        mkey = mkt.get("key", "")
                        if mkey not in ODDS_MARKETS:
                            continue
                        self._write_market(game_id, mkey, mkt.get("outcomes", []),
                                           bk.get("key", ""), conn=conn)
                n += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return n

    def _store_espn_backup(self, day: dt.date) -> int:
        """Best-effort backup using ESPN's embedded odds (verified fields)."""
        events = self.espn.events(self.sport.code, day)
        n = 0
        for ev in events:
            comp = self.espn.competition(ev)
            team_ids = self.espn.team_identity(comp)
            by_side = {t["home_away"]: t for t in team_ids}
            if not by_side.get("home") or not by_side.get("away"):
                continue
            game = self._game_key(by_side["home"]["name"], by_side["away"]["name"], ev.get("date"))
            if game is None:
                continue
            db.execute(self.db_path, "DELETE FROM odds WHERE game_id=?", (game,))
            for line in self.espn.backup_odds(comp):
                hm = int(line["home_moneyline"]) if line["home_moneyline"] else None
                am = int(line["away_moneyline"]) if line["away_moneyline"] else None
                if hm:
                    self._write_price(game, "h2h", by_side["home"]["abbrev"], hm, None, "ESPN", "espn")
                    n += 1
                if am:
                    self._write_price(game, "h2h", by_side["away"]["abbrev"], am, None, "ESPN", "espn")
                    n += 1
                over, under = line["over_odds"], line["under_odds"]
                if over and under and line["over_under"]:
                    self._write_price(game, "total", "Over", int(over), line["over_under"], "ESPN", "espn")
                    self._write_price(game, "total", "Under", int(under), line["over_under"], "ESPN", "espn")
                    n += 2
                home_ss, away_ss = line["home_spread_odds"], line["away_spread_odds"]
                if home_ss and away_ss and line["spread"] is not None:
                    # spread field is signed for the home team (favorite negative)
                    self._write_price(game, "spread", by_side["home"]["abbrev"], int(home_ss),
                                      line["spread"], "ESPN", "espn")
                    self._write_price(game, "spread", by_side["away"]["abbrev"], int(away_ss),
                                      -line["spread"], "ESPN", "espn")
                    n += 2
        return n

    def _write_market(self, game_id: int, mkey: str, outcomes: list[dict],
                      bookmaker: str, line_pick: float | None = None,
                      conn=None) -> None:
        if mkey == "h2h":
            for o in outcomes:
                side = self._normalize_team_side(game_id, o.get("name", ""))
                self._write_price(game_id, "h2h", side, o.get("price"), None,
                                  bookmaker, conn=conn)
        elif mkey == "spreads":
            for o in outcomes:
                side = self._normalize_team_side(game_id, o.get("name", ""))
                self._write_price(game_id, "spread", side, o.get("price"),
                                  o.get("point"), bookmaker, conn=conn)
        elif mkey == "totals":
            for o in outcomes:
                side = o.get("name", "")  # Over / Under
                self._write_price(game_id, "total", side, o.get("price"),
                                  o.get("point"), bookmaker, conn=conn)

    def _normalize_team_side(self, game_id: int, name: str) -> str:
        rows = db.query(self.db_path, """
            SELECT t.abbrev FROM teams t JOIN games g
              ON t.id IN (g.home_team, g.away_team)
            WHERE g.id=? AND (LOWER(t.name)=LOWER(?) OR LOWER(t.abbrev)=LOWER(?))
            LIMIT 1""", (game_id, name, name))
        return rows[0]["abbrev"] if rows else name

    def _is_pregame(self, game_id: int) -> bool:
        """Is this game still unplayed?

        Books keep quoting a game once it is under way, and The Odds API
        happily returns those in-play numbers. Storing them would let a
        pregame model read a live scoreline as enormous value, so they are
        refused at the point of entry rather than filtered later.
        """
        rows = db.query(self.db_path,
                        "SELECT start_utc, status FROM games WHERE id=?", (game_id,))
        if not rows:
            return False
        if rows[0]["status"] in ("in_progress", "final", "postponed"):
            return False
        try:
            start = dt.datetime.fromisoformat(
                (rows[0]["start_utc"] or "").replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return True
        return start > dt.datetime.now(dt.timezone.utc)

    def _write_price(self, game_id: int, market: str, side: str, price,
                     line: float | None, bookmaker: str, source: str = "odds_api",
                     conn=None) -> None:
        if price is None or side is None:
            return
        if not self._is_pregame(game_id):
            return
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        owns_connection = conn is None
        target = conn or db.connect(self.db_path)
        target.execute("""INSERT INTO odds (game_id, market, side, price, line, bookmaker, source, captured_utc)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (game_id, market, side, int(price), line, bookmaker or "unknown", source, now))
        if owns_connection:
            target.commit()
            target.close()

    # ------------------------------------------------------------ model
    def _history(self) -> object:
        from ..data import history_store
        return history_store.load_history(self.sport.code, self.db_path)

    def ensure_elo(self, until_date: dt.date | None = None) -> models.EloSystem:
        history = self._history()
        return models.train_elo(history, until_date=until_date or dt.date.today(),
                                sport=self.sport.code)

    def predict(self, home: str, away: str, elo: models.EloSystem) -> dict:
        return models.combined_probability(elo.predict(home, away), None)

    def _quality(self, elo: models.EloSystem, home: str, away: str) -> dict:
        return {
            "games_of_history": min(elo.games(home), elo.games(away)),
            "missing_starter_confirmation": True,   # no depth-chart feed wired yet
            "missing_injury_data": True,            # no injury feed wired yet
            "extreme_weather": False,
        }

    # ----------------------------------------------------- candidates
    def suggest(self, day: dt.date | None = None,
                ret_threshold: float | None = None) -> list[dict]:
        """Compute candidate edges without writing anything to the ledger.

        Persistence is intentionally owned by ``autopick`` so a candidate can
        never sit in a manual-review state.
        """
        cfg = db.get_config()
        threshold = ret_threshold if ret_threshold is not None else cfg["edge"]["target"]
        day = day or dt.date.today()
        self.sync_games(day)
        self.sync_odds(day=day)
        from ..data.history_store import HistoryUnavailableError
        try:
            elo = self.ensure_elo(day)
        except HistoryUnavailableError:
            # No history -> no model -> no candidates, never a default-1500 read.
            return []

        games = db.query(self.db_path, """
            SELECT g.id, ht.abbrev AS home, at.abbrev AS away, g.slate, g.start_utc
            FROM games g
            JOIN teams ht ON ht.id = g.home_team
            JOIN teams at ON at.id = g.away_team
            WHERE g.sport_id = (SELECT id FROM sports WHERE code=?)
              AND g.status='scheduled'
              AND datetime(g.start_utc) > datetime('now')
              AND COALESCE(g.game_date, date(g.start_utc))=date(?)
        """, (self.sport.code, day.isoformat()))
        picks: list[dict] = []
        for g in games:
            pred = self.predict(g["home"], g["away"], elo)
            picks += self._edges_for_game(g, pred, elo, threshold)

        return picks

    def _edges_for_game(self, game, pred: dict, elo: models.EloSystem,
                        threshold: float) -> list[dict]:
        # (side, model_prob, market, line, odds) per betting line
        suggestions = []
        for label, opponent, prob in (
                ("home", "away", pred["home_win_prob"]),
                ("away", "home", 1 - pred["home_win_prob"])):
            pair = self._best_pair(game["id"], "h2h", game[label], game[opponent])
            if pair:
                odds = int(pair["side_price"])
                ip = remove_vig(american_to_prob(pair["opp_price"]),
                                american_to_prob(odds))[1]
                e = edge(prob, ip)
                qual = self._quality(elo, game["home"], game["away"])
                if e >= threshold:
                    suggestions.append(self._make_pick(game, "h2h",
                                                       game["home"] if label == "home" else game["away"],
                                                       None, odds, prob, ip, e, qual))
        for side in ("home", "away"):
            other = game["away" if side == "home" else "home"]
            pair = self._best_pair(game["id"], "spread", game[side], other)
            if pair and pair["side_line"] is not None:
                line, odds = float(pair["side_line"]), int(pair["side_price"])
                side_mean = pred["margin_mean"] if side == "home" else -pred["margin_mean"]
                cover = cover_probability(side_mean, pred["margin_sd"], line)
                ip = remove_vig(american_to_prob(pair["opp_price"]),
                                american_to_prob(odds))[1]
                e = edge(cover, ip)
                qual = self._quality(elo, game["home"], game["away"])
                if e >= threshold:
                    suggestions.append(self._make_pick(game, "spread", game[side], line, odds,
                                                       cover, ip, e, qual))
        for side, opponent in (("Over", "Under"), ("Under", "Over")):
            pair = self._best_pair(game["id"], "total", side, opponent)
            if not pair or pair["side_line"] is None:
                continue
            total_line = float(pair["side_line"])
            odds = int(pair["side_price"])
            base, sd = self._total_model()
            p_over = over_probability(base, sd, total_line)
            prob = p_over if side == "Over" else 1 - p_over
            ip = remove_vig(american_to_prob(pair["opp_price"]),
                            american_to_prob(odds))[1]
            e = edge(prob, ip)
            qual = self._quality(elo, game["home"], game["away"])
            if e >= threshold:
                suggestions.append(self._make_pick(game, "total", side, total_line, odds,
                                                   prob, ip, e, qual))
        return suggestions

    def _make_pick(self, game, market: str, team: str, line, odds, prob, ip, e,
                   qual) -> dict:
        cfg = db.get_config()
        cbreak = conf.compute_confidence(
            e, prob, qual, cfg["confidence"],
            cfg["edge"].get("max_edge_display", cfg["edge"]["target"]),
        )
        return {
            "game_id": game["id"], "game": f"{game['away']} @ {game['home']}",
            "slate": game["slate"], "market": market, "team": team, "line": line,
            "odds": int(odds), "model_prob": round(prob, 4),
            "implied_prob": round(ip, 4), "edge": round(e, 4),
            "confidence": cbreak, "units": conf.units_from_confidence(cbreak["score"], cfg),
            "quality_flags": [k for k, v in qual.items() if v is True and k != "games_of_history"],
        }

    def _best_odds(self, game_id: int, market: str, side: str) -> Optional[int]:
        """Highest price for a side across bookmakers."""
        rows = db.query(self.db_path, """
            SELECT MAX(price) AS p FROM odds WHERE game_id=? AND market=? AND LOWER(side)=LOWER(?)""",
                        (game_id, market, side))
        return rows[0]["p"] if rows and rows[0]["p"] is not None else None

    def _best_line(self, game_id: int, market: str, side: str) -> Optional[float]:
        rows = db.query(self.db_path, """
            SELECT line FROM odds WHERE game_id=? AND market=? AND LOWER(side)=LOWER(?) ORDER BY price DESC LIMIT 1""",
                        (game_id, market, side))
        return rows[0]["line"] if rows else None

    def _best_quote(self, game_id: int, market: str, side: str,
                    line: float | None = None):
        """Return price and line from the same bookmaker quote."""
        sql = """SELECT price, line, bookmaker FROM odds
                 WHERE game_id=? AND market=? AND LOWER(side)=LOWER(?)"""
        params: list = [game_id, market, side]
        if line is not None:
            sql += " AND ABS(line - ?) < 0.0001"
            params.append(line)
        sql += " ORDER BY price DESC, captured_utc DESC LIMIT 1"
        rows = db.query(self.db_path, sql, tuple(params))
        return rows[0] if rows else None

    def _best_pair(self, game_id: int, market: str, side: str, opponent: str):
        """Best side quote with its opposing quote from the same book and line."""
        line_match = "1=1"
        if market == "spread":
            line_match = "ABS(a.line + b.line) < 0.0001"
        elif market == "total":
            line_match = "ABS(a.line - b.line) < 0.0001"
        rows = db.query(self.db_path, f"""
            SELECT a.price AS side_price, a.line AS side_line,
                   b.price AS opp_price, b.line AS opp_line,
                   a.bookmaker
            FROM odds a JOIN odds b
              ON b.game_id=a.game_id AND b.market=a.market
             AND b.bookmaker=a.bookmaker
            WHERE a.game_id=? AND a.market=?
              AND LOWER(a.side)=LOWER(?) AND LOWER(b.side)=LOWER(?)
              AND {line_match}
            ORDER BY a.price DESC, b.price DESC,
                     a.captured_utc DESC, b.captured_utc DESC
            LIMIT 1""", (game_id, market, side, opponent))
        return rows[0] if rows else None

    def _total_model(self):
        settings = (db.get_config().get("total_model", {}) or {}).get(self.sport.code, {})
        return (settings.get("base", self.sport.total_base),
                settings.get("sd", self.sport.total_sd))

class NflPicker(Picker):
    """NFL-bound Picker, kept for call sites that predate multi-sport."""

    def __init__(self, db_path=db.DEFAULT_DB, odds_key: str | None = None,
                 eta_groups: Optional[dict] = None):
        super().__init__("nfl", db_path=db_path, odds_key=odds_key,
                         eta_groups=eta_groups)
