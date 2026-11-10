import datetime as dt

import requests

from src.data import espn_client
from src.db import database as db


SCOREBOARD = {"events": [{
    "id": "401", "date": "2099-11-02T00:00Z",
    "competitions": [{
        "id": "401",
        "status": {"type": {"state": "post", "completed": True, "detail": "Final"}},
        "competitors": [
            {"order": 0, "homeAway": "home", "score": "112",
             "team": {"id": "28", "abbreviation": "TOR", "displayName": "Toronto Raptors"}},
            {"order": 1, "homeAway": "away", "score": "117",
             "team": {"id": "12", "abbreviation": "LAC", "displayName": "LA Clippers"}},
        ],
    }],
}]}


def test_core_outage_falls_back_to_site_scoreboard(tmp_path, monkeypatch):
    path = tmp_path / "fallback.sqlite"
    db.init_db(path)

    def fake_get(self, url, cache_name=None):
        if url.startswith(espn_client.CORE_BASE):
            raise requests.HTTPError("503 Server Error: Backend fetch failed")
        assert url.startswith(espn_client.SITE_BASE)
        return SCOREBOARD

    monkeypatch.setattr(espn_client.EspnClient, "_get_json", fake_get)
    client = espn_client.EspnClient()

    assert client.upsert_games("nba", dt.date(2099, 11, 1), db_path=path) == 1
    game = dict(db.query(path, """
        SELECT g.status, g.away_score, g.home_score, a.abbrev AS away, h.abbrev AS home
        FROM games g JOIN teams a ON a.id=g.away_team JOIN teams h ON h.id=g.home_team
    """)[0])
    assert game == {"status": "final", "away_score": 117, "home_score": 112,
                    "away": "LAC", "home": "TOR"}
