"""ESPN public core API client (free, no key).

Verified 2026-09-16 against live responses:
  - GET /v2/sports/{sport}/leagues/{league}/events?dates=YYYYMMDD-YYYYMMDD
    -> {"count", "pageIndex", "pageSize", "pageCount", "items":[{"$ref"}]}
  - event: id, date (ISO8601 UTC), name, shortName, competitions[0]:
      competitors: [{homeAway, team:{$ref}, winner, id}]
  - competition status ref -> {type:{state:"pre|in|post", completed, detail}}
  - competitor score ref  -> {value, displayValue}
  - competition odds ref  -> {items:[{provider:{name}, details, spread, overUnder, moneyline}]}

The legacy site.api.espn.com scoreboard returns 403 from this machine, so all
fetches go through sports.core.api.espn.com.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..db import database as db

ET = ZoneInfo("America/New_York")
CORE_BASE = "https://sports.core.api.espn.com/v2/sports"
SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "espn_cache"
CACHE_TTL_SECONDS = 15 * 60

SPORT_LEAGUES = {
    "nfl": ("football", "nfl"),
    "mlb": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"),
    "nba": ("basketball", "nba"),
    "wnba": ("basketball", "wnba"),
}

# NFL kickoff windows by US/Eastern start time (config-safe defaults). These
# names are local to this module's own computation, not an external API.
NFL_WINDOW_BY_HOUR = [
    (0, 11, "sunday_early"),     # 00:00-11:59 ET Sunday (London/morning window)
    (12, 15, "sunday_early"),
    (16, 19, "sunday_late"),
    (20, 23, "sunday_night"),
]


class EspnClient:
    def __init__(self, cache_ttl: int = CACHE_TTL_SECONDS):
        self.cache_ttl = cache_ttl
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (research script; no key)"
        # ESPN answers the odd burst with a 503 that clears in seconds; retry
        # with backoff rather than lose the hour. The pool matches the threads
        # the board and live refresh read with.
        retry = Retry(total=3, backoff_factor=1.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET",))
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
        self.session.mount("https://", adapter)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------- cache
    def _cache_get(self, name: str) -> dict | None:
        path = CACHE_DIR / name
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        return json.loads(path.read_text())

    def _cache_set(self, name: str, data) -> None:
        (CACHE_DIR / name).write_text(json.dumps(data))

    def _get_json(self, url: str, cache_name: str | None = None) -> dict:
        if cache_name and (cached := self._cache_get(cache_name)) is not None:
            return cached
        # No lang/region params: they are ESPN's defaults anyway, and since
        # 2026-11-09 the basketball feed answers 503 to any request carrying them.
        # ESPN's $refs come back as http://, which the basketball feed 503s on.
        url = url.replace("http://", "https://", 1)
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if cache_name:
            self._cache_set(cache_name, data)
        return data

    # -------------------------------------------------------------- events
    def events(self, sport: str, day: dt.date) -> list[dict]:
        """Resolve each event's $ref into a full event resource for a day."""
        sport_slug, league = SPORT_LEAGUES[sport]
        dstr = day.strftime("%Y%m%d")
        listing = self._get_json(
            f"{CORE_BASE}/{sport_slug}/leagues/{league}/events?dates={dstr}",
            cache_name=f"{sport}_{dstr}_events.json",
        )
        events: list[dict] = []
        for item in listing.get("items", []):
            ref = item["$ref"].split("?")[0]
            ev = self._get_json(ref, cache_name=f"event_{sport}_{ref.rstrip('/').split('/')[-1]}.json")
            events.append(ev)
        return events

    def site_events(self, sport: str, day: dt.date) -> list[dict]:
        """A day's events from the site scoreboard, everything inline.

        The core API goes down league by league ("Backend fetch failed")
        while this keeps answering. Teams, scores and status come in the one
        response, in shapes the helpers below already read.
        """
        sport_slug, league = SPORT_LEAGUES[sport]
        board = self._get_json(
            f"{SITE_BASE}/{sport_slug}/{league}/scoreboard?dates={day.strftime('%Y%m%d')}",
            cache_name=f"{sport}_{day.strftime('%Y%m%d')}_scoreboard.json",
        )
        return board.get("events", [])

    # --------------------------------------------------------- competitions
    def competition(self, event: dict) -> dict:
        comp = event["competitions"][0]
        if "$ref" in comp:
            return self._get_json(comp["$ref"].split("?")[0],
                                  cache_name=f"comp_{comp['id']}.json")
        return comp

    def status(self, comp: dict) -> dict:
        """Return normalized {state, completed, detail} from a competition."""
        status_obj = comp.get("status", {})
        if "$ref" in status_obj:
            st = self._get_json(status_obj["$ref"].split("?")[0],
                                cache_name=f"status_{comp['id']}.json")
        else:
            st = status_obj
        t = st.get("type", {})
        raw_state = t.get("state")
        state = {
            "pre": "scheduled",
            "in": "in_progress",
            "post": "final" if t.get("completed", False) else "postponed",
        }.get(raw_state, raw_state)
        return {"state": state, "completed": t.get("completed", False),
                "detail": t.get("detail"), "clock": st.get("displayClock", "")}

    def competitor_scores(self, comp: dict) -> dict[int, int]:
        """map competitor order -> final score (0 if game not final)."""
        scores: dict[int, int] = {}
        for c in comp.get("competitors", []):
            raw = c.get("score")
            if not isinstance(raw, dict):      # site scoreboard: inline "102"
                try:
                    scores[c.get("order", 0)] = int(float(raw or 0))
                except (TypeError, ValueError):
                    scores[c.get("order", 0)] = 0
                continue
            ref = raw.get("$ref")
            if not ref:
                scores[c["order"]] = 0
                continue
            sc = self._get_json(ref.split("?")[0], cache_name=f"score_{comp['id']}_{c['order']}.json")
            scores[c["order"]] = sc.get("value") or 0
        return scores

    def team_identity(self, comp: dict) -> list[dict]:
        """[(homeAway, team_id, display_name)] using team refs."""
        out: list[dict] = []
        for c in sorted(comp.get("competitors", []), key=lambda x: x.get("order", 0)):
            ref = c.get("team", {}).get("$ref")
            if not ref:
                team = c.get("team") or {}
                if team.get("id"):             # site scoreboard: team inline
                    out.append({
                        "home_away": c.get("homeAway"),
                        "espn_id": str(team["id"]),
                        "name": team.get("displayName", team.get("name", "")),
                        "abbrev": team.get("abbreviation", ""),
                    })
                continue
            clean_ref = ref.split("?")[0]
            team_id = str(c.get("team", {}).get("id") or clean_ref.rstrip("/").split("/")[-1])
            # Team ids repeat across leagues (NFL team 2 and MLB team 2 are
            # different clubs), so the cache key has to carry the league or one
            # sport's sync silently renames another's teams.
            league = _league_from_ref(clean_ref)
            team = self._get_json(clean_ref, cache_name=f"team_{league}_{team_id}.json")
            out.append({
                "home_away": c.get("homeAway"),
                "espn_id": team_id,
                "name": team.get("displayName", team.get("name", "")),
                "abbrev": team.get("abbreviation", ""),
            })
        return out

    def backup_odds(self, comp: dict) -> list[dict]:
        """Odds embedded in the competition (provider lines) - used as backup.

        Verified 2026-09-16: item has homeTeamOdds/awayTeamOdds with
        {favorite, moneyLine, spreadOdds}, top-level overOdds/underOdds,
        spread (signed for home, favorite negative), overUnder."""
        if "$ref" not in comp.get("odds", {}):
            return []
        odds = self._get_json(comp["odds"]["$ref"].split("?")[0],
                              cache_name=f"odds_backup_{comp['id']}.json")
        out = []
        for item in odds.get("items", []):
            home = item.get("homeTeamOdds") or {}
            away = item.get("awayTeamOdds") or {}
            if not home and not away:
                continue
            out.append({
                "provider": ref_name(item.get("provider")) or "",
                "home_moneyline": _num(home.get("moneyLine")),
                "away_moneyline": _num(away.get("moneyLine")),
                "home_spread_odds": _num(home.get("spreadOdds")),
                "away_spread_odds": _num(away.get("spreadOdds")),
                "spread": item.get("spread"),          # signed for home (fav = neg)
                "over_odds": _num(item.get("overOdds")),
                "under_odds": _num(item.get("underOdds")),
                "over_under": item.get("overUnder"),
            })
        return out

    @staticmethod
    def nfl_kickoff_window(start_utc_iso: str) -> str:
        """Label the NFL kickoff window from the US/Eastern start time."""
        start = dt.datetime.fromisoformat(start_utc_iso.replace("Z", "+00:00")).astimezone(ET)
        wd = start.weekday()  # Mon=0 ... Sun=6
        if wd == 0:
            return "monday_night"
        if wd == 3:
            return "thursday"
        if wd == 6:
            hour = start.hour
            for lo, hi, window in NFL_WINDOW_BY_HOUR:
                if lo <= hour <= hi:
                    return window
            return "sunday_night"
        # SNF/MNF occasionally move to other weekdays (holiday games)
        if start.hour >= 20:
            return "sunday_night"
        return ""

    # ------------------------------------------------------------- persist
    def upsert_games(self, sport: str, day: dt.date, db_path=db.DEFAULT_DB) -> int:
        """Fetch day's events, upsert teams + games. Returns # games."""
        try:
            return self._store_events(sport, self.events(sport, day), db_path)
        except requests.RequestException:
            # Any core read failing (listing, score, team) moves the whole day
            # to the site scoreboard. Rows are keyed on ESPN's id, so games the
            # core pass already wrote are simply written again.
            return self._store_events(sport, self.site_events(sport, day), db_path)

    def _store_events(self, sport: str, events: list[dict], db_path) -> int:
        sport_row = db.query(db_path, "SELECT id FROM sports WHERE code=?", (sport,))[0]
        sport_id = sport_row["id"]
        n = 0
        for ev in events:
            comp = self.competition(ev)
            teams = self.team_identity(comp)
            by_side = {t["home_away"]: t for t in teams}
            away, home = by_side.get("away"), by_side.get("home")
            if not away or not home:
                continue
            away_id = self._upsert_team(db_path, sport_id, away)
            home_id = self._upsert_team(db_path, sport_id, home)
            status = self.status(comp)
            scores = self.competitor_scores(comp)
            away_score, home_score = None, None
            for competitor in comp.get("competitors", []):
                score = scores.get(competitor.get("order", 0))
                if competitor.get("homeAway") == "away":
                    away_score = score
                elif competitor.get("homeAway") == "home":
                    home_score = score
            start_utc = ev.get("date", "")
            game_date = (dt.datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
                         .astimezone(ET).date().isoformat())
            window = self.nfl_kickoff_window(start_utc) if sport == "nfl" else None
            venue, venue_city = self.venue(comp)

            cur = db.connect(db_path)
            row = cur.execute("SELECT id FROM games WHERE external_id=?", (ev.get("id"),)).fetchone()
            state = status.get("state") or "scheduled"
            if row:
                cur.execute(
                    """UPDATE games SET away_team=?, home_team=?, start_utc=?, game_date=?, slate=?,
                       status=?, away_score=?, home_score=?,
                       venue=COALESCE(?, venue), venue_city=COALESCE(?, venue_city)
                       WHERE id=?""",
                    (away_id, home_id, start_utc, game_date, window, state,
                     away_score, home_score, venue, venue_city, row["id"]))
            else:
                cur.execute(
                    """INSERT INTO games (sport_id, external_id, away_team, home_team,
                       start_utc, game_date, slate, status, away_score, home_score,
                       venue, venue_city)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sport_id, ev.get("id"), away_id, home_id, start_utc, game_date, window,
                     state, away_score, home_score, venue, venue_city))
            cur.commit()
            cur.close()
            n += 1
        return n

    @staticmethod
    def venue(comp: dict) -> tuple[str | None, str | None]:
        """(stadium, "City, State") for one competition, or (None, None).

        ESPN sends the venue as a linked resource, so a payload that was
        fetched without it expanded carries only a $ref. Both parts are
        optional and the caller keeps whatever it already had.
        """
        venue = comp.get("venue") or {}
        name = venue.get("fullName") or None
        address = venue.get("address") or {}
        parts = [address.get("city"), address.get("state")]
        city = ", ".join(part for part in parts if part) or None
        return name, city

    def _upsert_team(self, db_path, sport_id: int, info: dict) -> int:
        cur = db.connect(db_path)
        row = cur.execute("SELECT id FROM teams WHERE sport_id=? AND league_key=?", (sport_id, info["espn_id"])).fetchone()
        if row:
            cur.execute("UPDATE teams SET name=?, abbrev=? WHERE id=?",
                        (info["name"], info["abbrev"], row["id"]))
            tid = row["id"]
        else:
            cursor = cur.execute("INSERT INTO teams (sport_id, league_key, name, abbrev) VALUES (?,?,?,?)",
                                 (sport_id, info["espn_id"], info["name"], info["abbrev"]))
            tid = cursor.lastrowid
        cur.commit()
        cur.close()
        return tid


def _league_from_ref(ref: str) -> str:
    """'.../leagues/nfl/seasons/2026/teams/2' -> 'nfl'."""
    parts = [p for p in (ref or "").split("/") if p]
    if "leagues" in parts:
        index = parts.index("leagues") + 1
        if index < len(parts):
            return parts[index]
    return "unknown"


def ref_name(ref_obj: dict | str | None) -> str | None:
    if not ref_obj:
        return None
    if isinstance(ref_obj, str):
        return ref_obj.split("/")[-1]
    if isinstance(ref_obj, dict):
        return ref_obj.get("name") or ref_obj.get("$ref", "").split("/")[-1]
    return None


def _num(v) -> float | None:
    """ESPN sometimes returns numbers as strings (e.g. '-110') - normalize."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
