"""Client for The Odds API (free tier, 500 requests/month).

Rules enforced here:
  - region = "us", markets = h2h,spreads,totals only (no props)
  - results are cached on disk; the same market set is never re-fetched for
    the same day unless cache_days windows have passed (default 1 day)
  - the x-requests-remaining header is read and persisted so the dashboard
    can show remaining credits
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from ..db.database import ROOT

load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
ODDS_CACHE = DATA_DIR / "odds_cache"
ODDS_CACHE.mkdir(parents=True, exist_ok=True)

CREDITS_FILE = DATA_DIR / "credits.json"

DEFAULT_KEY = os.getenv("ODDS_API_KEY", "")


# Books whose prices this tool is willing to quote. The Odds API's "us"
# region also returns offshore books - Bovada, LowVig, BetOnline, MyBookie -
# which are not available in Ontario. Taking the best price across all of them
# invents edges against numbers nobody here can actually bet, so the list is
# an allowlist rather than a preference. Empty means every book.
#
# theScore Bet is not offered by The Odds API in any region (checked against
# us, us2, uk, eu and au), so it cannot be quoted from this data source.
DEFAULT_BOOKS = ["draftkings", "fanduel", "betmgm", "betrivers"]


def allowed_books(config: dict | None = None) -> list[str]:
    """Lower-cased book keys that may be quoted; empty list means all."""
    if config is None:
        from ..db.database import get_config
        config = get_config()
    block = (config or {}).get("odds") or {}
    # `bookmakers: ~` in settings.yaml means "every book the feed returns".
    books = block.get("bookmakers", DEFAULT_BOOKS)
    if not books:
        return []
    return [str(b).strip().lower() for b in books if str(b).strip()]


def preferred_book(config: dict | None = None) -> str:
    """The book whose line wins when several offer the same player prop, or ''.

    The best price across books can sit on a different line from the one you
    actually bet at, so a prop is quoted from this book whenever it has one.
    """
    if config is None:
        from ..db.database import get_config
        config = get_config()
    return str(((config or {}).get("odds") or {}).get("preferred_book") or "").strip().lower()


class OddsAPIError(RuntimeError):
    pass


class OddsClient:
    def __init__(self, api_key: str | None = None, base: str = "https://api.the-odds-api.com/v4/sports"):
        self.api_key = api_key if api_key is not None else DEFAULT_KEY
        self.base = base
        self.session = requests.Session()

    # ------------------------------------------------------------------ creds
    def _save_credit_headers(self, resp: requests.Response) -> None:
        """Persist x-requests-remaining so the dashboard can show credits."""
        remaining = resp.headers.get("x-requests-remaining")
        used = resp.headers.get("x-requests-used")
        if remaining is not None:
            CREDITS_FILE.write_text(json.dumps({
                "remaining": int(remaining),
                "used": int(used or 0),
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }, indent=2))

    @staticmethod
    def credits() -> dict | None:
        if CREDITS_FILE.exists():
            try:
                return json.loads(CREDITS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                return None
        return None

    # --------------------------------------------------------------- sports
    def _sports_cache_path(self) -> Path:
        return ODDS_CACHE / "sports_list.json"

    def sports_list(self, force: bool = False) -> list[dict]:
        """Map of Odds-API sport keys (e.g. americanfootball_nfl). Cached for 1 day."""
        path = self._sports_cache_path()
        if not force and path.exists():
            try:
                age = dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)
                if age < dt.timedelta(days=1):
                    sport_meta, _ = json.loads(path.read_text(encoding="utf-8"))
                    return sport_meta
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        try:
            resp = self.session.get(self.base, params={"apiKey": self.api_key}, timeout=30)
        except requests.RequestException as exc:
            raise OddsAPIError(f"sports list request failed: {exc}") from exc
        self._save_credit_headers(resp)
        if resp.status_code == 401:
            raise OddsAPIError("Invalid ODDS_API_KEY. Get a free key at https://the-odds-api.com")
        if resp.status_code != 200:
            raise OddsAPIError(f"sports_list failed HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OddsAPIError("sports list returned invalid JSON") from exc
        # Raw response saved for inspection - never guess field names.
        (DATA_DIR / "samples").mkdir(exist_ok=True)
        (DATA_DIR / "samples" / "sports_list_0_raw.json").write_text(json.dumps(payload, indent=2))
        path.write_text(json.dumps([payload, dt.datetime.now().isoformat()]))
        return payload

    def active_sports(self, codes: dict[str, str] | None = None) -> dict[str, str]:
        """Return {short_code: odds_api_key} for each active sport."""
        if codes is None:
            # default short->api keys. Verify against live sports_list before use.
            codes = {
                "nfl": "americanfootball_nfl",
                "mlb": "baseball_mlb",
                "nhl": "icehockey_nhl",
                "nba": "basketball_nba",
                "wnba": "basketball_wnba",
            }
        out: dict[str, str] = {}
        for short, key in codes.items():
            found = self._resolve_key(short, key)
            if found:
                out[short] = found
        return out

    def _resolve_key(self, short: str, guess: str) -> str | None:
        """Find the right API key from the live sports list (never assume)."""
        try:
            sports = self.sports_list()
        except OddsAPIError:
            return guess
        candidates = [s for s in sports if s.get("active")]
        exact = [s for s in candidates if s["key"] == guess]
        if exact:
            return exact[0]["key"]
        # e.g. guess "americanfootball_nfl" but key could differ: match on group
        token = short.upper()
        for s in candidates:
            if token in (s.get("group") or "").upper():
                return s["key"]
        return None

    # ----------------------------------------------------------------- odds
    def _cache_path(self, sport_key: str) -> Path:
        safe = sport_key.replace("/", "_")
        return ODDS_CACHE / f"{safe}_h2h_spreads_totals.json"

    def fetch_odds(self, sport_key: str, force: bool = False, samples_dir: Path = DATA_DIR / "samples") -> list[dict]:
        """Fetch h2h+spreads+totals odds for one sport. Cache-aware, region us only."""
        cache = self._cache_path(sport_key)
        if not force and cache.exists():
            try:
                age = dt.datetime.now() - dt.datetime.fromtimestamp(cache.stat().st_mtime)
                if age < dt.timedelta(days=1):
                    cached = json.loads(cache.read_text(encoding="utf-8"))
                    _, payload = cached if self._cache_has_meta(cache) else (None, cached)
                    return payload
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass

        url = f"{self.base}/{sport_key}/odds/"
        try:
            resp = self.session.get(url, params={
                "apiKey": self.api_key,
                "regions": "us",
                "markets": "h2h,spreads,totals",
                # The API defaults to decimal. Everything downstream - the odds
                # table, the edge maths, the display - is American.
                "oddsFormat": "american",
            }, timeout=30)
        except requests.RequestException as exc:
            raise OddsAPIError(f"odds request for {sport_key} failed: {exc}") from exc
        self._save_credit_headers(resp)
        if resp.status_code != 200:
            raise OddsAPIError(f"odds for {sport_key} failed HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OddsAPIError(f"odds for {sport_key} returned invalid JSON") from exc
        samples_dir.mkdir(exist_ok=True)
        (samples_dir / f"odds_{sport_key}_0_raw.json").write_text(json.dumps(payload, indent=2))
        cache.write_text(json.dumps([dt.datetime.now().isoformat(), payload]))
        return payload

    @staticmethod
    def _cache_has_meta(cache: Path) -> bool:
        try:
            data = json.loads(cache.read_text())
            return isinstance(data, list) and len(data) == 2 and isinstance(data[0], str)
        except Exception:
            return False

    @staticmethod
    def best_price(outcomes: list[dict], name: str) -> dict | None:
        """Best available American price across any outcomes matching `name`."""
        best = None
        for o in outcomes:
            if o.get("name") == name or (isinstance(name, str) and o.get("name") and o["name"].lower() == name.lower()):
                if best is None or o["price"] > best["price"]:
                    best = o
        return best


def load_odds_into_sport_keys(api_key: str | None = None) -> dict[str, list[dict]]:
    """Fetch today odds for all *active* supported sports. Returns {short: payload}."""
    client = OddsClient(api_key)
    sports = client.active_sports()
    result: dict[str, list[dict]] = {}
    for short, key in sports.items():
        try:
            result[short] = client.fetch_odds(key)
        except OddsAPIError:
            continue
    return result
