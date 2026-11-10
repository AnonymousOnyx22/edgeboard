import datetime as dt

import pytest

from src.data.espn_client import EspnClient
from src.data.models import EloSystem, START_ELO
from src.db import database as db
from src.picker.backtest import record_summary
from src.picker.engine import NflPicker


@pytest.fixture()
def audit_db(tmp_path):
    path = tmp_path / "audit.sqlite"
    db.init_db(path)
    return path


def test_espn_status_is_normalized():
    client = EspnClient()
    assert client.status({"id": "1", "status": {"type": {"state": "pre"}}})["state"] == "scheduled"
    assert client.status({"id": "1", "status": {"type": {"state": "in"}}})["state"] == "in_progress"
    final = client.status({"id": "1", "status": {"type": {"state": "post", "completed": True}}})
    assert final["state"] == "final"


def test_elo_is_zero_sum_and_ties_are_half_a_win():
    elo = EloSystem()
    elo.update("HOME", "AWAY", 0)
    assert elo.get("HOME") + elo.get("AWAY") == pytest.approx(2 * START_ELO)
    assert elo.get("HOME") < START_ELO  # a home tie underperforms home advantage
    assert elo.get("AWAY") > START_ELO


def test_espn_scores_are_assigned_by_home_away_not_order(audit_db):
    class FakeEspn(EspnClient):
        def events(self, sport, day):
            return [{"id": "event-1", "date": "2026-09-20T17:00:00Z"}]

        def competition(self, event):
            return {"id": "comp-1", "competitors": [
                {"order": 0, "homeAway": "home"},
                {"order": 1, "homeAway": "away"},
            ]}

        def team_identity(self, comp):
            return [
                {"home_away": "home", "espn_id": "H", "name": "Home", "abbrev": "HOM"},
                {"home_away": "away", "espn_id": "A", "name": "Away", "abbrev": "AWY"},
            ]

        def status(self, comp):
            return {"state": "final", "completed": True}

        def competitor_scores(self, comp):
            return {0: 21, 1: 17}

    FakeEspn().upsert_games("nfl", dt.date(2026, 9, 20), audit_db)
    game = db.query(audit_db, "SELECT status, away_score, home_score FROM games")[0]
    assert dict(game) == {"status": "final", "away_score": 17, "home_score": 21}


def test_spread_and_total_use_points_lines_not_prices(audit_db):
    with db.connect(audit_db) as conn:
        conn.execute("INSERT INTO teams (id,sport_id,league_key,name,abbrev) VALUES (1,1,'A','Away','AWY')")
        conn.execute("INSERT INTO teams (id,sport_id,league_key,name,abbrev) VALUES (2,1,'H','Home','HOM')")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,status)
            VALUES (1,1,'G',1,2,'2026-09-20T17:00:00Z','scheduled')""")
        quotes = [
            ("spread", "HOM", -110, -3.5), ("spread", "AWY", -110, 3.5),
            ("total", "Over", -105, 44.5), ("total", "Under", -115, 44.5),
        ]
        for market, side, price, line in quotes:
            conn.execute("""INSERT INTO odds
                (game_id,market,side,price,line,bookmaker,source,captured_utc)
                VALUES (1,?,?,?,?,?,'test','2026-09-19T00:00:00Z')""",
                         (market, side, price, line, "book"))
        conn.commit()

    picker = NflPicker(db_path=audit_db)
    game = {"id": 1, "home": "HOM", "away": "AWY", "slate": "sunday_early"}
    pred = {"home_win_prob": 0.55, "margin_mean": 3.5, "margin_sd": 10.0}
    picks = picker._edges_for_game(game, pred, _EloStub(), -1.0)
    spreads = {p["team"]: p for p in picks if p["market"] == "spread"}
    totals = {p["team"]: p for p in picks if p["market"] == "total"}
    assert spreads["HOM"]["line"] == -3.5
    assert spreads["AWY"]["line"] == 3.5
    assert spreads["HOM"]["model_prob"] == pytest.approx(0.5)
    assert spreads["AWY"]["model_prob"] == pytest.approx(0.5)
    assert totals["Over"]["line"] == totals["Under"]["line"] == 44.5
    assert totals["Over"]["odds"] == -105
    assert totals["Under"]["odds"] == -115


def test_odds_api_team_names_are_normalized_to_abbreviations(audit_db):
    # The kickoff has to stay in the future: a price is only recorded for a
    # game that has not started, so a hard-coded date quietly turns this test
    # into a no-op the day it passes.
    kickoff = ((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3))
               .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    with db.connect(audit_db) as conn:
        conn.execute("INSERT INTO teams (id,sport_id,league_key,name,abbrev) VALUES (1,1,'A','Away Team','AWY')")
        conn.execute("INSERT INTO teams (id,sport_id,league_key,name,abbrev) VALUES (2,1,'H','Home Team','HOM')")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,game_date,status)
            VALUES (1,1,'G',1,2,?,?,'scheduled')""",
            (kickoff, kickoff[:10]))
        conn.commit()
    payload = [{
        "home_team": "Home Team", "away_team": "Away Team",
        "commence_time": kickoff,
        "bookmakers": [{"key": "book", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home Team", "price": -120},
            {"name": "Away Team", "price": 105},
        ]}]}],
    }]
    NflPicker(db_path=audit_db)._store_odds_api(payload)
    sides = {r["side"] for r in db.query(audit_db, "SELECT side FROM odds")}
    assert sides == {"HOM", "AWY"}


def test_a_price_is_never_recorded_for_a_game_already_under_way(audit_db):
    """Live prices look like enormous edges to a model that cannot see the
    score, so a started game must not take a pregame quote."""
    started = ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2))
               .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    with db.connect(audit_db) as conn:
        conn.execute("INSERT INTO teams (id,sport_id,league_key,name,abbrev) VALUES (1,1,'A','Away Team','AWY')")
        conn.execute("INSERT INTO teams (id,sport_id,league_key,name,abbrev) VALUES (2,1,'H','Home Team','HOM')")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,game_date,status)
            VALUES (1,1,'G',1,2,?,?,'in_progress')""", (started, started[:10]))
        conn.commit()
    payload = [{
        "home_team": "Home Team", "away_team": "Away Team",
        "commence_time": started,
        "bookmakers": [{"key": "book", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home Team", "price": -120},
            {"name": "Away Team", "price": 105},
        ]}]}],
    }]
    NflPicker(db_path=audit_db)._store_odds_api(payload)
    assert db.query(audit_db, "SELECT side FROM odds") == []


def test_record_roi_uses_units_staked(audit_db):
    with db.connect(audit_db) as conn:
        conn.execute("""INSERT INTO picks
            (id,sport_id,nature,source,status,units_final,created_utc)
            VALUES (1,1,'straight','manual','graded',3,'2026-09-19T00:00:00Z')""")
        conn.execute("""INSERT INTO results (pick_id,result,units_won,graded_utc)
            VALUES (1,'W',1.5,'2026-09-20T00:00:00Z')""")
        conn.commit()
    assert record_summary(audit_db)["overall"]["roi"] == pytest.approx(0.5)


class _EloStub:
    def games(self, team):
        return 10
