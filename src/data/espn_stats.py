"""Live player stats from ESPN's public endpoints (free, no key).

One client per sport; the sport registry supplies the path segments, so the
same code serves NFL, MLB and NHL.

Verified 2026-09-17 against live responses:

  - site.web.api.espn.com/apis/common/v3/sports/{sport}/{league}/athletes/{id}/gamelog
      -> {"names": [...stat keys...], "labels": [...], "seasonTypes": [
             {"displayName": "2026 Regular Season",
              "categories": [{"events": [{"eventId", "stats": [...]}]}]}],
          "events": {"<eventId>": {"week", "gameDate", "atVs", "score",
                                   "gameResult", "opponent": {"id","abbreviation"}}}}
      Accepts ?season=YYYY to pull a prior season. `names` is positional: it
      lines up index-for-index with each event's `stats` array.

  - sports.core.api.espn.com/.../seasons/{yr}/teams/{id}/athletes?limit=200
      -> {"count", "items": [{"$ref"}]}   (athlete refs, resolve for position)
  - sports.core.api.espn.com/.../teams/{id}/injuries
      -> {"count", "items": [{"$ref"}]}

site.api.espn.com (the legacy scoreboard host) 403s from this machine; the two
hosts above do not. Never guess a field name here - every key used below came
out of a real response.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import requests

from ..db.database import ROOT

CACHE_DIR = ROOT / "data" / "player_cache"
GAMELOG_HOST = "https://site.web.api.espn.com/apis/common/v3/sports"
CORE_HOST = "https://sports.core.api.espn.com/v2/sports"

# Gamelogs for finished games never change; current-season logs do.
FINISHED_TTL = 30 * 24 * 3600
LIVE_TTL = 20 * 60

# Stats ESPN does not report directly, built from ones it does.
# MLB total bases: singles + 2*doubles + 3*triples + 4*HR, which reduces to
# hits + doubles + 2*triples + 3*homeRuns.
DERIVED_STATS = {
    "mlb": {
        "totalBases": lambda r: (
            None if r.get("hits") is None else
            r["hits"] + (r.get("doubles") or 0) + 2 * (r.get("triples") or 0)
            + 3 * (r.get("homeRuns") or 0)),
    },
}

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class EspnStatsError(RuntimeError):
    pass


class EspnStats:
    """Player stats for one sport. Construct one per sport code."""

    def __init__(self, sport: str = "nfl", session: requests.Session | None = None):
        from . import sports as sport_registry
        self.sport = sport_registry.get(sport)
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = BROWSER_UA
        self.cache_dir = CACHE_DIR / self.sport.code
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def gamelog_base(self) -> str:
        return f"{GAMELOG_HOST}/{self.sport.espn_sport}/{self.sport.espn_league}/athletes"

    @property
    def core_base(self) -> str:
        return (f"{CORE_HOST}/{self.sport.espn_sport}/leagues/{self.sport.espn_league}")

    # --------------------------------------------------------------- cache
    def _get(self, url: str, params: dict | None = None,
             cache_name: str | None = None, ttl: int = LIVE_TTL) -> dict:
        path = self.cache_dir / cache_name if cache_name else None
        if path and path.exists() and time.time() - path.stat().st_mtime < ttl:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass  # corrupt cache entry - refetch below
        query = {"lang": "en", "region": "us", **(params or {})}
        resp = self.session.get(url, params=query, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if path:
            path.write_text(json.dumps(data), encoding="utf-8")
        return data

    # ------------------------------------------------------------ gamelog
    def gamelog(self, athlete_id: str | int, season: int | None = None) -> dict:
        """Raw gamelog payload for one athlete, optionally for a past season."""
        params = {"season": season} if season else None
        name = f"gamelog_{athlete_id}_{season or 'current'}.json"
        ttl = FINISHED_TTL if season and season < dt.date.today().year else LIVE_TTL
        return self._get(f"{self.gamelog_base}/{athlete_id}/gamelog", params, name, ttl)

    def game_rows(self, athlete_id: str | int, seasons: list[int] | None = None) -> list[dict]:
        """Flatten an athlete's gamelog into one row per game played.

        Each row carries the game context plus every stat ESPN reported for
        that player, keyed by ESPN's own `names` entries (passingYards,
        receivingYards, rushingAttempts, ...). Stats are floats; ESPN sends
        them as display strings and uses '-' for "did not record".
        """
        seasons = seasons or [None]
        rows: list[dict] = []
        seen: set[str] = set()
        for season in seasons:
            try:
                payload = self.gamelog(athlete_id, season)
            except requests.HTTPError:
                continue
            names = payload.get("names") or []
            events = payload.get("events") or {}
            for season_type in payload.get("seasonTypes") or []:
                label = season_type.get("displayName", "")
                # "2026 Regular Season" / "2024 Postseason"
                season_year = _leading_year(label) or season
                is_post = "Postseason" in label
                for category in season_type.get("categories") or []:
                    for entry in category.get("events") or []:
                        event_id = str(entry.get("eventId") or "")
                        if not event_id or event_id in seen:
                            continue
                        seen.add(event_id)
                        meta = events.get(event_id) or {}
                        row = {
                            "event_id": event_id,
                            "season": season_year,
                            "postseason": is_post,
                            "week": meta.get("week"),
                            "date": _iso_date(meta.get("gameDate")),
                            "opponent": (meta.get("opponent") or {}).get("abbreviation"),
                            "opponent_id": (meta.get("opponent") or {}).get("id"),
                            "home_away": "away" if meta.get("atVs") == "@" else "home",
                            "result": meta.get("gameResult"),
                            "score": meta.get("score"),
                        }
                        for i, key in enumerate(names):
                            stats = entry.get("stats") or []
                            row[key] = _num(stats[i]) if i < len(stats) else None
                        for key, build in DERIVED_STATS.get(self.sport.code, {}).items():
                            try:
                                row[key] = build(row)
                            except (TypeError, KeyError):
                                row[key] = None
                        rows.append(row)
        rows.sort(key=lambda r: (r["date"] or "", r["event_id"]), reverse=True)
        return rows

    def games_vs(self, athlete_id: str | int, opponent_abbrev: str,
                 seasons: list[int] | None = None) -> list[dict]:
        """That player's games against one specific opponent, newest first."""
        target = (opponent_abbrev or "").upper()
        return [r for r in self.game_rows(athlete_id, seasons)
                if (r.get("opponent") or "").upper() == target]

    # -------------------------------------------------------------- roster
    def roster(self, team_espn_id: str | int, season: int | None = None,
               skill_only: bool = True) -> list[dict]:
        """Active athletes for a team: [{id, name, position, jersey}]."""
        season = season or dt.date.today().year
        listing = self._get(f"{self.core_base}/seasons/{season}/teams/{team_espn_id}/athletes",
                            {"limit": 200},
                            f"roster_{team_espn_id}_{season}.json", LIVE_TTL)
        out: list[dict] = []
        for item in listing.get("items", []):
            ref = item.get("$ref", "").split("?")[0]
            athlete_id = ref.rstrip("/").split("/")[-1]
            try:
                athlete = self._get(ref, None, f"athlete_{athlete_id}.json", FINISHED_TTL)
            except requests.HTTPError:
                continue
            position = ((athlete.get("position") or {}).get("abbreviation") or "").upper()
            if skill_only and position not in self.sport.positions():
                continue
            out.append({
                "id": str(athlete.get("id") or athlete_id),
                "name": athlete.get("displayName") or athlete.get("fullName") or "",
                "short_name": athlete.get("shortName") or "",
                "position": position,
                "jersey": athlete.get("jersey"),
            })
        return out

    # ------------------------------------------------------------ injuries
    def injuries(self, team_espn_id: str | int) -> list[dict]:
        """Current injury report: [{athlete_id, name, status, detail}]."""
        listing = self._get(f"{self.core_base}/teams/{team_espn_id}/injuries", {"limit": 100},
                            f"injuries_{team_espn_id}.json", LIVE_TTL)
        out: list[dict] = []
        for item in listing.get("items", []):
            ref = item.get("$ref", "").split("?")[0]
            try:
                record = self._get(ref, None, None, 0)
            except requests.HTTPError:
                continue
            athlete = record.get("athlete") or {}
            athlete_id = athlete.get("id")
            name = athlete.get("displayName", "")
            if athlete.get("$ref"):
                # The injury record only carries a ref; resolve it for id + name.
                athlete_ref = athlete["$ref"].split("?")[0]
                athlete_id = athlete_id or athlete_ref.rstrip("/").split("/")[-1]
                if not name:
                    try:
                        full = self._get(athlete_ref, None,
                                         f"athlete_{athlete_id}.json", FINISHED_TTL)
                        name = full.get("displayName") or full.get("fullName") or ""
                    except requests.HTTPError:
                        pass
            out.append({
                "athlete_id": str(athlete_id or ""),
                "name": name,
                "status": record.get("status", ""),
                "detail": (record.get("details") or {}).get("type", ""),
                "date": _iso_date(record.get("date")),
            })
        return out

    def injured_ids(self, team_espn_id: str | int) -> dict[str, str]:
        """athlete_id -> status, for the statuses that threaten availability."""
        out = {}
        for entry in self.injuries(team_espn_id):
            status = (entry.get("status") or "").lower()
            if status and status not in ("active",):
                out[entry["athlete_id"]] = entry["status"]
        return out


# ------------------------------------------------------------------ helpers
def _num(value) -> float | None:
    """ESPN sends stats as display strings; '-' means nothing recorded."""
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _iso_date(value: str | None) -> str | None:
    """'2026-09-13T17:00:00.000+00:00' -> '2026-09-13'."""
    if not value:
        return None
    return str(value)[:10]


def _leading_year(label: str) -> int | None:
    """'2026 Regular Season' -> 2026. NHL labels spans: '2025-26' -> 2025."""
    head = (label or "").split(" ")[0].split("-")[0]
    return int(head) if head.isdigit() and len(head) == 4 else None
