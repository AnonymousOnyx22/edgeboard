import datetime as dt
import time

from src.db import database as db
from src.picker.scheduler import _schedule_dates


def test_schedule_dates_revisits_ungraded_pick_after_midnight(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    db.init_db(path)
    today = dt.date(2099, 9, 20)
    yesterday = today - dt.timedelta(days=1)

    with db.connect(path) as conn:
        conn.execute("""INSERT INTO teams
            (id,sport_id,league_key,name,abbrev) VALUES
            (1,1,'A','Away','AWY'),(2,1,'H','Home','HOM')""")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,game_date,status)
            VALUES (1,1,'G1',1,2,? ,?,'in_progress')""",
            (f"{yesterday.isoformat()}T23:00:00Z", yesterday.isoformat()))
        conn.execute("""INSERT INTO picks
            (id,sport_id,nature,source,status,created_utc)
            VALUES (1,1,'straight','model','approved',?)""",
            (f"{yesterday.isoformat()}T20:00:00Z",))
        conn.execute("""INSERT INTO pick_legs
            (pick_id,ord,game_id,market,team,odds_american)
            VALUES (1,0,1,'h2h','HOM',-110)""")
        conn.commit()

    assert _schedule_dates("nfl", today, 2, path) == [
        yesterday, today, today + dt.timedelta(days=1)
    ]


def test_schedule_dates_does_not_revisit_graded_pick(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    db.init_db(path)
    today = dt.date(2099, 9, 20)
    yesterday = today - dt.timedelta(days=1)

    with db.connect(path) as conn:
        conn.execute("""INSERT INTO teams
            (id,sport_id,league_key,name,abbrev) VALUES
            (1,1,'A','Away','AWY'),(2,1,'H','Home','HOM')""")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,game_date,status)
            VALUES (1,1,'G1',1,2,? ,?,'scheduled')""",
            (f"{yesterday.isoformat()}T23:00:00Z", yesterday.isoformat()))
        conn.execute("""INSERT INTO picks
            (id,sport_id,nature,source,status,created_utc)
            VALUES (1,1,'straight','model','graded',?)""",
            (f"{yesterday.isoformat()}T20:00:00Z",))
        conn.execute("""INSERT INTO pick_legs
            (pick_id,ord,game_id,market,team,odds_american)
            VALUES (1,0,1,'h2h','HOM',-110)""")
        conn.commit()

    assert _schedule_dates("nfl", today, 1, path) == [today]


def test_schedule_dates_revisits_game_stuck_in_progress(tmp_path):
    """A game left live while the app was closed is resynced, picks or not."""
    path = tmp_path / "scheduler.sqlite"
    db.init_db(path)
    today = dt.date(2099, 9, 20)
    old = today - dt.timedelta(days=6)

    with db.connect(path) as conn:
        conn.execute("""INSERT INTO teams
            (id,sport_id,league_key,name,abbrev) VALUES
            (1,1,'A','Away','AWY'),(2,1,'H','Home','HOM')""")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,game_date,status)
            VALUES (1,1,'G1',1,2,? ,?,'in_progress')""",
            (f"{old.isoformat()}T23:00:00Z", old.isoformat()))
        conn.commit()

    assert _schedule_dates("nfl", today, 1, path) == [old, today]


def test_board_reads_games_concurrently_and_survives_a_bad_one(monkeypatch):
    """Games are independent, so one unreachable matchup must not lose the
    others - and the players from every good game must still arrive."""
    import threading
    from src.picker import board as board_mod
    from src.picker import players

    class Card:
        def __init__(self, n):
            self.game_id, self.matchup = n, f"G{n}"
            self.home, self.away, self.sport = "HOM", "AWY", "mlb"
            self.bettable, self.started, self.status = True, False, "scheduled"
            self.kickoff = ""
        def kickoff_local(self, tz=None): return ""

    cards = [Card(n) for n in range(5)]
    live = set()
    peak = [0]
    lock = threading.Lock()

    class Sample:
        def __init__(self, n):
            self.athlete_id, self.name = str(n), f"P{n}"
            self.position, self.team, self.opponent = "OF", "HOM", "AWY"
            self.sport, self.injury_status = "mlb", ""
            self.recent_games, self.vs_games = [], []
        def stat_keys(self): return []

    class Deep:
        def __init__(self, n):
            self.priced, self.props, self.props_note = False, [], ""
            self._n = n
        def players(self):
            return [Sample(self._n)]

    def fake_deep(card, **kwargs):
        with lock:
            live.add(card.game_id)
            peak[0] = max(peak[0], len(live))
        time.sleep(0.05)
        with lock:
            live.discard(card.game_id)
        if card.game_id == 2:
            raise RuntimeError("ESPN unreachable")
        return Deep(card.game_id)

    monkeypatch.setattr(board_mod, "upcoming_board", lambda *a, **k: cards)
    monkeypatch.setattr(board_mod, "deep_matchup", fake_deep)

    board = players.build_player_board("mlb", days=1, max_games=5)

    assert peak[0] > 1, "games were read one at a time"
    assert board.games_scanned == 4, "the four good games must all be read"
    assert len(board.players) == 4
    assert "unreachable" in board.note


def test_upcoming_board_is_reused_until_the_data_moves(tmp_path, monkeypatch):
    """Every page needs a board and some need it twice, so building one has to
    be paid for once - but never after a game or a price has changed."""
    from src.picker import board as board_mod

    path = tmp_path / "board.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("""INSERT INTO teams (id,sport_id,league_key,name,abbrev)
                        VALUES (1,1,'A','Away','AWY'),(2,1,'H','Home','HOM')""")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,game_date,status)
            VALUES (1,1,'G1',1,2,'2099-09-20T17:00:00Z','2099-09-20','scheduled')""")
        conn.execute("""INSERT INTO game_history
            (sport,game_key,season,week,game_date,home_team,away_team,
             home_score,away_score,spread_line,total_line,source)
            VALUES ('nfl','H1',2098,1,'2098-09-01','HOM','AWY',21,14,3.0,45.0,'nflverse')""")
        conn.commit()

    board_mod.clear_board_cache()
    builds = {"n": 0}
    real_day_board = board_mod.day_board

    def counting(*args, **kwargs):
        builds["n"] += 1
        return real_day_board(*args, **kwargs)

    monkeypatch.setattr(board_mod, "day_board", counting)
    day = dt.date(2099, 9, 20)

    board_mod.upcoming_board("nfl", days=1, start=day, db_path=path)
    after_first = builds["n"]
    assert after_first > 0

    board_mod.upcoming_board("nfl", days=1, start=day, db_path=path)
    assert builds["n"] == after_first, "a second board must come from cache"

    # A new price has to invalidate it, or the page shows a stale number.
    with db.connect(path) as conn:
        conn.execute("""INSERT INTO odds (game_id,market,side,price,line,
                        bookmaker,source,captured_utc)
                        VALUES (1,'totals','over',-110,44.5,'b','t','2099-01-01')""")
        conn.commit()

    board_mod.upcoming_board("nfl", days=1, start=day, db_path=path)
    assert builds["n"] > after_first, "a new price must rebuild the board"


def test_a_cached_board_cannot_be_mutated_by_its_caller(tmp_path):
    """Callers filter and slice the list they get, which must not edit the
    copy the next caller receives."""
    from src.picker import board as board_mod

    path = tmp_path / "board2.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("""INSERT INTO game_history
            (sport,game_key,season,week,game_date,home_team,away_team,
             home_score,away_score,spread_line,total_line,source)
            VALUES ('nfl','H1',2098,1,'2098-09-01','HOM','AWY',21,14,3.0,45.0,'nflverse')""")
        conn.commit()
    board_mod.clear_board_cache()
    day = dt.date(2099, 9, 20)
    first = board_mod.upcoming_board("nfl", days=1, start=day, db_path=path)
    first.append("not a card")
    second = board_mod.upcoming_board("nfl", days=1, start=day, db_path=path)
    assert "not a card" not in second
