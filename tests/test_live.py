"""Live game state, and the books a price is allowed to come from."""
import datetime as dt

import pytest

from src.data.odds_client import allowed_books
from src.db import database as db
from src.picker import live


def iso(delta_hours: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(hours=delta_hours)).isoformat()


@pytest.fixture()
def live_db(tmp_path):
    path = tmp_path / "live.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("""INSERT INTO teams (id,sport_id,league_key,name,abbrev)
                        VALUES (1,1,'A','Away','AWY'),(2,1,'H','Home','HOM')""")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,status)
            VALUES
            (1,1,'E1',1,2,?,'scheduled'),   -- started, worth reading
            (2,1,'E2',1,2,?,'scheduled'),   -- not started yet
            (3,1,'E3',1,2,?,'final'),       -- done
            (4,1,'E4',1,2,?,'scheduled')    -- started days ago, never closed
            """, (iso(-1), iso(+3), iso(-4), iso(-40)))
        conn.commit()
    return path


def test_only_games_worth_reading_are_due(live_db):
    due = {row["id"] for row in live.due_games(live_db)}
    assert due == {1}, "a future, a finished and an abandoned game must be skipped"


def test_an_old_game_with_an_open_pick_is_still_settled(live_db):
    """The laptop slept through the game: the first page load closes it."""
    with db.connect(live_db) as conn:
        conn.execute("""INSERT INTO picks (id,sport_id,nature,source,status,created_utc)
                        VALUES (1,1,'straight','model','approved',?)""", (iso(-41),))
        conn.execute("""INSERT INTO pick_legs (pick_id,ord,game_id,market,team,odds_american)
                        VALUES (1,0,4,'h2h','HOM',-110)""")
        conn.commit()
    assert {row["id"] for row in live.due_games(live_db)} == {1, 4}

    recent = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(seconds=live.MIN_INTERVAL + 30)).isoformat()
    db.execute(live_db, "UPDATE games SET live_utc=? WHERE id=4", (recent,))
    assert {row["id"] for row in live.due_games(live_db)} == {1}, \
        "an old game is read every few minutes, not every page load"


def test_a_freshly_read_game_is_not_read_again(live_db):
    db.execute(live_db, "UPDATE games SET live_utc=? WHERE id=1",
               (dt.datetime.now(dt.timezone.utc).isoformat(),))
    assert live.due_games(live_db) == []


def test_a_stale_read_becomes_due_again(live_db):
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(seconds=live.MIN_INTERVAL + 30)).isoformat()
    db.execute(live_db, "UPDATE games SET live_utc=? WHERE id=1", (old,))
    assert [row["id"] for row in live.due_games(live_db)] == [1]


def test_describe_reads_a_game_in_progress():
    out = live.describe({"game_status": "in_progress", "status_detail": "Top 9th",
                         "away_score": 8, "home_score": 0,
                         "away": "MIN", "home": "LAA"})
    assert out == {"live": True, "label": "Live", "detail": "Top 9th",
                   "score": "MIN 8 - 0 LAA"}


def test_describe_reads_a_finished_game():
    out = live.describe({"game_status": "final", "status_detail": "",
                         "away_score": 3, "home_score": 4,
                         "away": "SF", "home": "LAD"})
    assert out["label"] == "Final" and out["live"] is False


def test_describe_says_nothing_before_a_game_starts():
    assert live.describe({"game_status": "scheduled", "status_detail": None,
                          "away_score": None, "home_score": None,
                          "away": "SF", "home": "LAD"}) is None


def test_a_game_with_no_score_yet_still_reports_live():
    out = live.describe({"game_status": "in_progress", "status_detail": "1st",
                         "away_score": None, "home_score": None,
                         "away": "SF", "home": "LAD"})
    assert out["live"] is True and out["score"] == ""


# ------------------------------------------------------------------- books
def test_offshore_books_are_not_quoted():
    """The "us" region returns books nobody in Ontario can bet. Pricing
    against them invents edges that cannot be taken."""
    books = allowed_books()
    assert books, "an empty allowlist would let every book through"
    for offshore in ("bovada", "lowvig", "betonlineag", "mybookieag", "betus"):
        assert offshore not in books


def test_picks_are_priced_at_fanduel():
    assert allowed_books() == ["fanduel"]


def test_an_empty_setting_allows_every_book():
    assert allowed_books({"odds": {"bookmakers": None}}) == []
    assert allowed_books({"odds": {"bookmakers": []}}) == []


def test_book_keys_are_compared_case_insensitively():
    assert allowed_books({"odds": {"bookmakers": ["DraftKings", " FanDuel "]}}) \
        == ["draftkings", "fanduel"]


def test_a_finished_game_is_graded_without_being_asked(live_db, monkeypatch):
    """Grading used to wait for the hourly cycle or a button. A final score is
    the only thing it was ever waiting for, so it happens on the spot."""
    from src.picker import live as live_mod

    calls = {"n": 0}

    def fake_read(row, client=None):
        return {"game_id": row["id"], "state": "final", "detail": "",
                "away_score": 3, "home_score": 5}

    def fake_grade(path, *a, **k):
        calls["n"] += 1
        return [1]

    monkeypatch.setattr(live_mod, "read_game", fake_read)
    monkeypatch.setattr("src.picker.grader.grade_all", fake_grade)

    out = live_mod.refresh(live_db)
    assert out["finished"] == 1
    assert out["graded"] == 1
    assert calls["n"] == 1


def test_a_game_still_running_does_not_trigger_grading(live_db, monkeypatch):
    from src.picker import live as live_mod

    calls = {"n": 0}
    monkeypatch.setattr(live_mod, "read_game", lambda row, client=None: {
        "game_id": row["id"], "state": "in_progress", "detail": "Top 5th",
        "away_score": 1, "home_score": 0})
    monkeypatch.setattr("src.picker.grader.grade_all",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or [])

    out = live_mod.refresh(live_db)
    assert out["live"] == 1 and out["graded"] == 0
    assert calls["n"] == 0


def test_a_grading_failure_does_not_lose_the_score(live_db, monkeypatch):
    """The score is what matters; a grader that throws must not roll it back."""
    from src.picker import live as live_mod

    monkeypatch.setattr(live_mod, "read_game", lambda row, client=None: {
        "game_id": row["id"], "state": "final", "detail": "",
        "away_score": 7, "home_score": 2})

    def boom(*a, **k):
        raise RuntimeError("grader down")

    monkeypatch.setattr("src.picker.grader.grade_all", boom)
    out = live_mod.refresh(live_db)
    assert out["finished"] == 1 and out["graded"] == 0
    row = db.query(live_db, "SELECT status, away_score FROM games WHERE id=1")[0]
    assert row["status"] == "final" and row["away_score"] == 7
