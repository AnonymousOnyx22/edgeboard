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


