"""Parlay optimizer: correlation rules, ranking, and same-game handling."""
import pytest

from src.picker.board import scoring_rates, total_model
from src.picker.parlay_builder import Leg, ParlayError, build_parlay, optimize


def _leg(game, prob=0.7, odds=-150, athlete="", stat="", market="h2h"):
    return Leg(game_id=game, market=market, team=f"T{game}", line=None,
               odds_american=odds, model_prob=prob,
               athlete_id=athlete, stat_key=stat)


def test_optimizer_returns_ranked_parlays():
    legs = [_leg(f"g{i}", prob=0.75 - 0.05 * i) for i in range(4)]
    found = optimize(legs, min_legs=2, max_legs=3, limit=5)
    assert found
    probs = [p.combined_model_prob for p in found]
    assert probs == sorted(probs, reverse=True)


def test_optimizer_never_pairs_two_legs_from_one_game():
    legs = [_leg("g1", market="h2h"), _leg("g1", market="spread"), _leg("g2")]
    for parlay in optimize(legs, min_legs=2, max_legs=2, limit=10):
        games = [leg.game_id for leg in parlay.legs]
        assert len(set(games)) == len(games)


def test_optimizer_never_uses_one_player_twice():
    legs = [_leg("g1", athlete="a1", stat="receivingYards", market="prop"),
            _leg("g2", athlete="a1", stat="receptions", market="prop"),
            _leg("g3", athlete="a2", stat="receptions", market="prop")]
    for parlay in optimize(legs, min_legs=2, max_legs=3, limit=10):
        athletes = [leg.athlete_id for leg in parlay.legs if leg.athlete_id]
        assert len(set(athletes)) == len(athletes)


def test_same_game_parlay_is_discounted_when_allowed():
    legs = [_leg("g1", prob=0.7), _leg("g1", prob=0.7, market="spread")]
    parlay = build_parlay(legs, allow_same_game=True)
    assert parlay.correlated
    # Naive independence would be 0.49; correlation must pull it down.
    assert parlay.combined_model_prob < 0.49
    assert any("correlation" in note.lower() for note in parlay.notes)


def test_same_game_parlay_is_refused_by_default():
    with pytest.raises(ParlayError):
        build_parlay([_leg("g1"), _leg("g1", market="spread")])


def test_value_ranking_differs_from_probability_ranking():
    legs = [_leg("g1", prob=0.80, odds=-400),    # very likely, poor price
            _leg("g2", prob=0.80, odds=-400),
            _leg("g3", prob=0.55, odds=+200),    # less likely, strong price
            _leg("g4", prob=0.55, odds=+200)]
    safest = optimize(legs, 2, 2, rank_by="probability", limit=1)[0]
    richest = optimize(legs, 2, 2, rank_by="value", limit=1)[0]
    assert safest.combined_model_prob > richest.combined_model_prob
    assert richest.expected_value > safest.expected_value


def test_unpriced_legs_are_flagged():
    legs = [_leg("g1"), Leg(game_id="g2", market="prop", team="Player", line=60.5,
                            odds_american=-110, model_prob=0.7, priced=False)]
    parlay = build_parlay(legs)
    assert not parlay.fully_priced
    assert any("assumed price" in note for note in parlay.notes)


def test_minimum_probability_filters_the_search():
    legs = [_leg(f"g{i}", prob=0.55) for i in range(4)]
    assert optimize(legs, 2, 2, min_probability=0.9) == []


def test_pool_smaller_than_min_legs_returns_nothing():
    assert optimize([_leg("g1")], min_legs=2, max_legs=4) == []


# ------------------------------------------------------------ totals model
class _Frame:
    """Stand-in for an empty history frame."""
    empty = True


def test_scoring_rates_falls_back_without_history():
    rates = scoring_rates(_Frame(), "nfl")
    assert rates["teams"] == {}
    assert rates["league"] > 0


def test_total_model_uses_the_league_base_when_teams_are_unknown():
    base, sd = total_model({"h2h_total": None, "h2h_weight": 0.0}, "nfl")
    assert base == pytest.approx(43.0)
    assert sd > 0


def test_head_to_head_scoring_pulls_the_total():
    plain, _ = total_model({"h2h_total": None, "h2h_weight": 0.0}, "nfl")
    nudged, _ = total_model({"h2h_total": 90.0, "h2h_weight": 0.3}, "nfl")
    assert nudged > plain


def test_each_sport_has_its_own_scale():
    nfl, _ = total_model({}, "nfl")
    mlb, _ = total_model({}, "mlb")
    nhl, _ = total_model({}, "nhl")
    assert nfl > mlb > nhl
