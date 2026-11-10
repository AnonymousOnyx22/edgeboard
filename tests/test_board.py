"""Board safety: started games must never feed a pregame read."""
import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from src.data import props_odds as props_odds_mod
from src.picker import autopick
from src.picker import board as board_mod


def _future_card():
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
             ).isoformat().replace("+00:00", "Z")
    return board_mod.GameCard(game_id=1, home="BUF", away="MIA", start_utc=start,
                              kickoff_window="", status="scheduled",
                              home_name="Buffalo Bills", away_name="Miami Dolphins")


def _started_card():
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
             ).isoformat().replace("+00:00", "Z")
    return board_mod.GameCard(game_id=2, home="BUF", away="MIA", start_utc=start,
                              kickoff_window="", status="scheduled",
                              home_name="Buffalo Bills", away_name="Miami Dolphins")


def test_prop_book_skips_a_started_game_on_cursor_anyway(monkeypatch):
    calls = []

    class FakeProps:
        def __init__(self, *a, **k):
            pass

        def find_event_id(self, *a, **k):
            calls.append("find_event_id")
            return "evt1"

        def cached_event_props(self, *a, **k):
            calls.append("cached_event_props")
            return None

    monkeypatch.setattr(props_odds_mod, "PropsOddsClient", FakeProps)
    book, note = board_mod._prop_book(_started_card(), fetch_prices=True)
    assert book is None
    assert "started" in note
    assert calls == [], "a started game must never touch the props pipeline"


def test_prop_book_prices_an_upcoming_game(monkeypatch):
    calls = []

    class FakeProps:
        def __init__(self, *a, **k):
            pass

        def find_event_id(self, *a, **k):
            calls.append("find_event_id")
            return "evt1"

        def cached_event_props(self, *a, **k):
            calls.append("cached_event_props")
            return None

        def event_props(self, *a, **k):
            calls.append("event_props")
            return {"bookmakers": []}

    monkeypatch.setattr(props_odds_mod, "PropsOddsClient", FakeProps)
    book, note = board_mod._prop_book(_future_card(), fetch_prices=True)
    assert book is not None
    assert book.empty
    assert note == ""
    assert calls == ["find_event_id", "cached_event_props", "event_props"]

# ----------------------------------------------------- autopick gates

def _line(market="spread", raw=0.62, implied=0.52, edge=0.03):
    return SimpleNamespace(market=market, raw_model_prob=raw, implied_prob=implied,
                           edge=edge, confidence={"score": 8}, backing=None)


GATES = {"min_confidence": 6, "min_edge": 0.02, "min_backing_games": 0,
         "min_backing_rate": None, "skip_markets": {"all": ["h2h"], "nfl": ["total"]}}


def test_paused_markets_are_never_picked():
    assert not autopick.qualifies(_line("h2h"), GATES, "nba")[0]
    assert not autopick.qualifies(_line("total"), GATES, "nfl")[0]
    assert autopick.qualifies(_line("total"), GATES, "mlb")[0]
    assert autopick.qualifies(_line("spread"), GATES, "nfl")[0]


def test_a_model_far_off_the_market_is_skipped():
    # 20+ points off the price went 10-22: that gap is missing information.
    assert not autopick.qualifies(_line(raw=0.75, implied=0.52), GATES, "nba")[0]
    assert autopick.qualifies(_line(raw=0.66, implied=0.52), GATES, "nba")[0]
