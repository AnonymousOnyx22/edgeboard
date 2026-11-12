"""Player-prop prices from The Odds API, for any sport in the registry.

Verified 2026-09-17 against live responses for both NFL and MLB:

  GET /v4/sports/{sport_key}/events
      -> [{"id", "commence_time", "home_team", "away_team"}]   costs 0 credits
  GET /v4/sports/{sport_key}/events/{id}/odds
        ?regions=us&markets=player_pass_yds,...&oddsFormat=american
      -> {"bookmakers": [{"key", "markets": [{"key", "outcomes": [
             {"description": "Josh Allen",   <- the player
              "name": "Over" | "Under",
              "point": 253.5,
              "price": -113}]}]}]}

Credits are the binding constraint: this endpoint bills **one credit per
market per event**, so a nine-market pull across sixteen games is 144 of a
free plan's 500 monthly credits. Everything here is therefore per-event, on
demand, cached hard on disk, and refused outright when the remaining balance
falls below `credit_floor`.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import time

import requests
from dotenv import load_dotenv

from ..db.database import ROOT
from . import sports as sport_registry
from .odds_client import CREDITS_FILE, OddsAPIError

load_dotenv(ROOT / ".env")

CACHE_DIR = ROOT / "data" / "props_cache"
API_HOST = "https://api.the-odds-api.com/v4/sports"
CACHE_TTL_SECONDS = 6 * 3600     # lines move, but not minute to minute

# Each sport's stat-key/market-key mapping lives in the sport registry.


class CreditGuard(OddsAPIError):
    """Raised instead of spending credits the user asked to protect."""


class PropsOddsClient:
    def __init__(self, sport: str = "nfl", api_key: str | None = None,
                 credit_floor: int = 50, depth: str = "core"):
        self.sport = sport_registry.get(sport)
        # "core" is a handful of markets; "all" is everything this sport can
        # model. Each market costs a credit per game, so the dial is explicit.
        self.depth = depth
        self.api_key = api_key if api_key is not None else os.getenv("ODDS_API_KEY", "")
        self.credit_floor = credit_floor
        self.session = requests.Session()
        self.cache_dir = CACHE_DIR / self.sport.code
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def api_base(self) -> str:
        return f"{API_HOST}/{self.sport.odds_key}"

    @property
    def default_markets(self) -> list[str]:
        return self.sport.markets_for(self.depth)

    # -------------------------------------------------------------- credits
    @staticmethod
    def credits() -> dict | None:
        if CREDITS_FILE.exists():
            try:
                return json.loads(CREDITS_FILE.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def remaining(self) -> int | None:
        info = self.credits()
        return int(info["remaining"]) if info else None

    def _save_credits(self, resp: requests.Response) -> None:
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is None:
            return
        CREDITS_FILE.write_text(json.dumps({
            "remaining": int(float(remaining)),
            "used": int(float(resp.headers.get("x-requests-used") or 0)),
            "last_cost": int(float(resp.headers.get("x-requests-last") or 0)),
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }, indent=2))

    def _check_budget(self, cost: int) -> None:
        left = self.remaining()
        if left is not None and left - cost < self.credit_floor:
            raise CreditGuard(
                f"Refusing to spend {cost} credits: {left} left, floor is "
                f"{self.credit_floor}. Lower the floor in Settings to override.")

    # --------------------------------------------------------------- events
    def events(self, force: bool = False) -> list[dict]:
        """Upcoming NFL events with their Odds API ids. Costs no credits."""
        path = self.cache_dir / "events.json"
        if not force and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_SECONDS:
            return json.loads(path.read_text())
        if not self.api_key:
            raise OddsAPIError("No ODDS_API_KEY set. Add one to .env.")
        resp = self.session.get(f"{self.api_base}/events",
                                params={"apiKey": self.api_key}, timeout=30)
        self._save_credits(resp)
        if resp.status_code == 401:
            raise OddsAPIError("Invalid ODDS_API_KEY.")
        if resp.status_code != 200:
            raise OddsAPIError(f"events failed HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        path.write_text(json.dumps(data))
        return data

    def find_event_id(self, home_name: str, away_name: str,
                      start_utc: str | None = None) -> str | None:
        """Match a DB game to its Odds API event by team names and kickoff."""
        candidates = []
        for event in self.events():
            home_score = _name_ratio(event.get("home_team", ""), home_name)
            away_score = _name_ratio(event.get("away_team", ""), away_name)
            if home_score < 0.6 or away_score < 0.6:
                continue
            gap = _time_gap(event.get("commence_time"), start_utc)
            candidates.append((home_score + away_score - gap, event["id"]))
        if not candidates:
            return None
        return max(candidates)[1]

    # ---------------------------------------------------------------- props
    def event_props(self, event_id: str, markets: list[str] | None = None,
                    force: bool = False) -> dict:
        """Prop prices for one event. Costs one credit per market."""
        markets = markets or self.default_markets
        key = _markets_key(markets)
        path = self.cache_dir / f"props_{event_id}_{key}.json"
        if not force and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_SECONDS:
            return json.loads(path.read_text())
        if not self.api_key:
            raise OddsAPIError("No ODDS_API_KEY set. Add one to .env.")

        self._check_budget(len(markets))
        resp = self.session.get(
            f"{self.api_base}/events/{event_id}/odds",
            params={"apiKey": self.api_key, "regions": "us",
                    "markets": ",".join(markets), "oddsFormat": "american"},
            timeout=40)
        self._save_credits(resp)
        if resp.status_code == 422:
            raise OddsAPIError(f"Odds API rejected those markets: {resp.text[:200]}")
        if resp.status_code != 200:
            raise OddsAPIError(f"props failed HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        path.write_text(json.dumps(data))
        return data

    def cached_event_props(self, event_id: str,
                           markets: list[str] | None = None) -> dict | None:
        """Whatever is already on disk for this event - never spends a credit."""
        markets = markets or self.default_markets
        key = _markets_key(markets)
        path = self.cache_dir / f"props_{event_id}_{key}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                return None
        return None


# ------------------------------------------------------------------ parsing
class PropBook:
    """Indexed prop prices for one event: (player, stat) -> offered lines."""

    def __init__(self, payload: dict | None, sport: str = "nfl"):
        self.payload = payload or {}
        self.sport = sport_registry.get(sport)
        self.market_to_stat = self.sport.market_to_stat
        self.offers: dict[tuple[str, str], list[dict]] = {}
        self._index()

    def _index(self) -> None:
        from .odds_client import allowed_books
        books = set(allowed_books())
        for book in self.payload.get("bookmakers", []):
            # Offshore books are priced here too, and a prop is no more
            # bettable than a game line at a book you cannot reach.
            if books and (book.get("key") or "").lower() not in books:
                continue
            for market in book.get("markets", []):
                stat_key = self.market_to_stat.get(market.get("key", ""))
                if not stat_key:
                    continue
                grouped: dict[tuple[str, float], dict] = {}
                for outcome in market.get("outcomes", []):
                    player = (outcome.get("description") or "").strip()
                    point = outcome.get("point")
                    side = (outcome.get("name") or "").lower()
                    if not player or point is None or side not in ("over", "under"):
                        continue
                    entry = grouped.setdefault((player, float(point)), {
                        "bookmaker": book.get("key", ""), "line": float(point),
                        "over": None, "under": None})
                    entry[side] = outcome.get("price")
                for (player, _point), entry in grouped.items():
                    self.offers.setdefault((_norm(player), stat_key), []).append(entry)

    def lines_for(self, player: str, stat_key: str) -> list[dict]:
        """Offered lines for a player-stat, best Over price first."""
        direct = self.offers.get((_norm(player), stat_key))
        if direct is None:
            direct = self._fuzzy(player, stat_key)
        return sorted(direct or [], key=lambda o: (o["over"] is None, -(o["over"] or -9999)))

    def _fuzzy(self, player: str, stat_key: str) -> list[dict] | None:
        """Odds API and ESPN spell a few names differently (Jr., III, accents)."""
        names = [key[0] for key in self.offers if key[1] == stat_key]
        match = difflib.get_close_matches(_norm(player), names, n=1, cutoff=0.86)
        return self.offers.get((match[0], stat_key)) if match else None

    def players(self) -> set[str]:
        return {key[0] for key in self.offers}

    @property
    def empty(self) -> bool:
        return not self.offers


def _markets_key(markets: list[str]) -> str:
    """Short, stable cache suffix for a market set."""
    trimmed = sorted(m.replace("player_", "").replace("batter_", "b_")
                     .replace("pitcher_", "p_") for m in markets)
    return "_".join(trimmed)[:120]


def _norm(name: str) -> str:
    """Comparable name: lowercase, no punctuation, no generational suffix."""
    cleaned = "".join(ch for ch in (name or "").lower() if ch.isalnum() or ch == " ")
    parts = [p for p in cleaned.split() if p not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return " ".join(parts)


def _name_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _time_gap(commence: str | None, start_utc: str | None) -> float:
    """Hours between two kickoff timestamps, as a small tie-breaking penalty."""
    if not commence or not start_utc:
        return 0.0
    try:
        a = dt.datetime.fromisoformat(commence.replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return min(abs((a - b).total_seconds()) / 3600.0, 48.0) / 48.0
