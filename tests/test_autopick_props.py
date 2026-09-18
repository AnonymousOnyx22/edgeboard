"""Player props going through the picker: selection, dedupe, commit, grading."""
import pytest

from src.db import database as db
from src.picker import autopick
from src.picker.grader import grade_pick, grade_prop
from src.picker.props import Projection, PropCandidate


@pytest.fixture()
def props_db(tmp_path):
    path = tmp_path / "props.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("""INSERT INTO teams
            (id,sport_id,league_key,name,abbrev) VALUES
            (1,1,'A','Away','AWY'),(2,1,'H','Home','HOM')""")
        conn.execute("""INSERT INTO games
            (id,sport_id,external_id,away_team,home_team,start_utc,game_date,status)
            VALUES (1,1,'G1',1,2,'2099-09-20T17:00:00Z','2099-09-20','scheduled')""")
        conn.commit()
    return path


def make_candidate(**overrides):
    """A priced prop the model likes, as rank_market_candidates would emit it."""
    projection = Projection(stat_key="passingYards", mean=268.0, sd=48.0,
                            n_eff=7.4, n_games=9, n_vs=3, vs_average=281.0,
                            recent_average=272.0, sport="nfl")
    fields = dict(
        athlete_id="3918298", player="Josh Allen", position="QB",
        team="BUF", opponent="MIA", stat_key="passingYards", line=249.5,
        probability=0.62, projection=projection, hit_vs_opponent=(3, 3),
        hit_recent=(6, 9), game_id=1, sport="nfl", side="over", odds=-110,
        implied_prob=0.524, edge=0.096, bookmaker="draftkings",
    )
    fields.update(overrides)
    return PropCandidate(**fields)


class FakeCard:
    sport = "nfl"
    matchup = "AWY @ HOM"
    started = False
    bettable = True

    def kickoff_local(self, tz="America/New_York"):
        return "Sun 1:00 PM"


# ------------------------------------------------------------------ adapting
def test_priced_prop_becomes_a_committable_pick():
    pick = autopick.to_prop_pick(make_candidate())
    assert pick.market == "prop:passingYards"
    assert pick.side == "Over"
    assert pick.athlete_id == "3918298"
    assert pick.player == "Josh Allen"
    assert pick.edge == pytest.approx(0.096)
    assert 1 <= pick.confidence["score"] <= 10
    assert pick.units > 0


def test_unpriced_prop_is_never_committable():
    """No book price means no edge to measure, so it cannot enter the record."""
    assert autopick.to_prop_pick(
        make_candidate(odds=None, implied_prob=None, edge=None)) is None


def test_under_keeps_its_side():
    pick = autopick.to_prop_pick(make_candidate(side="under"))
    assert pick.side == "Under"


def test_market_roundtrips_to_its_stat_key():
    assert autopick.stat_key_of("prop:receivingYards") == "receivingYards"
    assert autopick.stat_key_of("h2h") is None
    assert not autopick.is_prop_market("total")


# ----------------------------------------------------------------- selecting
def rules(**overrides):
    r = dict(autopick.PROP_DEFAULTS)
    r.update(overrides)
    return r


def test_thin_edge_is_rejected():
    """Confidence is relaxed so the edge gate is the one under test."""
    pick = autopick.to_prop_pick(make_candidate(edge=0.01, probability=0.53))
    ok, why = autopick.prop_qualifies(pick, rules(min_confidence=1))
    assert not ok and "edge" in why


def test_a_weak_prop_is_rejected_on_confidence():
    pick = autopick.to_prop_pick(make_candidate(edge=0.01, probability=0.53))
    ok, why = autopick.prop_qualifies(pick, rules())
    assert not ok and "confidence" in why


def test_flagged_prop_is_rejected():
    """The model flags an implausible edge; the picker must not sell it."""
    candidate = make_candidate()
    candidate.warnings = ["edge of 31% is implausibly large"]
    pick = autopick.to_prop_pick(candidate)
    ok, why = autopick.prop_qualifies(pick, rules(min_confidence=1, min_edge=0.0))
    assert not ok and "implausibly" in why


def test_selection_is_capped_and_ordered_by_confidence():
    card = FakeCard()
    pairs = []
    for i, edge in enumerate([0.05, 0.20, 0.12]):
        candidate = make_candidate(edge=edge, probability=0.60 + i * 0.05,
                                   athlete_id=f"a{i}", player=f"Player {i}")
        pairs.append((card, autopick.to_prop_pick(candidate)))
    chosen = autopick.select_props(pairs, rules(min_confidence=1,
                                                min_edge=0.0, max_per_day=2))
    assert len(chosen) == 2
    scores = [pick.confidence["score"] for _, pick in chosen]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------- committing
def test_commit_writes_the_athlete(props_db):
    pick = autopick.to_prop_pick(make_candidate())
    pick_id = autopick.commit(FakeCard(), pick, autopick.DEFAULTS, props_db)
    assert pick_id is not None
    leg = db.query(props_db, "SELECT * FROM pick_legs WHERE pick_id=?", (pick_id,))[0]
    assert leg["market"] == "prop:passingYards"
    assert leg["athlete_id"] == "3918298"
    assert leg["player"] == "Josh Allen"
    assert leg["line"] == pytest.approx(249.5)
    assert "3/3 vs MIA" in leg["reason"]


def test_same_prop_is_never_logged_twice(props_db):
    pick = autopick.to_prop_pick(make_candidate())
    assert autopick.commit(FakeCard(), pick, autopick.DEFAULTS, props_db) is not None
    assert autopick.commit(FakeCard(), pick, autopick.DEFAULTS, props_db) is None


def test_two_players_can_hold_the_same_line(props_db):
    """Same game, market, side and line - different athlete, different bet."""
    first = autopick.to_prop_pick(make_candidate())
    second = autopick.to_prop_pick(
        make_candidate(athlete_id="4241479", player="Tua Tagovailoa"))
    assert autopick.commit(FakeCard(), first, autopick.DEFAULTS, props_db) is not None
    assert autopick.commit(FakeCard(), second, autopick.DEFAULTS, props_db) is not None


def test_a_started_game_is_refused(props_db):
    card = FakeCard()
    card.started = True
    pick = autopick.to_prop_pick(make_candidate())
    assert autopick.commit(card, pick, autopick.DEFAULTS, props_db) is None


def test_game_lines_still_leave_the_athlete_empty(props_db):
    """The existing path must be untouched by the new columns."""
    from src.picker.board import LineCandidate

    line = LineCandidate(game_id=1, game="AWY @ HOM", market="h2h", side="HOM",
                         line=None, odds=-130, model_prob=0.62, implied_prob=0.565,
                         edge=0.055, confidence={"score": 7}, units=2.0)
    pick_id = autopick.commit(FakeCard(), line, autopick.DEFAULTS, props_db)
    leg = db.query(props_db, "SELECT * FROM pick_legs WHERE pick_id=?", (pick_id,))[0]
    assert leg["athlete_id"] is None and leg["player"] is None


# ------------------------------------------------------------------ grading
def test_grade_prop_over_under_and_push():
    assert grade_prop("Over", 249.5, 280) == "W"
    assert grade_prop("Over", 249.5, 210) == "L"
    assert grade_prop("Under", 249.5, 210) == "W"
    assert grade_prop("Under", 249.5, 280) == "L"
    # Whole-number lines do push.
    assert grade_prop("Over", 1, 1) == "P"


def test_grade_prop_refuses_a_missing_stat_line():
    with pytest.raises(ValueError):
        grade_prop("Over", 249.5, None)


class FakeStats:
    """Stands in for ESPN's gamelog feed."""

    def __init__(self, value):
        self.value = value

    def game_rows(self, athlete_id, seasons=None):
        return [{"event_id": "G1", "passingYards": self.value}]


def test_a_committed_prop_grades_from_the_game_log(props_db):
    pick = autopick.to_prop_pick(make_candidate())
    pick_id = autopick.commit(FakeCard(), pick, autopick.DEFAULTS, props_db)
    db.execute(props_db, """UPDATE games SET status='final', away_score=20,
                            home_score=24 WHERE id=1""")

    graded = grade_pick(pick_id, props_db, stats=FakeStats(301))
    assert graded["result"] == "W"
    assert graded["units_won"] > 0


def test_a_prop_that_misses_is_a_loss(props_db):
    pick = autopick.to_prop_pick(make_candidate())
    pick_id = autopick.commit(FakeCard(), pick, autopick.DEFAULTS, props_db)
    db.execute(props_db, """UPDATE games SET status='final', away_score=20,
                            home_score=24 WHERE id=1""")

    graded = grade_pick(pick_id, props_db, stats=FakeStats(188))
    assert graded["result"] == "L"
    assert graded["units_won"] < 0


def test_a_player_who_did_not_play_is_not_silently_graded(props_db):
    """No stat line is an error, never a loss - the record must stay honest."""
    pick = autopick.to_prop_pick(make_candidate())
    pick_id = autopick.commit(FakeCard(), pick, autopick.DEFAULTS, props_db)
    db.execute(props_db, """UPDATE games SET status='final', away_score=20,
                            home_score=24 WHERE id=1""")

    class Absent:
        def game_rows(self, athlete_id, seasons=None):
            return []

    with pytest.raises(ValueError):
        grade_pick(pick_id, props_db, stats=Absent())
