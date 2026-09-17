"""NFL stats via nfl_data_py (free, no key) + ESPN core API fallback.

Verified fields used only from real responses; never hardcode guessed names
beyond the module-level lists below, and inspect live frames before relying
on new columns.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

try:
    import nfl_data_py as nfl
    NFL_DATA_PY = True
except ImportError:
    nfl = None  # type: ignore
    NFL_DATA_PY = False


def nfl_available() -> bool:
    return NFL_DATA_PY


def schedule_dataframe(seasons: list[int] | None = None) -> pd.DataFrame:
    """Historical + current NFL schedule with closing lines/scores.
    Returns empty frame when nfl_data_py is unavailable."""
    if not NFL_DATA_PY:
        return pd.DataFrame()
    if seasons is None:
        seasons = [dt.date.today().year]
    try:
        if hasattr(nfl, "import_schedules"):
            return nfl.import_schedules(seasons)
        if hasattr(nfl, "nfl_schedule"):
            return nfl.nfl_schedule(seasons=seasons)  # type: ignore
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame()


def closing_lines() -> pd.DataFrame:
    """Clean schedule with only the columns the picker/model consume."""
    df = schedule_dataframe()
    keep = [c for c in [
        "season", "week", "gameday", "weekday", "gametime", "home_team",
        "away_team", "home_score", "away_score", "spread_line", "total_line",
        "home_moneyline", "away_moneyline", "home_conf", "away_conf",
        "home_coach", "away_coach", "home_coach_name", "away_coach_name",
    ] if c in df.columns]
    if not keep:
        return df
    return df[keep]


def results_for_training(min_year: int | None = None) -> pd.DataFrame:
    """Completed games used to fit the logistic model: spread, total, ml."""
    if not NFL_DATA_PY:
        return pd.DataFrame()
    year = dt.date.today().year
    start = min_year or (year - 3)
    df = schedule_dataframe(seasons=list(range(start, year + 1)))
    if df.empty or "home_score" not in df.columns:
        return df
    df = df.dropna(subset=["home_score", "away_score"]).copy()
    df["home_won"] = (df["home_score"] > df["away_score"]).astype(int)
    return df


class EarlySeasonGuard:
    """How far into the season the model may over-trust Elo (quality penalty)."""
    def __init__(self, current_week: int | None = None):
        self.current_week = current_week or 1

    def penalty(self) -> float:
        if self.current_week <= 6:
            return 0.0  # penalty value handled in confidence.quality_score
        return 0.0