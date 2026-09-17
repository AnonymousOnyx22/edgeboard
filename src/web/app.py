"""Flask application for the local-only Edgeboard website."""
from __future__ import annotations

import datetime as dt
import math
import os
from pathlib import Path

import yaml
from flask import Flask, flash, redirect, render_template, request, url_for

from ..data import sports as sport_registry
from ..db import database as db
from ..picker import actions, board
from ..picker.backtest import record_summary
from ..picker.engine import Picker
from ..picker.grader import grade_all
from ..picker.parlay_builder import optimize


def create_app(db_path=None) -> Flask:
    root = Path(__file__).resolve().parent
    app = Flask(__name__, template_folder=str(root / "templates"),
                static_folder=str(root / "static"))
    app.config.update(
        SECRET_KEY=os.getenv("EDGEBOARD_SECRET", "edgeboard-local-only"),
        DB_PATH=str(db_path or db.DEFAULT_DB),
    )
    db.init_db(app.config["DB_PATH"])

    @app.context_processor
    def shared_context():
        from ..data.odds_client import OddsClient
        credits = OddsClient.credits()
        return {
            "today": dt.date.today().isoformat(),
            "sports": [sport_registry.get(code) for code in sport_registry.enabled_codes()],
            "credits": credits,
        }

    @app.get("/")
    def home():
        path = app.config["DB_PATH"]
        summary = record_summary(path)["overall"]
        pending = _scalar(path, "SELECT COUNT(*) FROM picks WHERE status='suggested'")
        open_picks = _scalar(path, """SELECT COUNT(*) FROM picks WHERE
            status IN ('suggested','approved','edited','content_generated','scheduled')""")
        recent = _tracked_picks(path, limit=5)
        performance = _performance(path)
        return render_template("home.html", active="home", summary=summary,
                               pending=pending, open_picks=open_picks,
                               recent=recent, performance=performance)

    @app.get("/slate")
    def slate():
        path = app.config["DB_PATH"]
        day = _date_arg(request.args.get("date"))
        sport = _sport_arg(request.args.get("sport"))
        cards = board.slate_board(day, sport, db_path=path)
        tracked = _tracked_picks(path, day.isoformat(), sport=sport)
        return render_template("slate.html", active="slate", day=day,
                               sport=sport_registry.get(sport), cards=cards,
                               tracked=tracked)

    @app.post("/sync")
    def sync():
        day = _date_arg(request.form.get("date"))
        sport = _sport_arg(request.form.get("sport"))
        try:
            suggestions = Picker(sport, db_path=app.config["DB_PATH"]).suggest(day, persist=True)
            flash(f"Slate refreshed. {len(suggestions)} qualifying model pick(s) found.", "success")
        except Exception as exc:
            flash(f"Refresh failed: {exc}", "error")
        return redirect(url_for("slate", date=day.isoformat(), sport=sport))

    @app.get("/model-picks")
    def model_picks():
        path = app.config["DB_PATH"]
        day = _date_arg(request.args.get("date"))
        sport = _sport_arg(request.args.get("sport"))
        legs = max(2, min(6, request.args.get("legs", 3, type=int)))
        rank = request.args.get("rank", "probability")
        if rank not in {"probability", "value"}:
            rank = "probability"
        min_prob = max(0.01, min(0.99, request.args.get("min_prob", 0.55, type=float)))
        cards = board.slate_board(day, sport, db_path=path)
        pool = board.parlay_pool(cards, min_probability=min_prob)
        parlays = optimize(pool, min_legs=legs, max_legs=legs,
                           rank_by=rank, limit=8) if len(pool) >= legs else []
        summary = _model_summary(path)
        return render_template("model_picks.html", active="model", day=day,
                               sport=sport_registry.get(sport), legs=legs,
                               rank=rank, min_prob=min_prob, pool=pool,
                               parlays=parlays, summary=summary)

    @app.post("/picks/<int:pick_id>/<action>")
    def pick_action(pick_id: int, action: str):
        path = app.config["DB_PATH"]
        try:
            if action == "approve":
                actions.approve_pick(pick_id, path)
                flash(f"Pick #{pick_id} added to your card.", "success")
            elif action == "reject":
                actions.reject_pick(pick_id, path)
                flash(f"Pick #{pick_id} passed.", "info")
            else:
                flash("Unknown pick action.", "error")
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(request.referrer or url_for("slate"))

    @app.get("/performance")
    def performance():
        path = app.config["DB_PATH"]
        return render_template("performance.html", active="performance",
                               summary=record_summary(path),
                               performance=_performance(path),
                               picks=_tracked_picks(path, limit=100))

    @app.post("/grade")
    def grade():
        graded = grade_all(app.config["DB_PATH"], request.form.get("date") or None)
        flash(f"Graded {len(graded)} completed pick(s).", "success")
        return redirect(url_for("performance"))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        config = db.get_config()
        if request.method == "POST":
            try:
                target = float(request.form["edge_target"])
                max_edge = float(request.form["max_edge_display"])
                if not 0 < target <= max_edge <= 1:
                    raise ValueError("Use an edge threshold above 0 and below the full-scale edge.")
                config["edge"]["target"] = target
                config["edge"]["max_edge_display"] = max_edge
                for code in sport_registry.REGISTRY:
                    config.setdefault("sports", {}).setdefault(code, {})["enabled"] = (
                        request.form.get(f"sport_{code}") == "on"
                    )
                (db.ROOT / "config" / "settings.yaml").write_text(
                    yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
                )
                flash("Settings saved.", "success")
                return redirect(url_for("settings"))
            except (KeyError, ValueError) as exc:
                flash(str(exc), "error")
        return render_template("settings.html", active="settings", config=config,
                               registry=sport_registry.REGISTRY)

    return app


def _date_arg(value: str | None) -> dt.date:
    try:
        return dt.date.fromisoformat(value) if value else dt.date.today()
    except ValueError:
        return dt.date.today()


def _sport_arg(value: str | None) -> str:
    enabled = sport_registry.enabled_codes()
    return value if value in enabled else enabled[0]


def _scalar(path, sql: str, params: tuple = ()) -> int:
    row = db.query(path, sql, params)[0]
    return int(row[0])


def _tracked_picks(path, day: str | None = None, sport: str | None = None,
                   limit: int = 30) -> list[dict]:
    sql = """SELECT p.id, p.status, p.source, p.nature, p.edge,
                    COALESCE(p.confidence_final,p.confidence_model) AS confidence,
                    COALESCE(p.units_final,p.units_model) AS units,
                    l.market,l.team,l.line,l.odds_american,l.model_prob,l.implied_prob,
                    g.game_date,g.start_utc,g.status AS game_status,
                    at.abbrev AS away,ht.abbrev AS home,
                    r.result,r.units_won,r.graded_utc
             FROM picks p JOIN pick_legs l ON l.pick_id=p.id AND l.ord=0
             JOIN games g ON g.id=l.game_id
             JOIN teams at ON at.id=g.away_team JOIN teams ht ON ht.id=g.home_team
             LEFT JOIN results r ON r.pick_id=p.id WHERE 1=1"""
    params: list = []
    if day:
        sql += " AND COALESCE(g.game_date,date(g.start_utc))=date(?)"
        params.append(day)
    if sport:
        sql += " AND p.sport_id=(SELECT id FROM sports WHERE code=?)"
        params.append(sport)
    sql += " ORDER BY COALESCE(r.graded_utc,p.created_utc) DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in db.query(path, sql, tuple(params))]


def _model_summary(path) -> dict:
    rows = db.query(path, """SELECT r.result,r.units_won,
        COALESCE(p.units_final,p.units_model,1) AS risk
        FROM results r JOIN picks p ON p.id=r.pick_id WHERE p.source='model'""")
    wins = sum(row["result"] == "W" for row in rows)
    losses = sum(row["result"] == "L" for row in rows)
    units = sum(float(row["units_won"]) for row in rows)
    risk = sum(float(row["risk"]) for row in rows)
    return {"wins": wins, "losses": losses, "decisions": wins + losses,
            "hit_rate": wins / (wins + losses) if wins + losses else 0,
            "units": units, "roi": units / risk if risk else 0}


def _performance(path) -> dict:
    rows = [dict(row) for row in db.query(path, """
        SELECT r.graded_utc,r.units_won,r.result,p.id
        FROM results r JOIN picks p ON p.id=r.pick_id
        ORDER BY r.graded_utc,p.id""")]
    values = [100.0]
    labels = ["Start"]
    for row in rows:
        values.append(values[-1] + float(row["units_won"]))
        labels.append(f"#{row['id']} {row['result']}")
    width, height, pad = 900, 260, 18
    low, high = min(values), max(values)
    spread = max(high - low, 2.0)
    points = []
    for index, value in enumerate(values):
        x = pad + (width - 2 * pad) * index / max(len(values) - 1, 1)
        y = pad + (height - 2 * pad) * (high - value + spread * 0.08) / (spread * 1.16)
        points.append(f"{x:.1f},{y:.1f}")
    return {"rows": rows, "values": values, "labels": labels,
            "points": " ".join(points), "latest": values[-1],
            "positive": values[-1] >= 100, "width": width, "height": height}


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8502, debug=False)
