import datetime as dt

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
            VALUES (1,1,'G1',1,2,? ,?,'in_progress')""",
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
