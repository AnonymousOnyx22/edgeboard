import pytest

from src.picker import confidence as conf


def _cfg():
    return {
        "weights": {"edge": 0.45, "probability": 0.35, "quality": 0.20},
        "quality_penalties": {
            "missing_starter_confirmation": 2.0,
            "missing_injury_data": 1.0,
            "small_sample_threshold": 4,
            "early_season_small_sample": 1.0,
            "missing_stat_feed": 1.0,
            "extreme_weather": 1.0,
        },
    }


def test_edge_score_scales_with_target():
    assert conf.edge_score(0.03, target=0.03) == pytest.approx(10.0)
    assert conf.edge_score(0.015, target=0.03) == pytest.approx(5.0)
    assert conf.edge_score(-0.01, target=0.03) == 0.0


def test_prob_score():
    assert conf.prob_score(0.75) == pytest.approx(10.0)
    assert conf.prob_score(0.50) == 0.0
    assert conf.prob_score(0.60) == pytest.approx(4.0)
    assert conf.prob_score(0.80) == pytest.approx(10.0)  # clamped


def test_quality_score_starts_at_10():
    assert conf.quality_score({}, _cfg()) == 10.0


def test_quality_score_penalizes_flags():
    cfg = _cfg()
    assert conf.quality_score({"missing_injury_data": True}, cfg) == pytest.approx(9.0)
    q2 = {"missing_starter_confirmation": True, "missing_injury_data": True}
    assert conf.quality_score(q2, cfg) == pytest.approx(7.0)


def test_quality_penalizes_early_season():
    cfg = _cfg()
    assert conf.quality_score({"games_of_history": 2}, cfg) == pytest.approx(9.0)
    assert conf.quality_score({"games_of_history": 9}, cfg) == pytest.approx(10.0)


def test_confidence_in_range_and_ordering():
    weak = conf.compute_confidence(0.031, 0.53, {"games_of_history": 12}, _cfg())
    strong = conf.compute_confidence(0.09, 0.68, {"games_of_history": 12}, _cfg())
    assert 1 <= weak["score"] <= 10
    assert 1 <= strong["score"] <= 10
    assert strong["score"] > weak["score"]
    for k in ("edge_score", "prob_score", "quality_score", "raw"):
        assert k in strong


def test_units_tiers():
    tiers = {"units": {"tiers": {1: 5.5, 2: 7.5, 3: 10}}}
    assert conf.units_from_confidence(4.0, tiers) == 1.0
    assert conf.units_from_confidence(7.0, tiers) == 2.0
    assert conf.units_from_confidence(9.0, tiers) == 3.0


def test_flat_stake_overrides_the_tiers():
    flat = {"units": {"flat": 1, "tiers": {1: 5.5, 2: 7.5, 3: 10}}}
    assert {conf.units_from_confidence(s, flat) for s in (4.0, 7.0, 9.0)} == {1.0}


def test_display_modes():
    assert conf.display(8, {"confidence": {"display": "percent"}}) == "80%"
    assert conf.display(8, {"confidence": {"display": "stars"}}).count("★") == 8
    assert conf.display(8, {"confidence": {"display": "scale"}}) == "8/10"