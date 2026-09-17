import pytest

from src.picker import edge


def test_american_to_prob_even():
    assert edge.american_to_prob(100) == pytest.approx(0.5)
    assert edge.american_to_prob(-100) == pytest.approx(0.5)


def test_american_to_prob_favorite_underdog():
    # -150 -> 0.60, +150 -> 0.40 (with vig both sides)
    assert edge.american_to_prob(-150) == pytest.approx(0.6)
    assert edge.american_to_prob(150) == pytest.approx(0.4)
    assert edge.american_to_prob(-110) == pytest.approx(0.5238, abs=1e-4)


def test_american_to_prob_zero_raises():
    with pytest.raises(ValueError):
        edge.american_to_prob(0)


def test_decimal_from_american():
    assert edge.decimal_from_american(-110) == pytest.approx(1.9091, abs=1e-4)
    assert edge.decimal_from_american(150) == pytest.approx(2.5)


def test_remove_vig_splits_even_evens():
    # -110 / -110 -> each 52.38% implied, vig free -> 50/50
    p_a, p_h = edge.remove_vig(edge.american_to_prob(-110), edge.american_to_prob(-110))
    assert p_a == pytest.approx(0.5)
    assert p_h == pytest.approx(0.5)
    assert p_a + p_h == pytest.approx(1.0)


def test_remove_vig_extracts_true_prob():
    # -200/+170 implied .667/.370 (heavy vig), true ~ .643/.357
    p_dog, p_fav = edge.remove_vig(edge.american_to_prob(170), edge.american_to_prob(-200))
    assert p_dog + p_fav == pytest.approx(1.0)
    assert p_fav > 0.6
    assert p_dog == pytest.approx(0.357, abs=1e-2)


def test_remove_vig_requires_overround():
    with pytest.raises(ValueError):
        edge.remove_vig(0.25, 0.25)  # sums to 0.5 - no vig


def test_remove_vig_argument_order_is_away_home():
    # The engine relies on (away, home) order: return values are (away_vf, home_vf).
    p_away, p_home = edge.remove_vig(edge.american_to_prob(-130), edge.american_to_prob(-110))
    assert p_away > p_home
    assert p_home == pytest.approx(0.481, abs=1e-3)
    assert p_away + p_home == pytest.approx(1.0)


def test_edge_positive_when_model_above_market():
    e = edge.edge(0.60, 0.55)
    assert e == pytest.approx(0.05)


def test_cover_probability_half_point():
    # model exactly at line -> 50/50 cover
    assert edge.cover_probability(3.0, 10.0, -3.0) == pytest.approx(0.5, abs=1e-6)
    # mean 10 sd 6 covering -3 -> clearly favored
    assert edge.cover_probability(10.0, 6.0, -3.0) > 0.85
    # line -3.5 at mean 3.5 -> 50/50
    assert edge.cover_probability(3.5, 10.0, -3.5) == pytest.approx(0.5, abs=1e-6)


def test_over_probability():
    assert edge.over_probability(44.0, 10.0, 44.0) == pytest.approx(0.5)
    assert edge.over_probability(50.0, 10.0, 44.0) > 0.7
    assert edge.over_probability(40.0, 10.0, 44.0) < 0.4


def test_payout_multiplier():
    assert edge.payout_multiplier(-110) == pytest.approx(0.9091, abs=1e-4)
    assert edge.payout_multiplier(120) == pytest.approx(1.2)
    assert edge.payout_multiplier(-100) == pytest.approx(1.0)