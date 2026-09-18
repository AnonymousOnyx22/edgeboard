import pytest

from src.db import database as db
from src.picker import players
from src.web.app import create_app


@pytest.fixture()
def client(tmp_path):
    path = tmp_path / "web.sqlite"
    db.init_db(path)
    app = create_app(path)
    app.testing = True
    return app.test_client()


@pytest.mark.parametrize("path, heading", [
    ("/", b"Your edge"),
    ("/games", b"games"),
    ("/model-picks", b"Model picks"),
    ("/performance", b"Performance"),
    ("/settings", b"Settings"),
    ("/players", b"Players"),
    ("/teams", b"Teams"),
    ("/players?team=BUF", b"Players"),
])
def test_pages_render(client, path, heading):
    response = client.get(path)
    assert response.status_code == 200
    assert heading in response.data
    assert b"Streamlit" not in response.data
    assert b"Edgeboard" in response.data


def test_stylesheet_is_served(client):
    """Assert the contract, not the palette - colours are meant to change."""
    response = client.get("/static/app.css")
    assert response.status_code == 200
    for token in (b"--bg:", b"--ink:", b"--ease-out:", b"--sidebar:"):
        assert token in response.data


def test_settled_results_have_signed_semantic_styles(client):
    css = client.get("/static/app.css").data
    for token in (b"--win:", b"--loss:", b".result-win", b".result-loss"):
        assert token in css

    for name in ("home.html", "model_picks.html", "performance.html"):
        source, _, _ = client.application.jinja_loader.get_source(
            client.application.jinja_env, name)
        assert "result-win" in source
        assert "result-loss" in source


def test_team_filter_wraps_and_is_capped(client):
    """Chips run across the page and wrap, but only three rows deep - beyond
    that the filter scrolls rather than pushing the board below the fold."""
    css = client.get("/static/app.css").get_data(as_text=True)
    assert ".team-filter" in css
    rule = css.split(".team-filter {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap" in rule
    assert "max-height" in rule
    assert "overflow-y: auto" in rule


def test_every_team_is_still_reachable_in_the_filter(client):
    """Scrolling must not mean dropping teams: all of them stay in the markup."""
    from src.picker import players as players_mod

    body = client.get("/players?sport=nfl").get_data(as_text=True)
    options = players_mod.team_options("nfl", client.application.config["DB_PATH"])
    for item in options:
        assert item["abbrev"] in body


def test_the_sidebar_nav_rule_does_not_reach_other_navs(client):
    """The team filter is a <nav> too, and an unscoped rule ran it down the
    page in a column instead of across it."""
    css = client.get("/static/app.css").get_data(as_text=True)
    # An unscoped rule starts at the beginning of its own line.
    for line in css.splitlines():
        assert not line.startswith("nav {"), line
    rule = css.split(".team-filter {", 1)[1].split("}", 1)[0]
    assert "flex-direction: row" in rule


def test_hovering_a_name_adds_colour(client):
    css = client.get("/static/app.css").get_data(as_text=True)
    assert "--accent:" in css
    assert ".player-link:hover b" in css
    assert ".team-link:hover b" in css


def test_script_is_served(client):
    response = client.get("/static/app.js")
    assert response.status_code == 200
    assert b"IntersectionObserver" in response.data


def test_navigation_collapse_is_wired(client):
    """The rail's toggle and the mobile drawer both need their hooks present."""
    body = client.get("/").data
    assert b'id="navToggle"' in body
    assert b'id="menuButton"' in body
    assert b'id="sidebar"' in body
    assert b'id="scrim"' in body


def test_reveal_styles_only_apply_when_scripting_is_available(client):
    """Content must never be left invisible if app.js fails to run."""
    assert b"document.documentElement.classList.add('js')" in client.get("/").data
    assert b"html.js .metric" in client.get("/static/app.css").data


def test_jargon_is_not_user_facing(client):
    """'Slate' is insider language; the UI says games."""
    for path in ("/", "/games", "/model-picks", "/players", "/teams"):
        assert b"slate" not in client.get(path).data.lower()


def test_the_full_ledger_folds_away(client):
    """The ledger grows without bound, so it must not be forced open."""
    body = client.get("/model-picks").data
    assert b'<details class="section-fold">' in body
    assert b"Every model pick" in body
    # No `open` attribute: it starts collapsed.
    assert b'class="section-fold" open' not in body


def test_review_workflow_is_not_user_facing(client):
    body = client.get("/games").data.lower() + client.get("/").data.lower()
    assert b"awaiting review" not in body
    assert b"track</button>" not in body
    assert b"pass</button>" not in body


def test_posts_require_csrf(client):
    assert client.post("/grade").status_code == 400
    client.get("/performance")
    with client.session_transaction() as state:
        token = state["_csrf_token"]
    assert client.post("/grade", data={"_csrf_token": token}).status_code == 302


def test_unknown_player_is_404(client):
    assert client.get("/player/nfl/999999999").status_code == 404


def test_unknown_team_is_404(client):
    assert client.get("/teams/nfl/NOPE").status_code == 404


def test_player_team_filter_reuses_ready_full_board(client, monkeypatch):
    views = [
        players.PlayerView("1", "Josh Allen", "QB", "BUF", "MIA", "nfl"),
        players.PlayerView("2", "Tua Tagovailoa", "QB", "MIA", "BUF", "nfl"),
    ]

    class FullBoard:
        players = views

        def by_team(self, team):
            return [view for view in self.players if view.team == team]

    full = {"status": "ready", "at": 1, "board": FullBoard(),
            "progress": 1.0}

    class Cache:
        def get(self, sport, days, day, team=None):
            return full if team is None else None

        def is_fresh(self, entry):
            return bool(entry and entry.get("status") == "ready")

        def start(self, *args, **kwargs):
            raise AssertionError("filtering a ready board must not rebuild it")

    monkeypatch.setattr(players, "CACHE", Cache())
    monkeypatch.setattr(players, "team_options", lambda sport, path: [
        {"abbrev": "BUF", "name": "Buffalo Bills"},
        {"abbrev": "MIA", "name": "Miami Dolphins"},
    ])
    response = client.get("/players?sport=nfl&teams=BUF,MIA")
    assert response.status_code == 200
    assert response.data.index(b"Buffalo Bills") < response.data.index(b"Miami Dolphins")
    assert b"Josh Allen" in response.data
    assert b"Tua Tagovailoa" in response.data
    assert b"2 selected" in response.data


def test_player_pages_markup_has_headshots(client):
    """The whole page needs an empty-DB-empty state, and the stylesheet
    must carry the player-board pieces (headshot sizes, sport switch)."""
    assert client.get("/players").status_code == 200
    css = client.get("/static/app.css").data
    assert b".headshot.lg" in css
    assert b".seg-row" in css
    assert b".player-link" in css


def test_form_column_shows_games_against_this_opponent(client, monkeypatch):
    """The form a player page sells is head-to-head: what they did against the
    club they are about to face, not against the league at large."""
    from src.picker.props import Projection, PropCandidate

    bet = PropCandidate(
        athlete_id="1", player="Josh Allen", position="QB", team="BUF",
        opponent="MIA", stat_key="passingYards", line=249.5, probability=0.63,
        projection=Projection("passingYards", 268.0, 48.0, 7.4, 9, 3, 281.0,
                              272.0, "nfl"),
        hit_vs_opponent=(3, 3), hit_recent=(6, 9), game_id=1, sport="nfl")

    faced = players.PlayerView("1", "Josh Allen", "QB", "BUF", "MIA", "nfl",
                               bets=[bet])
    # Overall form and head-to-head form deliberately differ.
    faced.recent = [{"passingYards": v} for v in (111, 112, 113, 114, 115)]
    faced.vs_games = [{"passingYards": v} for v in (301, 302, 303)]

    never = players.PlayerView("2", "Rookie Back", "RB", "BUF", "MIA", "nfl",
                               bets=[bet])
    never.recent = [{"passingYards": 90}]
    never.vs_games = []

    class FullBoard:
        players = [faced, never]

        def by_team(self, team):
            return list(self.players)

    entry = {"status": "ready", "at": 1, "board": FullBoard(), "progress": 1.0}

    class Cache:
        def get(self, sport, days, day, team=None):
            return entry

        def is_fresh(self, e):
            return True

        def start(self, *args, **kwargs):
            return entry

    monkeypatch.setattr(players, "CACHE", Cache())
    monkeypatch.setattr(players, "team_options", lambda sport, path: [
        {"abbrev": "BUF", "name": "Buffalo Bills"}])

    body = client.get("/players?sport=nfl").get_data(as_text=True)
    assert "Last 5 vs opponent" in body
    # The head-to-head numbers are shown...
    assert "301 · 302 · 303" in body
    # ...and the overall ones are not passed off as head-to-head.
    assert "111 · 112" not in body
    # A player who has never faced them is said so, not quietly filled in.
    assert "first meeting" in body


def test_h2h_form_reads_only_prior_meetings():
    view = players.PlayerView("1", "A", "QB", "BUF", "MIA", "nfl")
    view.recent = [{"passingYards": 100}, {"passingYards": 200}]
    view.vs_games = [{"passingYards": 300}, {"passingYards": 400}]
    assert view.h2h_form("passingYards", 5) == "300 · 400"
    assert view.recent_form("passingYards", 5) == "100 · 200"
    assert view.h2h_count("passingYards") == 2
    assert players.PlayerView("2", "B", "QB", "BUF", "MIA", "nfl") \
        .h2h_form("passingYards") == ""


def test_tickets_page_renders(client):
    response = client.get("/tickets")
    assert response.status_code == 200
    assert b"Today\xe2\x80\x99s games" in response.data


def test_ticket_carries_when_where_and_both_clubs(client, monkeypatch):
    """A ticket is only useful if it says who, when and where without a click."""
    from src.picker import board as board_mod

    card = board_mod.GameCard(
        game_id=7, home="CIN", away="CHC",
        start_utc="2099-09-18T22:40:00Z", kickoff_window="", status="scheduled",
        sport="mlb", game_date="2099-09-18",
        venue="Great American Ball Park", venue_city="Cincinnati, Ohio")

    monkeypatch.setattr(board_mod, "upcoming_board",
                        lambda *a, **k: [card])
    body = client.get("/tickets?sport=mlb&date=2099-09-18").get_data(as_text=True)
    assert "Great American Ball Park" in body
    assert "Cincinnati, Ohio" in body
    assert ">CHC<" in body and ">CIN<" in body
    assert "Fri, Sep 18" in body
    assert "/game/mlb/7" in body


def test_a_started_game_is_marked_on_its_ticket(client, monkeypatch):
    from src.picker import board as board_mod

    card = board_mod.GameCard(
        game_id=8, home="CIN", away="CHC",
        start_utc="2020-09-18T22:40:00Z", kickoff_window="", status="final",
        sport="mlb", game_date="2020-09-18", venue="Park")
    monkeypatch.setattr(board_mod, "upcoming_board", lambda *a, **k: [card])
    body = client.get("/tickets?sport=mlb&date=2020-09-18").get_data(as_text=True)
    assert "is-started" in body
    assert "Final" in body


def test_unknown_game_is_404(client):
    assert client.get("/game/mlb/99999999").status_code == 404
    assert client.get("/game/nope/1").status_code == 404
