"""Head-to-head layer: meetings, records, and participation filtering."""
import pandas as pd
import pytest

from src.picker import h2h


def _history():
    """Four BUF/MIA meetings plus noise, in the nflverse column shape."""
    return pd.DataFrame([
        {"season": 2025, "week": 10, "gameday": "2025-11-09", "home_team": "MIA",
         "away_team": "BUF", "home_score": 30, "away_score": 13,
         "spread_line": -8.5, "total_line": 50.5, "roof": "open"},
        {"season": 2025, "week": 3, "gameday": "2025-09-18", "home_team": "BUF",
         "away_team": "MIA", "home_score": 31, "away_score": 21,
         "spread_line": 12.5, "total_line": 50.5, "roof": "open"},
        {"season": 2024, "week": 9, "gameday": "2024-11-03", "home_team": "BUF",
         "away_team": "MIA", "home_score": 30, "away_score": 27,
         "spread_line": 6.5, "total_line": 49.0, "roof": "open"},
        {"season": 2024, "week": 2, "gameday": "2024-09-12", "home_team": "MIA",
         "away_team": "BUF", "home_score": 10, "away_score": 31,
         "spread_line": 2.5, "total_line": 48.5, "roof": "open"},
        {"season": 2025, "week": 4, "gameday": "2025-09-28", "home_team": "KC",
         "away_team": "DEN", "home_score": 20, "away_score": 17,
         "spread_line": 3.5, "total_line": 44.5, "roof": "open"},
    ])


def test_series_finds_only_that_pairing():
    series = h2h.team_series("BUF", "MIA", _history())
    assert series.n == 4
    assert all({m.home, m.away} == {"BUF", "MIA"} for m in series.meetings)


def test_series_is_newest_first():
    series = h2h.team_series("BUF", "MIA", _history())
    dates = [m.date for m in series.meetings]
    assert dates == sorted(dates, reverse=True)


def test_record_counts_from_team_a_perspective():
    series = h2h.team_series("BUF", "MIA", _history())
    assert series.record() == (3, 1, 0)
    flipped = h2h.team_series("MIA", "BUF", _history())
    assert flipped.record() == (1, 3, 0)


def test_margin_and_total():
    meeting = h2h.team_series("BUF", "MIA", _history()).meetings[0]
    assert meeting.margin == 17          # MIA 30 - BUF 13, MIA at home
    assert meeting.total == 43
    assert meeting.margin_for("MIA") == 17
    assert meeting.margin_for("BUF") == -17


def test_cover_and_over_use_the_posted_line():
    series = h2h.team_series("BUF", "MIA", _history())
    latest = series.meetings[0]
    # MIA home, spread_line -8.5 (underdog), won by 17 -> covered.
    assert latest.home_covered() is True
    assert latest.over_hit() is False     # 43 points against a 50.5 total
    assert series.totals_over_rate() == (2, 4)
    assert series.cover_rate_for("BUF") == (1, 4)


def test_missing_line_is_not_guessed():
    frame = _history()
    # A book that never posted a line leaves these blank, not zero.
    frame.loc[:, "spread_line"] = float("nan")
    frame.loc[:, "total_line"] = float("nan")
    series = h2h.team_series("BUF", "MIA", frame)
    assert series.cover_rate_for("BUF") is None
    assert series.totals_over_rate() is None


def test_no_meetings_is_reported_not_invented():
    series = h2h.team_series("BUF", "SEA", _history())
    assert series.n == 0
    assert series.avg_total() is None
    assert series.avg_margin_for("BUF") is None
    assert "No prior meetings" in series.summary_line()


def test_la_and_washington_abbreviations_match_across_sources():
    frame = pd.DataFrame([
        {"season": 2025, "week": 1, "gameday": "2025-09-07", "home_team": "LA",
         "away_team": "WAS", "home_score": 24, "away_score": 21,
         "spread_line": 3.0, "total_line": 45.0, "roof": "open"},
    ])
    # The DB stores ESPN spellings; the history frame uses nflverse ones.
    series = h2h.team_series("LAR", "WSH", frame)
    assert series.n == 1
    assert series.record() == (1, 0, 0)
    assert series.meetings[0].margin_for("LAR") == 3


# ------------------------------------------------------------ participation
def _game(**stats):
    base = {"event_id": "1", "date": "2026-09-13", "opponent": "MIA"}
    base.update(stats)
    return base


def test_did_play_rejects_a_rest_game():
    sat_out = _game(passingAttempts=0, rushingAttempts=0, passingYards=0)
    played = _game(passingAttempts=29, rushingAttempts=6, passingYards=334)
    assert h2h.did_play(sat_out, "QB") is False
    assert h2h.did_play(played, "QB") is True


def test_sample_drops_games_the_player_sat_out():
    sample = h2h.PlayerSample(
        athlete_id="1", name="Test QB", position="QB", team="BUF", opponent="MIA",
        vs_games=[_game(event_id="a", passingAttempts=30, passingYards=300),
                  _game(event_id="b", passingAttempts=0, passingYards=0)],
        recent_games=[_game(event_id="a", passingAttempts=30, passingYards=300),
                      _game(event_id="b", passingAttempts=0, passingYards=0)])
    assert len(sample.vs_games) == 1
    assert len(sample.recent_games) == 1
    assert sample.vs_average("passingYards") == 300


def test_unknown_position_is_never_filtered_out():
    assert h2h.did_play(_game(), "LS") is True


def test_stat_keys_follow_the_sport():
    mlb = h2h.PlayerSample(athlete_id="1", name="Batter", position="1B",
                           team="NYY", opponent="BOS", sport="mlb")
    assert "hits" in mlb.stat_keys()
    nfl = h2h.PlayerSample(athlete_id="2", name="Receiver", position="WR",
                           team="BUF", opponent="MIA")
    assert "receivingYards" in nfl.stat_keys()
