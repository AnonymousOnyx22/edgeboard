import datetime as dt

import pandas as pd
import pytest
import requests

from src.data.odds_client import OddsAPIError, OddsClient
from src.db import database as db
from src.picker import actions, board, h2h
from src.picker.engine import Picker
from src.picker.grader import grade_pick, ungraded_picks
from src.picker.parlay_builder import Leg, build_parlay


@pytest.fixture()
def backend_db(tmp_path):
    path = tmp_path / "backend.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("""INSERT INTO teams
            (id,sport_id,league_key,name,abbrev) VALUES
            (1,1,'A','Away','AWY'),(2,1,'H','Home','HOM'),
            (3,1,'C','Club C','CLB'),(4,1,'D','Club D','DST')""")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,game_date,status)
            VALUES
            (1,1,'G1',1,2,'2099-09-20T17:00:00Z','2099-09-20','scheduled'),
            (2,1,'G2',3,4,'2099-09-21T17:00:00Z','2099-09-21','scheduled')""")
        conn.commit()
    return path


def test_historical_inputs_exclude_games_on_or_after_selected_day():
    history = pd.DataFrame([
        {"gameday": "2024-01-01", "season": 2023, "home_team": "HOM",
         "away_team": "AWY", "home_score": 20, "away_score": 10},
        {"gameday": "2024-10-01", "season": 2024, "home_team": "AWY",
         "away_team": "HOM", "home_score": 30, "away_score": 7},
    ])
    cutoff = dt.date(2024, 6, 1)
    prior = board.history_before(history, cutoff)
    assert list(prior["gameday"]) == ["2024-01-01"]
    series = h2h.team_series("HOM", "AWY", history, as_of=cutoff)
    assert [meeting.date for meeting in series.meetings] == ["2024-01-01"]


def test_suggest_never_models_in_progress_games(backend_db, monkeypatch):
    db.execute(backend_db, "UPDATE games SET status='in_progress' WHERE id=2")
    picker = Picker("nfl", db_path=backend_db)
    monkeypatch.setattr(picker, "sync_games", lambda day: 2)
    monkeypatch.setattr(picker, "sync_odds", lambda day=None: {"source": "test", "games": 2})
    monkeypatch.setattr(picker, "ensure_elo", lambda day: object())
    monkeypatch.setattr(picker, "_edges_for_game",
                        lambda game, prediction, elo, threshold: [game["id"]])
    monkeypatch.setattr(picker, "predict", lambda home, away, elo: {})
    assert picker.suggest(dt.date(2099, 9, 20)) == [1]


def test_network_failure_is_wrapped_for_espn_fallback():
    client = OddsClient(api_key="test")

    class Offline:
        def get(self, *args, **kwargs):
            raise requests.ConnectionError("offline")

    client.session = Offline()
    with pytest.raises(OddsAPIError, match="offline"):
        client.fetch_odds("americanfootball_nfl", force=True)


def test_final_game_without_scores_is_not_gradable(backend_db):
    db.execute(backend_db,
               "UPDATE games SET status='final',away_score=NULL,home_score=NULL WHERE id=1")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with db.connect(backend_db) as conn:
        conn.execute("""INSERT INTO picks
            (id,sport_id,nature,source,status,units_final,created_utc)
            VALUES (1,1,'straight','model','approved',1,?)""", (now,))
        conn.execute("""INSERT INTO pick_legs
            (pick_id,ord,game_id,market,team,odds_american)
            VALUES (1,0,1,'h2h','HOM',-110)""")
        conn.commit()
    assert ungraded_picks(backend_db) == []
    with pytest.raises(ValueError, match="no complete score"):
        grade_pick(1, backend_db)


def test_saved_parlay_keeps_price_snapshots(backend_db):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    legs = [
        Leg(1, "h2h", "HOM", None, -110, 0.60),
        Leg(2, "h2h", "DST", None, +120, 0.55),
    ]
    parlay = build_parlay(legs, min_legs=2, max_legs=6)
    saved = actions.save_model_parlay(parlay, "nfl", backend_db)
    assert saved["created"] is True
    assert len(db.query(backend_db, "SELECT * FROM pick_snapshots")) == 1

    with db.connect(backend_db) as conn:
        for game_id, side, price in ((1, "HOM", -105), (2, "DST", +125)):
            conn.execute("""INSERT INTO odds
                (game_id,market,side,price,line,bookmaker,source,captured_utc)
                VALUES (?,'h2h',?,?,NULL,'book','test',?)""",
                (game_id, side, price, now))
        conn.commit()
    assert actions.snapshot_open_parlays("nfl", backend_db) == 1
    snapshots = db.query(backend_db,
                         "SELECT decimal_odds FROM pick_snapshots ORDER BY id")
    assert len(snapshots) == 2
    assert snapshots[0]["decimal_odds"] != snapshots[1]["decimal_odds"]

    duplicate = actions.save_model_parlay(parlay, "nfl", backend_db)
    assert duplicate["created"] is False
    assert duplicate["pick_id"] == saved["pick_id"]
    watched = actions.tracked_parlays("nfl", backend_db)
    assert len(watched) == 1
    assert len(watched[0]["legs"]) == 2
    assert len(watched[0]["snapshots"]) == 2


def test_odds_refresh_is_atomic_when_provider_data_is_bad(backend_db):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.execute(backend_db, """INSERT INTO odds
        (game_id,market,side,price,line,bookmaker,source,captured_utc)
        VALUES (1,'h2h','HOM',-110,NULL,'old','test',?)""", (now,))
    payload = [{
        "home_team": "Home", "away_team": "Away",
        "commence_time": "2099-09-20T17:00:00Z",
        "bookmakers": [{"key": "book", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": "not-a-price"},
            {"name": "Away", "price": 105},
        ]}]}],
    }]
    with pytest.raises(ValueError):
        Picker("nfl", db_path=backend_db)._store_odds_api(payload)
    rows = db.query(backend_db, "SELECT bookmaker,price FROM odds WHERE game_id=1")
    assert [dict(row) for row in rows] == [{"bookmaker": "old", "price": -110}]
