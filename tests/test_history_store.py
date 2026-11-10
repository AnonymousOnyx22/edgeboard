"""History store: empty sources must fail loudly, never model on nothing."""
import pandas as pd
import pytest

from src.data import espn_schedule, history_store, stats_nfl


def test_backfill_raises_when_the_source_returns_no_games(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_nfl, "schedule_dataframe",
                        lambda *a, **k: pd.DataFrame())
    with pytest.raises(history_store.HistoryUnavailableError):
        history_store.backfill("nfl", seasons=1, db_path=tmp_path / "empty.sqlite")


def test_load_history_raises_when_store_and_sources_are_all_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(history_store, "history_frame", lambda *a, **k: pd.DataFrame())

    def no_backfill(*a, **k):
        raise history_store.HistoryUnavailableError("source down")

    monkeypatch.setattr(history_store, "backfill", no_backfill)
    monkeypatch.setattr(espn_schedule, "load_or_build_history",
                        lambda *a, **k: pd.DataFrame())
    with pytest.raises(history_store.HistoryUnavailableError):
        history_store.load_history("nfl", db_path=tmp_path / "x.sqlite")


def test_load_history_prefers_espn_cache_when_the_store_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(history_store, "history_frame", lambda *a, **k: pd.DataFrame())

    def no_backfill(*a, **k):
        raise history_store.HistoryUnavailableError("source down")

    monkeypatch.setattr(history_store, "backfill", no_backfill)
    fallback = pd.DataFrame([{"season": 2025, "week": 1, "gameday": "2025-09-07",
                              "home_team": "BUF", "away_team": "MIA",
                              "home_score": 24, "away_score": 21}])
    monkeypatch.setattr(espn_schedule, "load_or_build_history",
                        lambda *a, **k: fallback)
    frame = history_store.load_history("nfl", db_path=tmp_path / "y.sqlite")
    assert len(frame) == 1


def test_load_history_returns_empty_only_when_the_caller_opted_out(tmp_path, monkeypatch):
    monkeypatch.setattr(history_store, "history_frame", lambda *a, **k: pd.DataFrame())
    frame = history_store.load_history("nfl", auto_backfill=False,
                                       db_path=tmp_path / "z.sqlite")
    assert frame.empty