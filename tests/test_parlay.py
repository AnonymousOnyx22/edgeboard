import pytest

from src.picker.parlay_builder import Leg, ParlayError, build_parlay


def _legs(n=2, game_id_prefix=1):
    return [Leg(game_id=f"{game_id_prefix}{i}", market="h2h", team="T", line=None,
                odds_american=-110, model_prob=0.55) for i in range(n)]


def test_two_legged_combine():
    p = build_parlay(_legs(2))
    # two -110 legs (~1.9091 each) -> dec ~3.64, payout ~2.64u
    assert p.combined_odds == pytest.approx(1.9091 * 1.9091, abs=1e-3)
    assert p.payout_per_unit == pytest.approx(1.9091 * 1.9091 - 1.0, abs=1e-3)
    assert p.implied_prob == pytest.approx((0.5238) ** 2, abs=1e-3)


def test_leg_count_bounds():
    with pytest.raises(ParlayError):
        build_parlay(_legs(1))
    with pytest.raises(ParlayError):
        build_parlay(_legs(5))


def test_same_game_legs_rejected():
    with pytest.raises(ParlayError):
        build_parlay([Leg(game_id="g1", market="h2h", team="A", line=None,
                          odds_american=-110, model_prob=0.5),
                      Leg(game_id="g1", market="spread", team="B", line=-3,
                          odds_american=-110, model_prob=0.5)])


def test_different_games_allowed():
    p = build_parlay([Leg(game_id="g1", market="h2h", team="A", line=None,
                          odds_american=-110, model_prob=0.5),
                      Leg(game_id="g2", market="h2h", team="B", line=None,
                          odds_american=120, model_prob=0.4)])
    assert p.combined_odds == pytest.approx(1.9091 * 2.2, abs=1e-3)


def test_american_display():
    p = build_parlay(_legs(2))
    assert p.american_display().startswith("+")  # dec 3.64 -> +264