"""Prop projection model: weighting, shrinkage, line search, market edges."""
import pytest

from src.picker import props
from src.picker.h2h import PlayerSample


def _game(event_id, date, opponent, **stats):
    row = {"event_id": event_id, "date": date, "opponent": opponent,
           "receivingTargets": 6, "receptions": 4}
    row.update(stats)
    return row


def _receiver(vs_values=(), recent_values=(), opponent="MIA"):
    """A WR whose receiving yards are exactly the values given, newest first.

    Real gamelogs arrive newest first, so index 0 gets the latest date.
    """
    vs = [_game(f"v{i}", f"2025-01-{len(vs_values) - i:02d}", opponent,
                receivingYards=v)
          for i, v in enumerate(vs_values)]
    recent = [_game(f"r{i}", f"2026-01-{len(recent_values) - i:02d}", "NE",
                    receivingYards=v)
              for i, v in enumerate(recent_values)]
    return PlayerSample(athlete_id="1", name="Test WR", position="WR",
                        team="BUF", opponent=opponent,
                        vs_games=vs, recent_games=recent)


# ------------------------------------------------------------- projection
def test_projection_needs_data():
    empty = PlayerSample(athlete_id="1", name="No Games", position="WR",
                         team="BUF", opponent="MIA")
    assert props.project(empty, "receivingYards") is None


def test_projection_tracks_the_sample():
    sample = _receiver(recent_values=(80, 80, 80, 80, 80))
    projection = props.project(sample, "receivingYards")
    assert projection.mean == pytest.approx(80.0)
    assert projection.n_games == 5


def test_flat_sample_still_gets_a_spread_floor():
    """Five identical games must not imply the outcome is certain."""
    sample = _receiver(recent_values=(80, 80, 80, 80, 80))
    projection = props.project(sample, "receivingYards")
    assert projection.sd >= 22.0
    probability = props.hit_probability(sample, "receivingYards", 70.5, projection)
    assert probability < 0.95


def test_games_against_this_opponent_are_weighted_up():
    """The head-to-head games are the point, so they must move the number."""
    without = _receiver(recent_values=(50, 50, 50, 50))
    with_h2h = _receiver(vs_values=(120, 120), recent_values=(50, 50, 50, 50))
    assert (props.project(with_h2h, "receivingYards").mean
            > props.project(without, "receivingYards").mean)


def test_recency_beats_ancient_history():
    """Later games carry more weight than earlier ones."""
    improving = _receiver(recent_values=(100, 100, 20, 20))   # newest first
    declining = _receiver(recent_values=(20, 20, 100, 100))
    assert (props.project(improving, "receivingYards").mean
            > props.project(declining, "receivingYards").mean)


# ------------------------------------------------------------- probability
def test_thin_samples_are_pulled_toward_a_coin_flip():
    """One perfect game is not a 99% bet."""
    single = _receiver(recent_values=(200,))
    probability = props.hit_probability(single, "receivingYards", 40.5)
    assert 0.5 < probability < 0.9


def test_more_evidence_raises_confidence_in_the_same_claim():
    thin = _receiver(recent_values=(100,))
    thick = _receiver(recent_values=(100,) * 8)
    line = 60.5
    assert (props.hit_probability(thick, "receivingYards", line)
            > props.hit_probability(thin, "receivingYards", line))


def test_probability_falls_as_the_line_rises():
    sample = _receiver(recent_values=(60, 70, 80, 90, 100))
    low = props.hit_probability(sample, "receivingYards", 40.5)
    high = props.hit_probability(sample, "receivingYards", 140.5)
    assert low > high


def test_count_hits_reports_the_real_denominator():
    games = [_game("a", "2026-01-01", "MIA", receivingYards=80),
             _game("b", "2026-01-02", "MIA", receivingYards=40),
             _game("c", "2026-01-03", "MIA")]          # stat not recorded
    assert props.count_hits(games, "receivingYards", 50.5) == (1, 2)


# ------------------------------------------------------------- line search
def test_candidate_lines_sit_below_the_projection():
    sample = _receiver(recent_values=(80, 90, 70, 85, 75))
    projection = props.project(sample, "receivingYards")
    lines = props.candidate_lines(projection)
    assert lines
    assert max(lines) <= projection.mean + 5
    assert all(line > 0 for line in lines)
    assert all(abs(line % 1 - 0.5) < 1e-9 for line in lines)   # always x.5


def test_best_line_reaches_the_target_when_it_can():
    sample = _receiver(recent_values=(120, 130, 125, 140, 135, 128))
    found = props.best_line(sample, "receivingYards", target=0.70)
    assert found is not None
    line, probability = found
    assert probability >= 0.70
    assert line < 120      # a line it clears, not one it merely projects to


# ------------------------------------------------------------ market props
class _Book:
    """Minimal stand-in for props_odds.PropBook."""

    def __init__(self, offers):
        self._offers = offers

    def lines_for(self, player, stat_key):
        return self._offers.get(stat_key, [])


def test_market_candidate_prices_a_real_edge():
    sample = _receiver(recent_values=(100,) * 8)
    book = _Book({"receivingYards": [
        {"bookmaker": "draftkings", "line": 60.5, "over": -110, "under": -110}]})
    found = props.market_candidates(sample, book, min_probability=0.5, min_edge=0.0)
    over = [c for c in found if c.side == "over"]
    assert over
    candidate = over[0]
    assert candidate.priced
    assert candidate.odds == -110
    assert candidate.implied_prob == pytest.approx(0.5, abs=0.01)
    assert candidate.edge == pytest.approx(candidate.probability - candidate.implied_prob,
                                           abs=1e-3)


def test_market_candidate_can_favour_the_under():
    sample = _receiver(recent_values=(10,) * 8)
    book = _Book({"receivingYards": [
        {"bookmaker": "fanduel", "line": 60.5, "over": -110, "under": -110}]})
    found = props.market_candidates(sample, book, min_probability=0.5, min_edge=0.0)
    assert any(c.side == "under" for c in found)


def test_no_price_means_no_market_candidate():
    sample = _receiver(recent_values=(100,) * 8)
    book = _Book({"receivingYards": [
        {"bookmaker": "dk", "line": 60.5, "over": None, "under": None}]})
    assert props.market_candidates(sample, book, min_probability=0.0, min_edge=-1.0) == []


def test_evidence_reads_from_the_bet_side():
    sample = _receiver(vs_values=(10, 10), recent_values=(10,) * 6)
    book = _Book({"receivingYards": [
        {"bookmaker": "dk", "line": 60.5, "over": -110, "under": -110}]})
    unders = [c for c in props.market_candidates(sample, book, min_probability=0.5,
                                                 min_edge=0.0) if c.side == "under"]
    assert unders
    # The player stayed under in both meetings, so the Under reads 2/2.
    assert "2/2 vs MIA" in unders[0].evidence()


def test_require_head_to_head_filters_out_first_meetings():
    no_history = _receiver(recent_values=(100,) * 8)
    book = _Book({"receivingYards": [
        {"bookmaker": "dk", "line": 60.5, "over": -110, "under": -110}]})
    kept = props.rank_market_candidates([no_history], book, min_probability=0.5,
                                        min_edge=0.0, require_h2h=True)
    assert kept == []


# ------------------------------------------------------- market as a prior
def test_market_prior_pulls_the_model_toward_the_price():
    """A model that disagrees wildly with a sharp book is usually the wrong one."""
    sample = _receiver(recent_values=(100,) * 8)
    book = _Book({"receivingYards": [
        {"bookmaker": "dk", "line": 60.5, "over": +200, "under": -250}]})
    found = props.market_candidates(sample, book, min_probability=0.0, min_edge=-1.0)
    over = [c for c in found if c.side == "over"][0]
    # The reported probability sits between the raw model view and the price.
    assert over.raw_probability > over.probability > over.implied_prob


def test_raw_probability_is_preserved_for_inspection():
    sample = _receiver(recent_values=(100,) * 8)
    book = _Book({"receivingYards": [
        {"bookmaker": "dk", "line": 60.5, "over": -110, "under": -110}]})
    over = [c for c in props.market_candidates(sample, book, min_probability=0.0,
                                               min_edge=-1.0) if c.side == "over"][0]
    assert over.raw_probability is not None
    assert over.edge == pytest.approx(over.probability - over.implied_prob, abs=1e-3)


def test_wild_disagreement_is_flagged_not_hidden():
    sample = _receiver(recent_values=(100,) * 8)
    book = _Book({"receivingYards": [
        {"bookmaker": "dk", "line": 10.5, "over": +300, "under": -400}]})
    over = [c for c in props.market_candidates(sample, book, min_probability=0.0,
                                               min_edge=-1.0) if c.side == "over"][0]
    assert over.warnings
    assert not over.trustworthy
    assert any("market" in w.lower() for w in over.warnings)


def test_injury_and_thin_sample_are_warned_about():
    sample = _receiver(recent_values=(100, 100, 100))
    sample.injury_status = "Questionable"
    book = _Book({"receivingYards": [
        {"bookmaker": "dk", "line": 60.5, "over": -110, "under": -110}]})
    found = props.market_candidates(sample, book, min_probability=0.0,
                                    min_edge=-1.0, min_games=1)
    over = [c for c in found if c.side == "over"][0]
    joined = " ".join(over.warnings).lower()
    assert "questionable" in joined
    assert "3 games" in joined
    assert "no prior game against this opponent" in joined


def test_a_clean_prop_carries_no_warnings():
    """Deep sample, head-to-head history, and a line the model broadly agrees with."""
    sample = _receiver(vs_values=(70, 75), recent_values=(70, 72, 68, 74, 71, 73, 69, 70))
    book = _Book({"receivingYards": [
        {"bookmaker": "dk", "line": 71.5, "over": -110, "under": -110}]})
    priced = props.market_candidates(sample, book, min_probability=0.0, min_edge=-1.0)
    assert priced
    assert all(c.trustworthy for c in priced), [c.warnings for c in priced]
