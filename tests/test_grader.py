import pytest

from src.picker.grader import grade_straight, units_won


def test_h2h():
    assert grade_straight("h2h", "NE", None, 17, 14, "NE", "NY") == "W"
    assert grade_straight("h2h", "NE", None, 10, 24, "NE", "NY") == "L"
    assert grade_straight("h2h", "NE", None, 14, 14, "NE", "NY") == "P"


def test_spread_favorite_covers():
    # NE -6.5, NE wins by 10 -> cover
    assert grade_straight("spread", "NE", -6.5, 10, 0, "NE", "NY") == "W"
    # NE -6.5 wins by 3 -> lose
    assert grade_straight("spread", "NE", -6.5, 17, 14, "NE", "NY") == "L"


def test_spread_underdog_covers():
    # NY +6.5, loses by 3 -> cover
    assert grade_straight("spread", "NY", 6.5, 17, 14, "NE", "NY") == "W"
    # NY +6.5, loses by 10 -> lose
    assert grade_straight("spread", "NY", 6.5, 27, 14, "NE", "NY") == "L"


def test_spread_push_whole_number():
    assert grade_straight("spread", "NE", -7, 17, 10, "NE", "NY") == "P"


def test_total_over_under():
    assert grade_straight("total", "Over", 44.5, 24, 21, "NE", "NY") == "W"
    assert grade_straight("total", "Under", 44.5, 24, 17, "NE", "NY") == "W"
    assert grade_straight("total", "Over", 45, 24, 21, "NE", "NY") == "P"


def test_units_won():
    assert units_won("W", 1.0, 0.9091) == pytest.approx(0.9091)
    assert units_won("L", 2.0, 0.5) == pytest.approx(-2.0)
    assert units_won("P", 1.0, 0.9091) == 0.0


def test_unknown_total_side_raises():
    with pytest.raises(ValueError):
        grade_straight("total", "Left", 44.5, 24, 21, "NE", "NY")