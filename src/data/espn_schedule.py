"""Completed-game history from ESPN, for sports nflverse does not cover.

Verified 2026-09-17:

  GET site.web.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{id}/schedule
        ?season=YYYY
    -> {"events": [{"id", "date", "season", "week",
                    "competitions": [{"competitors": [
                        {"homeAway", "score": {"value"}, "winner",
                         "team": {"abbreviation"}}],
                      "status": {"type": {"state": "post"}}}]}]}

One request returns a team's whole season with scores already inline, which is
what makes this cheap enough to build league-wide history from: thirty-odd
requests per season rather than one per game.

NFL history still comes from nflverse, because that carries closing spreads and
totals as well as scores, and cover history is worth having.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd
import requests

from ..db.database import ROOT
from . import sports as sport_registry

CACHE_DIR = ROOT / "data" / "schedule_cache"
SCHEDULE_HOST = "https://site.web.api.espn.com/apis/site/v2/sports"
CORE_HOST = "https://sports.core.api.espn.com/v2/sports"
FINISHED_TTL = 7 * 24 * 3600
LIVE_TTL = 30 * 60

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Columns match the nflverse schedule frame the NFL model already consumes, so
# both histories can feed the same Elo trainer.
COLUMNS = ["game_id", "season", "week", "gameday", "home_team", "away_team",
           "home_score", "away_score", "spread_line", "total_line"]


class EspnSchedule:
    def __init__(self, sport: str = "mlb", session: requests.Session | None = None):
        self.sport = sport_registry.get(sport)
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = BROWSER_UA
        self.cache_dir = CACHE_DIR / self.sport.code
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, url: str, params: dict, cache_name: str, ttl: int) -> dict:
        path = self.cache_dir / cache_name
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        url = url.replace("http://", "https://", 1)  # ESPN refs are http://
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    # ------------------------------------------------------------- one team
    def team_ids(self) -> list[str]:
        """Every team id in the league."""
        listing = self._get(
            f"{CORE_HOST}/{self.sport.espn_sport}/leagues/{self.sport.espn_league}/teams",
            {"limit": 100},
            "team_ids.json", FINISHED_TTL)
        out = []
        for item in listing.get("items", []):
            ref = item.get("$ref", "").split("?")[0]
            out.append(ref.rstrip("/").split("/")[-1])
        return out

    def team_schedule(self, team_id: str | int, season: int) -> list[dict]:
        """Completed games for one team in one season, as history rows."""
        current = season >= dt.date.today().year
        payload = self._get(
            f"{SCHEDULE_HOST}/{self.sport.espn_sport}/{self.sport.espn_league}"
            f"/teams/{team_id}/schedule",
            {"season": season},
            f"schedule_{team_id}_{season}.json",
            LIVE_TTL if current else FINISHED_TTL)

        rows: list[dict] = []
        for event in payload.get("events", []):
            competitions = event.get("competitions") or []
            if not competitions:
                continue
            comp = competitions[0]
            state = ((comp.get("status") or {}).get("type") or {}).get("state")
            if state != "post":
                continue
            sides = {}
            for competitor in comp.get("competitors", []):
                score = (competitor.get("score") or {}).get("value")
                abbrev = (competitor.get("team") or {}).get("abbreviation")
                if score is None or not abbrev:
                    continue
                sides[competitor.get("homeAway")] = (abbrev, float(score))
            if "home" not in sides or "away" not in sides:
                continue
            rows.append({
                "game_id": str(event.get("id") or ""),
                "season": int(event.get("season", {}).get("year") or season),
                "week": int(event.get("week", {}).get("number") or 0),
                "gameday": str(event.get("date") or "")[:10],
                "home_team": sides["home"][0],
                "away_team": sides["away"][0],
                "home_score": sides["home"][1],
                "away_score": sides["away"][1],
                "spread_line": None,   # ESPN's schedule feed carries no closing line
                "total_line": None,
            })
        return rows

    # ---------------------------------------------------------- whole league
    def history(self, seasons: list[int] | None = None,
                progress=None) -> pd.DataFrame:
        """League-wide completed games, deduplicated, as a schedule frame."""
        seasons = seasons or [dt.date.today().year]
        teams = self.team_ids()
        seen: set[str] = set()
        rows: list[dict] = []
        total = max(1, len(teams) * len(seasons))
        done = 0
        for season in seasons:
            for team_id in teams:
                try:
                    for row in self.team_schedule(team_id, season):
                        if row["game_id"] and row["game_id"] not in seen:
                            seen.add(row["game_id"])
                            rows.append(row)
                except requests.HTTPError:
                    pass
                done += 1
                if progress:
                    progress(done / total, f"{self.sport.name} {season}")
        frame = pd.DataFrame(rows, columns=COLUMNS)
        if not frame.empty:
            frame = frame.sort_values("gameday").reset_index(drop=True)
        return frame


def load_or_build_history(sport: str = "mlb", seasons: list[int] | None = None,
                          progress=None) -> pd.DataFrame:
    """Cached league history for a sport. NFL is served by nflverse instead."""
    config = sport_registry.get(sport)
    if config.history_source == "nflverse":
        from . import models
        return models.load_or_build_history()

    seasons = seasons or _default_seasons(config)
    cache = CACHE_DIR / config.code / f"history_{min(seasons)}_{max(seasons)}.parquet"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and time.time() - cache.stat().st_mtime < LIVE_TTL:
        try:
            return pd.read_parquet(cache)
        except (OSError, ValueError):
            pass
    frame = EspnSchedule(config.code).history(seasons, progress=progress)
    if not frame.empty:
        try:
            frame.to_parquet(cache, index=False)
        except (OSError, ImportError):
            pass
    return frame


def _default_seasons(config) -> list[int]:
    """Current season and the two before it."""
    year = dt.date.today().year
    return [year - 2, year - 1, year]
