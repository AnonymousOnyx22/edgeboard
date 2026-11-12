"""Game lines when the only prices on file are ESPN's embedded backup.

Without an Odds API key every price is stored under bookmaker "ESPN". The
FanDuel allowlist used to filter all of them out, so every game had no
candidates and the autopicker committed nothing, cycle after cycle.
"""
import pytest

from src.data import odds_client
from src.db import database as db
from src.picker import board


@pytest.fixture()
def priced_db(tmp_path, monkeypatch):
    monkeypatch.setattr(odds_client, "allowed_books", lambda config=None: ["fanduel"])
    path = tmp_path / "backup.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO teams (id,sport_id,league_key,name,abbrev) VALUES (1,1,'A','Away','AWY')")
        conn.execute("INSERT INTO teams (id,sport_id,league_key,name,abbrev) VALUES (2,1,'H','Home','HOM')")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,status)
            VALUES (1,1,'G',1,2,'2026-09-20T17:00:00Z','scheduled')""")
    return path


def _quote(path, side, price, book):
    with db.connect(path) as conn:
        conn.execute("""INSERT INTO odds
            (game_id,market,side,price,line,bookmaker,source,captured_utc)
            VALUES (1,'h2h',?,?,NULL,?,'test','2026-09-19T00:00:00Z')""",
                     (side, price, book))


def test_espn_backup_prices_a_game_no_allowed_book_has(priced_db):
    _quote(priced_db, "HOM", -150, "ESPN")
    _quote(priced_db, "AWY", +130, "ESPN")
    pair = board.best_pair(1, "h2h", "HOM", "AWY", priced_db)
    assert pair is not None
    assert pair["bookmaker"] == "ESPN" and pair["side_price"] == -150


def test_an_allowed_book_beats_a_better_espn_price(priced_db):
    _quote(priced_db, "HOM", -150, "FanDuel")
    _quote(priced_db, "AWY", +130, "FanDuel")
    _quote(priced_db, "HOM", -120, "ESPN")
    _quote(priced_db, "AWY", +100, "ESPN")
    pair = board.best_pair(1, "h2h", "HOM", "AWY", priced_db)
    assert pair["bookmaker"] == "FanDuel" and pair["side_price"] == -150


def test_offshore_books_still_never_price_a_game(priced_db):
    _quote(priced_db, "HOM", -110, "bovada")
    _quote(priced_db, "AWY", -110, "bovada")
    assert board.best_pair(1, "h2h", "HOM", "AWY", priced_db) is None
