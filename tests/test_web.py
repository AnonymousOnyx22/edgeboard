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


def test_nothing_is_held_hidden_waiting_for_a_script_or_a_scroll(client):
    """Hide-until-revealed left whole panels and the chart blank whenever an
    animation stalled. Content is visible from the first paint."""
    assert b"classList.add('js')" not in client.get("/").data
    css = client.get("/static/app.css").get_data(as_text=True)
    assert "html.js" not in css
    assert "data-reveal" not in client.get("/static/app.js").get_data(as_text=True)


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


def test_a_player_bet_is_named_after_the_player(client):
    """A prop stores its market as prop:<stat> and its side in the team
    column. Formatted like a game line that reads "Over PROP:RUSHINGYARDS",
    which says nothing about whose bet it is."""
    from src.web.app import bet_label

    prop = {"market": "prop:rushingYards", "team": "Over", "line": 51.5,
            "player": "Bijan Robinson", "sport_code": "nfl"}
    assert bet_label(prop) == "Bijan Robinson 51.5+ Rush Yds"

    under = dict(prop, team="Under", market="prop:passingYards",
                 line=185.5, player="Cooper Rush")
    assert bet_label(under) == "Cooper Rush under 185.5 Pass Yds"

    # Game lines are untouched.
    assert bet_label({"market": "spread", "team": "ATL", "line": 2.5,
                      "sport_code": "nfl"}) == "ATL SPREAD +2.5"
    assert bet_label({"market": "h2h", "team": "SF", "line": None,
                      "sport_code": "mlb"}) == "SF H2H"


def test_no_page_leaks_the_raw_prop_market_key(client):
    for path in ("/", "/games", "/model-picks", "/performance"):
        body = client.get(path).get_data(as_text=True)
        assert "PROP:" not in body.upper(), path


def test_units_are_shown_in_dollars_too(client):
    """Units are what the model stakes; the money line is the same number at
    the configured unit size."""
    from src.web.app import money, unit_size

    assert unit_size({"units": {"dollars_per_unit": 25}}) == 25.0
    cfg = {"units": {"dollars_per_unit": 10}}
    assert money(3.4, cfg) == "+$34.00"
    assert money(-2.0, cfg) == "-$20.00"
    assert money(1234.5, cfg) == "+$12,345.00"

    # A missing or zero size hides the money entirely rather than showing $0.
    assert money(3.4, {"units": {"dollars_per_unit": 0}}) == ""
    assert money(3.4, {}) == ""
    assert money(None, cfg) == ""
    # Junk in the config must not take a page down.
    assert money(3.4, {"units": {"dollars_per_unit": "abc"}}) == ""


def test_the_money_line_appears_beside_the_unit_figure(client):
    import re
    for path in ("/", "/performance", "/model-picks"):
        body = client.get(path).get_data(as_text=True)
        subs = re.findall(r'class="metric-sub[^"]*">([^<]+)<', body)
        assert subs, f"no money line on {path}"
        assert any("$" in value for value in subs), path


def test_unit_size_is_editable_in_settings(client):
    assert b'name="dollars_per_unit"' in client.get("/settings").data


def seed_pick(path, pick_id, sport_code, game_id, team):
    """One committed model pick on one league, for the ledger tests."""
    with db.connect(path) as conn:
        sport_id = conn.execute("SELECT id FROM sports WHERE code=?",
                                (sport_code,)).fetchone()["id"]
        conn.execute("""INSERT OR IGNORE INTO teams
            (id,sport_id,league_key,name,abbrev) VALUES (?,?,?,?,?)""",
            (game_id * 10, sport_id, f"A{game_id}", f"Away {game_id}", "AWY"))
        conn.execute("""INSERT OR IGNORE INTO teams
            (id,sport_id,league_key,name,abbrev) VALUES (?,?,?,?,?)""",
            (game_id * 10 + 1, sport_id, f"H{game_id}", f"Home {game_id}", "HOM"))
        conn.execute("""INSERT OR IGNORE INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,game_date,status)
            VALUES (?,?,?,?,?,'2099-09-20T17:00:00Z','2099-09-20','scheduled')""",
            (game_id, sport_id, f"G{game_id}", game_id * 10, game_id * 10 + 1))
        conn.execute("""INSERT INTO picks
            (id,sport_id,nature,source,status,edge,units_model,created_utc)
            VALUES (?,?,'straight','model','approved',0.05,1.0,'2099-01-01')""",
            (pick_id, sport_id))
        conn.execute("""INSERT INTO pick_legs
            (pick_id,ord,game_id,market,team,line,odds_american,model_prob,
             implied_prob,edge)
            VALUES (?,0,?,'h2h',?,NULL,-110,0.6,0.52,0.08)""",
            (pick_id, game_id, team))
        conn.commit()


def test_model_picks_filters_only_the_ledger(client):
    """The sport buttons sit with the ledger and filter it. The headline
    record stays every-league, and says so, rather than looking like it
    disagrees with the table underneath."""
    import re

    path = client.application.config["DB_PATH"]
    seed_pick(path, 901, "nfl", 91, "HOM")
    seed_pick(path, 902, "mlb", 92, "HOM")
    seed_pick(path, 903, "mlb", 93, "AWY")

    counts = {}
    for sport in ("nfl", "mlb", "nhl"):
        body = client.get(f"/model-picks?sport={sport}").get_data(as_text=True)
        assert f"{sport.upper()} ledger" in body
        ledger = body.split('id="ledger"', 1)[1]
        counts[sport] = len(re.findall(r"#(\d+)", ledger))
        # The record is not filtered, so it is labelled.
        assert "Every league" in body

    assert counts["nfl"] == 1
    assert counts["mlb"] == 2
    assert counts["nhl"] == 0


def test_model_picks_has_no_sport_buttons_above_the_record(client):
    body = client.get("/model-picks").get_data(as_text=True)
    head = body.split('id="ledger"', 1)[0]
    assert 'class="seg ' not in head


def test_model_picks_shows_open_picks_not_upcoming_predictions(client):
    body = client.get("/model-picks").get_data(as_text=True)
    assert "Upcoming predictions" not in body
    assert "Open picks" in body


def test_model_picks_carries_a_small_bankroll_chart(client):
    body = client.get("/model-picks").get_data(as_text=True)
    assert 'class="board-chart"' in body
    # It links to the full performance page rather than duplicating it.
    assert 'href="/performance"' in body


def test_status_reads_open_live_or_finished(client):
    """approved/graded describe what the ledger did with a pick, not what is
    happening to the bet."""
    from src.web.app import pick_state

    assert pick_state({"result": None, "game_status": "scheduled"})["label"] == "Open"
    assert pick_state({"result": None, "game_status": "in_progress"})["label"] == "Live"
    # Over but unsettled is its own state: "Finished" beside an empty result
    # reads as a bug rather than as waiting on a box score.
    assert pick_state({"result": None, "game_status": "final"})["label"] == "Settling"
    assert pick_state({"result": "W", "game_status": "final"})["label"] == "Finished"


def test_visible_numbers_use_a_true_minus_and_dates_read_as_words():
    from src.web.app import dashless, nice_date, odds_text, signed_text

    assert odds_text(-155) == "−155"
    assert odds_text(108) == "+108"
    assert odds_text(None) == ""
    assert signed_text(-1.5, "%+.1f", "%") == "−1.5%"
    assert signed_text(2.25, "%+.2f", "u") == "+2.25u"
    assert nice_date("2026-11-01") == "Sun, Nov 1"
    assert nice_date("2026-11-01T17:00:00Z") == "Sun, Nov 1"
    assert nice_date("not a date") == "not a date"
    # ESPN's live strings lose their hyphens.
    assert dashless("VAN 0 - 0 SJ") == "VAN 0, SJ 0"
    assert dashless("19:11 - 1st Period") == "19:11, 1st Period"


def test_a_bet_reads_the_way_you_would_say_it():
    from src.web.app import bet_text

    assert bet_text({"market": "h2h", "team": "DET", "line": None,
                     "sport_code": "nba"}) == "DET to win"
    assert bet_text({"market": "spread", "team": "DET", "line": -7.5,
                     "sport_code": "nba"}) == "DET −7.5"
    assert bet_text({"market": "total", "team": "Over", "line": 222.5,
                     "sport_code": "nba"}) == "Over 222.5"
    assert bet_text({"market": "prop:rushingYards", "team": "Over", "line": 51.5,
                     "player": "Bijan Robinson", "sport_code": "nfl"}) \
        == "Bijan Robinson 51.5+ Rush Yds"


def test_every_page_carries_the_crawl(client):
    for path in ("/", "/tickets", "/model-picks", "/players", "/settings"):
        body = client.get(path).get_data(as_text=True)
        assert 'class="crawl' in body, path
        # The search bar and the date were noise; they stay gone.
        assert 'id="palette"' not in body, path
        assert "top-date" not in body, path


def test_open_picks_run_along_the_crawl(client):
    path = client.application.config["DB_PATH"]
    seed_pick(path, 951, "nba", 95, "HOM")
    body = client.get("/").get_data(as_text=True)
    crawl = body.split('class="crawl', 1)[1].split("</section>", 1)[0]
    assert "HOM to win" in crawl
    assert "−110" in crawl


def test_the_retired_line_board_sends_you_to_todays_games(client):
    response = client.get("/games?sport=nfl")
    assert response.status_code == 302
    assert "/tickets" in response.headers["Location"]
    nav = client.get("/").get_data(as_text=True).split('class="rail-nav"', 1)[1]
    assert 'href="/games' not in nav.split("</nav>", 1)[0]


def test_no_page_shows_a_raw_workflow_status(client):
    for path in ("/", "/games", "/performance", "/model-picks"):
        body = client.get(path).get_data(as_text=True)
        for jargon in (">approved<", ">graded<", ">content_generated<"):
            assert jargon not in body, f"{jargon} on {path}"


def test_every_unit_figure_carries_its_dollar_value(client):
    """Units are the stake the model speaks in; the money is the same number
    at the configured unit size, and both belong together."""
    import re

    path = client.application.config["DB_PATH"]
    seed_pick(path, 910, "nfl", 94, "HOM")
    with db.connect(path) as conn:
        conn.execute("""INSERT INTO results (pick_id,result,units_won,graded_utc)
                        VALUES (910,'W',1.91,'2099-01-02')""")
        conn.commit()

    body = client.get("/performance").get_data(as_text=True)
    cells = re.findall(r'class="units-win[^"]*">(.*?)</td>', body, re.S)
    assert cells, "no graded unit cell rendered"
    flat = " ".join(re.sub(r"<[^>]+>", " ", cells[0]).split())
    assert "u" in flat and "$" in flat, flat


def test_pricing_is_reported_as_a_count_not_a_contradiction(client, monkeypatch):
    """`priced` was true if ANY game had book prices, while the note came from
    the FIRST game without them - so a mostly-priced board said "priced" and
    "model-only" at the same time."""
    from src.picker import board as board_mod
    from src.picker import players as players_mod

    class Card:
        def __init__(self, n, priced):
            self.game_id, self.matchup = n, f"AWY{n} @ HOM{n}"
            self.home, self.away, self.sport = "HOM", "AWY", "nfl"
            self.bettable, self.started, self.status = True, False, "scheduled"
            self.kickoff = ""
            self.priced = priced
        def kickoff_local(self, tz=None): return ""

    class Deep:
        def __init__(self, priced):
            self.priced = priced
            self.props, self.props_note = [], (
                "" if priced else "No matching Odds API event, so props are model-only.")
        def players(self): return []

    cards = [Card(1, True), Card(2, True), Card(3, False)]
    monkeypatch.setattr(board_mod, "upcoming_board", lambda *a, **k: cards)
    monkeypatch.setattr(board_mod, "deep_matchup",
                        lambda card, **kw: Deep(card.priced))

    built = players_mod.build_player_board("nfl", days=1, max_games=3)
    assert built.games_scanned == 3
    assert built.priced_games == 2
    # The note names the one game it is about.
    assert "AWY3 @ HOM3" in built.note


def _service_client(tmp_path, monkeypatch, admins=""):
    monkeypatch.setenv("EDGEBOARD_SERVICE", "1")
    monkeypatch.setenv("EDGEBOARD_ADMIN_EMAILS", admins)
    path = tmp_path / "svc.sqlite"
    db.init_db(path)
    app = create_app(path)
    app.testing = True
    return app.test_client()


def test_service_mode_hides_the_owner_pages_from_visitors(tmp_path, monkeypatch):
    client = _service_client(tmp_path, monkeypatch, admins="owner@example.com")
    assert client.get("/settings").status_code == 404
    # Public pages, pricing and the age gate are there.
    body = client.get("/").get_data(as_text=True)
    assert "/pricing" in body
    assert client.get("/pricing").status_code == 200


def test_free_plan_sees_two_open_picks_per_league_per_day(tmp_path, monkeypatch):
    client = _service_client(tmp_path, monkeypatch)
    path = client.application.config["DB_PATH"]
    for n in range(4):
        seed_pick(path, 960 + n, "nba", 96, "HOM")
    body = client.get("/model-picks").get_data(as_text=True)
    assert "2 more open picks" in body
    crawl = body.split('class="crawl', 1)[1].split("</section>", 1)[0]
    assert crawl.count('class="crawl-item') == 2


def test_hit_rates_are_a_paid_feature_in_service_mode(tmp_path, monkeypatch):
    client = _service_client(tmp_path, monkeypatch)
    response = client.get("/props")
    assert response.status_code == 302
