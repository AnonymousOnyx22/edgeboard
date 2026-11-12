"""NFL game model: Elo ratings + logistic regression on schedule history.

Schedule data comes from nfl_data_py (free). Elo is trained sequentially on
completed games; a logistic regression optionally blends on simple features
(elo diff, ytd win% diff, rest days diff). Neither requires possession/play
data, so it only ever touches the lightweight schedule pull.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from ..db.database import ROOT

CACHE_DIR = ROOT / "data" / "schedule_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

START_ELO = 1500.0
K_FACTOR = 20.0
# NFL defaults, kept as module constants for callers that predate the sport
# registry. Per-sport values come from data/sports.py.
ELO_HOME_ADV = 60.0


def _h2h_expected_elo(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** (- (rating_a - rating_b) / 400.0))


class EloSystem:
    """Elo ratings for one league.

    The rating maths is the same everywhere; only the scale differs. Home
    advantage and the points-per-rating-point slope come from the sport
    registry, because 100 Elo points is worth ~2.75 points in the NFL and a
    fraction of a run in baseball.
    """

    def __init__(self, sport: str = "nfl") -> None:
        from . import sports as sport_registry
        self.sport = sport_registry.get(sport)
        self.ratings: dict[str, float] = {}
        self.games_seen: dict[str, int] = {}

    @property
    def home_advantage(self) -> float:
        return self.sport.elo_home_advantage

    @property
    def points_per_100(self) -> float:
        return self.sport.points_per_100_elo

    def get(self, team: str) -> float:
        return self.ratings.get(team, START_ELO)

    def games(self, team: str) -> int:
        return self.games_seen.get(team, 0)

    def update(self, home: str, away: str, home_margin: float) -> None:
        """home_margin > 0 means home won by that many points."""
        expected_home = _h2h_expected_elo(self.get(home) + self.home_advantage,
                                          self.get(away))
        margin_weight = max(1.0, math.log1p(abs(home_margin)))  # K scales with blowout
        k = K_FACTOR * margin_weight
        actual_home = 1.0 if home_margin > 0 else (0.0 if home_margin < 0 else 0.5)
        self.ratings[home] = self.get(home) + k * (actual_home - expected_home)
        self.ratings[away] = self.get(away) - k * (actual_home - expected_home)
        self.games_seen[home] = self.games_seen.get(home, 0) + 1
        self.games_seen[away] = self.games_seen.get(away, 0) + 1

    def predict(self, home: str, away: str, sd: float | None = None) -> dict:
        """home_win_prob, margin mean/sd."""
        diff = self.get(home) + self.home_advantage - self.get(away)
        p_home = _h2h_expected_elo(self.get(home) + self.home_advantage, self.get(away))
        mu = diff * self.points_per_100 / 100.0
        return {"home_win_prob": p_home, "margin_mean": mu,
                "margin_sd": sd or self.sport.margin_sd}


def train_elo(history: pd.DataFrame, until_date: dt.date | None = None,
              sport: str = "nfl") -> EloSystem:
    """Train Elo on all completed games up to `until_date` (default: all)."""
    elo = EloSystem(sport)
    if history.empty:
        return elo
    df = history.dropna(subset=["home_team", "away_team", "home_score", "away_score"]).copy()
    if "gameday" in df.columns:
        df["gameday"] = pd.to_datetime(df["gameday"], errors="coerce")
        if until_date:
            df = df[df["gameday"].dt.date <= until_date]
        df = df.sort_values("gameday")
    for _, row in df.iterrows():
        margin = row["home_score"] - row["away_score"]
        elo.update(row["home_team"], row["away_team"], margin)
    return elo


def _ytd_winpct(hist: pd.DataFrame, team: str, current_row: pd.Series) -> float:
    """Win% before the current game (schedule order within season)."""
    try:
        idx = current_row.name
    except Exception:
        return 0.5
    before = hist.iloc[:int(idx)] if hasattr(idx, "__index__") and isinstance(idx, int) else hist
    played = before[before["home_team"] == team]
    if played.empty:
        played = before[before["away_team"] == team]
        if played.empty:
            return 0.5
        wins = sum(played["away_score"].to_numpy() > played["home_score"].to_numpy())
    else:
        wins = sum(played["home_score"].to_numpy() > played["away_score"].to_numpy())
    return wins / len(played)


def load_or_build_history(min_year: int | None = None) -> pd.DataFrame:
    """Schedule history cached as parquet for a day (nfl_data_py pull is heavy)."""
    import pandas as pd

    today = dt.date.today()
    cache_path = CACHE_DIR / f"schedule_{today.isoformat()}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)
    from . import stats_nfl
    year = today.year
    start = min_year or (year - 2)
    df = stats_nfl.schedule_dataframe(seasons=list(range(start, year + 1)))
    if not df.empty:
        df.to_parquet(cache_path, index=False)
    return df


def combined_probability(elo_predict: dict, logistic_p: float | None,
                         elo_weight: float = 0.6) -> dict:
    """Blend Elo win prob with logistic estimate (if available)."""
    p = elo_predict["home_win_prob"]
    if logistic_p is not None:
        p = elo_weight * p + (1 - elo_weight) * logistic_p
    out = dict(elo_predict)
    out["home_win_prob"] = max(0.02, min(0.98, p))
    return out
