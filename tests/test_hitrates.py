import pytest

from src.db import database as db
from src.picker import hitrates
from src.picker import players
from src.picker.players import PlayerBoard, PlayerView
from src.picker.props import PropCandidate, Projection


def game(event, value, opponent="MIA", season=2026, date=None, key="points"):
    return {"event_id": str(event), key: value, "opponent": opponent,
            "season": season, "date": date or f"2026-10-{30 - event:02d}"}


# ------------------------------------------------------------------ the maths
def test_windows_count_only_the_newest_games():
    games = [game(i, v) for i, v in enumerate([30, 10, 30, 30, 10, 30, 30])]
    rate = hitrates.window_rate(games, "points", 24.5, "over", 5)
    assert (rate.hits, rate.games) == (3, 5)
    assert hitrates.window_rate(games, "points", 24.5, "over", 20).games == 7


def test_a_missing_stat_is_no_game_not_a_miss():
    games = [game(0, 30), game(1, None), game(2, 30), game(3, None), game(4, 10)]
    rate = hitrates.window_rate(games, "points", 24.5, "over", 3)
    # The window reaches past the two unrecorded games to three real ones.
    assert (rate.hits, rate.games) == (2, 3)


def test_under_counts_the_other_way_and_over_includes_the_line():
    games = [game(0, 25), game(1, 24), game(2, 20)]
    assert hitrates.window_rate(games, "points", 25, "over", 5).hits == 1
    under = hitrates.window_rate(games, "points", 24.5, "under", 5)
    assert (under.hits, under.games) == (2, 3)


def test_head_to_head_only_counts_this_opponent():
    games = [game(0, 30, "BOS"), game(1, 30, "MIA"), game(2, 10, "MIA"),
             game(3, 30, "NYK")]
    rate = hitrates.h2h_rate(games, "points", 24.5, "over", "MIA")
    assert (rate.hits, rate.games) == (1, 2)
    assert hitrates.h2h_rate(games, "points", 24.5, "over", "").games == 0


def test_season_is_the_newest_games_season():
    games = [game(0, 30, season=2026), game(1, 30, season=2026),
             game(2, 10, season=2025)]
    rate = hitrates.season_rate(games, "points", 24.5, "over")
    assert (rate.hits, rate.games) == (2, 2)


def test_grades_need_enough_games():
    assert hitrates.Rate(2, 2).grade == "thin"
    assert hitrates.Rate(4, 5).grade == "strong"
    assert hitrates.Rate(3, 5).grade == "middling"
    assert hitrates.Rate(1, 5).grade == "weak"
    assert hitrates.Rate(0, 0).grade == "none"
    assert hitrates.Rate(4, 5).text == "4/5"


# -------------------------------------------------------------------- the grid
def candidate(line=24.5, side="over", odds=None, edge=None):
    proj = Projection("points", 26.0, 5.0, 8.0, 8, 2, 28.0, 25.0, sport="nba")
    return PropCandidate(
        athlete_id="1", player="Test Player", position="G", team="BOS",
        opponent="MIA", stat_key="points", line=line, probability=0.71,
        projection=proj, hit_vs_opponent=(1, 2), hit_recent=(6, 10),
        sport="nba", side=side, odds=odds, edge=edge,
        bookmaker="fanduel" if odds is not None else "")


def board_with(view):
    board = PlayerBoard(sport="nba", days=7, start="2026-10-31",
                        built_utc="2026-10-31T00:00:00")
    board.players = [view]
    return board


def view_with(bets, recent=None, vs=None):
    return PlayerView(
        athlete_id="1", name="Test Player", position="G", team="BOS",
        opponent="MIA", sport="nba", game_id=5, game="BOS @ MIA",
        kickoff="Sat 8:30 pm ET", recent=recent or [], vs_games=vs or [],
        bets=bets)


def test_rows_wait_for_the_full_log_before_l20_and_season():
    recent = [game(i, 30, "BOS" if i else "MIA") for i in range(10)]
    board = board_with(view_with([candidate()], recent=recent))

    rows = hitrates.build_rows(board)
    assert len(rows) == 1
    row = rows[0]
    assert (row.l10.hits, row.l10.games) == (10, 10)
    assert row.l20 is None and row.season is None
    assert row.bet == "Over 24.5 Points"

    history = {"1": [game(i, 30 if i % 2 else 10) for i in range(25)]}
    full = hitrates.build_rows(board, history)[0]
    assert full.l20.games == 20
    assert full.season.games > 0


# -------------------------------------------------------------------- the page
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from src.web.app import create_app
    from src.web.hitrates_bp import bp

    path = tmp_path / "web.sqlite"
    db.init_db(path)
    app = create_app(path)
    if "hitrates" not in app.blueprints:
        app.register_blueprint(bp)
    app.testing = True
    return app.test_client()


def fake_cache(entry):
    class Cache:
        def get(self, *args, **kwargs):
            return entry

        def is_fresh(self, e):
            return True

        def start(self, *args, **kwargs):
            return entry
    return Cache()


def test_the_grid_renders_priced_and_model_lines(client, monkeypatch):
    recent = [game(i, 30) for i in range(10)]
    board = board_with(view_with(
        [candidate(), candidate(line=4.5, side="under", odds=-120, edge=0.05)],
        recent=recent))
    monkeypatch.setattr(players, "CACHE", fake_cache(
        {"status": "ready", "board": board, "progress": 1.0, "message": ""}))

    class History:
        def get_or_start(self, b):
            return {"status": "ready", "history": {"1": recent}, "progress": 1.0}
    monkeypatch.setattr(hitrates, "HISTORY", History())

    response = client.get("/props?sport=nba")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Hit rates" in body
    assert "Test Player" in body
    assert "Model line" in body          # the unpriced bet says so
    assert "−120" in body           # the priced one shows its odds
    assert "10/10" in body
    assert "/player/nba/1" in body


def test_the_grid_says_when_it_is_still_building(client, monkeypatch):
    monkeypatch.setattr(players, "CACHE", fake_cache(
        {"status": "building", "board": None, "progress": 0.4,
         "message": "Read 2/6 games..."}))
    body = client.get("/props?sport=nba").get_data(as_text=True)
    assert "Reading the" in body
    assert 'http-equiv="refresh"' in body
