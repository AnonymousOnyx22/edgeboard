"""Backtesting: hit rate, ROI, calibration by confidence bucket.

Two modes:
  1. record_summary()      - from the real graded DB (what you actually bet).
  2. simulate_season()      - closed-loop replay of the model strategy on a
                              past NFL season using historical closing moneylines,
                              reporting accuracy by model-confidence bucket so you
                              can verify 8/10 picks actually win more than 5/10.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ..db import database as db
from ..data import models, stats_nfl
from ..picker import confidence as conf


def record_summary(db_path=db.DEFAULT_DB) -> dict:
    """Overall graded record: hit rate, units, ROI, grouped by category."""
    rows = db.query(db_path, """
        SELECT p.sport_id, p.nature, p.source, p.confidence_final,
               COALESCE(p.units_final, p.units_model, 1.0) AS units_staked,
               r.result, r.units_won
        FROM results r JOIN picks p ON p.id = r.pick_id
    """)
    sport_names = {s["id"]: s["code"] for s in db.query(db_path, "SELECT id, code FROM sports")}

    def summarize(frame: pd.DataFrame) -> dict:
        if frame.empty:
            return {"wins": 0, "losses": 0, "pushes": 0, "units": 0.0, "roi": 0.0, "n": 0}
        w = len(frame[frame["result"] == "W"])
        l = len(frame[frame["result"] == "L"])
        p = len(frame[frame["result"] == "P"])
        bets = w + l
        units = float(frame["units_won"].sum())
        stake = float(frame["units_staked"].sum())
        rop = units / stake if stake else 0.0
        return {"wins": w, "losses": l, "pushes": p, "n": bets,
                "hit_rate": round(w / bets, 4) if bets else 0.0,
                "units": round(units, 2), "roi": round(rop, 4)}

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return {"overall": summarize(df), "by_sport": {}, "by_source": {},
                "by_nature": {}, "by_confidence": {}}
    df["sport"] = df["sport_id"].map(sport_names)
    return {
        "overall": summarize(df),
        "by_sport": {k: summarize(g) for k, g in df.groupby("sport")},
        "by_source": {k: summarize(g) for k, g in df.groupby("source")},
        "by_nature": {k: summarize(g) for k, g in df.groupby("nature")},
        "by_confidence": _calibration_by_confidence(db_path),
    }


def _calibration_by_confidence(db_path=db.DEFAULT_DB) -> dict:
    rows = db.query(db_path, """
        SELECT p.confidence_final as conf, r.result
        FROM results r JOIN picks p ON p.id=r.pick_id
        WHERE p.confidence_final IS NOT NULL
    """)
    buckets: dict[str, dict] = {}
    for r in rows:
        b = int(round(float(r["conf"])))
        buckets.setdefault(b, {"wins": 0, "losses": 0, "pushes": 0})
        key = {"W": "wins", "L": "losses", "P": "pushes"}.get(r["result"], "pushes")
        buckets[b][key] += 1
    out = {}
    for b in sorted(buckets):
        d = buckets[b]
        plays = d["wins"] + d["losses"]
        out[b] = {
            "n": plays,
            "hit_rate": round(d["wins"] / plays, 4) if plays else 0.0,
            "wins": d["wins"], "losses": d["losses"], "pushes": d["pushes"],
        }
    return out


# ---------------------------------------------------------------------------
# Historical closed-loop simulation
# ---------------------------------------------------------------------------

def simulate_season(season: int | None = None, target_edge: float = 0.03,
                    min_prob: float = 0.50) -> dict:
    """Replay the picker on one past season. Only uses data known before each
    kickoff: Elo trained on everything up to that point."""
    from ..data import models as M
    season = season or dt.date.today().year - 1
    df = stats_nfl.schedule_dataframe(seasons=[season])
    if df.empty:
        return {"error": "no schedule data"}
    req = ["home_team", "away_team", "home_score", "away_score",
           "home_moneyline", "away_moneyline", "gameday"]
    if not all(c in df.columns for c in req):
        return {"error": "schedule missing closing line columns"}

    df = df.dropna(subset=req).copy()
    df["gameday"] = pd.to_datetime(df["gameday"], errors="coerce")

    # Train Elo on all seasons strictly before this one + earlier weeks of this season
    prior = stats_nfl.schedule_dataframe(seasons=list(range(season - 2, season)))
    history = pd.concat([prior, df]).dropna(subset=["home_score", "away_score"]).reset_index(drop=True)

    picks = []
    for _, row in df.iterrows():
        until = row["gameday"] - dt.timedelta(days=1)
        elo = models.train_elo(history, until_date=until.date()) if hasattr(until, "date") else models.train_elo(history)
        home, away = row["home_team"], row["away_team"]
        pred = elo.predict(home, away)
        p_home = pred["home_win_prob"]
        hm_price, am_price = float(row["home_moneyline"]), float(row["away_moneyline"])
        p_away = 1 - p_home

        from ..picker.edge import american_to_prob, remove_vig
        ip_away, ip_home = remove_vig(american_to_prob(am_price), american_to_prob(hm_price))
        incoming = ("home", p_home, ip_home, hm_price) if p_home >= p_away else ("away", p_away, ip_away, am_price)

        side, p_model, ip_market, price = incoming
        edge = p_model - ip_market
        if edge < target_edge or p_model <= min_prob:
            continue
        quality = {"games_of_history": min(elo.games(home), elo.games(away)),
                   "missing_starter_confirmation": False,
                   "missing_injury_data": True if season == dt.date.today().year else False,
                   "extreme_weather": False}
        cfg = {"edge": {"target": target_edge, "max_edge_display": 0.10}, "confidence": {
            "weights": {"edge": 0.45, "probability": 0.35, "quality": 0.20},
            "quality_penalties": _default_penalties()}}
        cbreak = conf.compute_confidence(edge, p_model, quality,
                                         cfg["confidence"], cfg["edge"]["max_edge_display"])
        m = row["home_score"] - row["away_score"]
        won = (m > 0) if side == "home" else (m < 0)
        picks.append({
            "week": row.get("week"), "date": row["gameday"],
            "bet": f"{home} vs {away}", "side": side, "price": int(price),
            "edge": round(edge, 4), "conf": cbreak["score"],
            "result": "W" if won else "L",
            "units": conf.units_from_confidence(cbreak["score"], cfg),
        })

    return _summarize_simulation(picks)


def _default_penalties() -> dict:
    import yaml as _y  # noqa: F401
    from ..db.database import get_config
    return get_config()["confidence"]["quality_penalties"]


def _summarize_simulation(picks: list[dict]) -> dict:
    from .edge import payout_multiplier
    df = pd.DataFrame(picks)
    if df.empty:
        return {"n": 0, "hit_rate": 0.0, "roi": 0.0, "by_conf": {}, "picks": []}
    wins = df["result"] == "W"
    stake = df["units"].sum()
    win_units = df.loc[wins].apply(lambda r: r["units"] * payout_multiplier(r["price"]), axis=1).sum()
    loss_units = df.loc[~wins, "units"].sum()
    units = win_units - loss_units
    by_conf = {}
    for c in sorted(df["conf"].unique()):
        sub = df[df["conf"] == c]
        w = (sub["result"] == "W").sum()
        plays = len(sub)
        by_conf[int(c)] = {
            "n": plays,
            "hit_rate": round(w / plays, 4) if plays else 0.0,
            "units": round(sum(sub.apply(lambda r: r["units"] * payout_multiplier(r["price"]) if r["result"] == "W"
                                         else -r["units"], axis=1)), 2),
        }
    return {
        "n": len(df), "wins": int(wins.sum()), "losses": int((~wins).sum()),
        "hit_rate": round(wins.mean(), 4),
        "units": round(units, 2), "roi": round(units / stake, 4) if stake else 0.0,
        "avg_edge": round(df["edge"].mean(), 4),
        "by_conf": by_conf,
    }
