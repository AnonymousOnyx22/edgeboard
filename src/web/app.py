"""Flask application for the local-only Edgeboard website."""
from __future__ import annotations

import datetime as dt
import hmac
import os
import secrets
from pathlib import Path

import yaml
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

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
        # A random local secret prevents another website from forging this
        # app's session cookie when EDGEBOARD_SECRET is not configured.
        SECRET_KEY=os.getenv("EDGEBOARD_SECRET") or secrets.token_hex(32),
        DB_PATH=str(db_path or db.DEFAULT_DB),
        # Waitress runs the app outside debug mode, which leaves Jinja caching
        # compiled templates and the browser caching CSS. Edits then appear to
        # do nothing until the whole server is restarted, so both are off.
        TEMPLATES_AUTO_RELOAD=True,
        SEND_FILE_MAX_AGE_DEFAULT=0,
    )
    app.jinja_env.auto_reload = True
    db.init_db(app.config["DB_PATH"])

    @app.before_request
    def verify_csrf():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("_csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            abort(400, description="The form expired. Reload the page and try again.")
        return None

    @app.context_processor
    def shared_context():
        from ..data.odds_client import OddsClient
        credits = OddsClient.credits()
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return {
            "today": dt.date.today().isoformat(),
            "sports": [sport_registry.get(code) for code in sport_registry.enabled_codes()],
            "credits": credits,
            # Club crests and player headshots, resolved in the templates.
            "team_logo": sport_registry.team_logo,
            "player_headshot": sport_registry.player_headshot,
            "csrf_token": token,
        }

    @app.get("/")
    def home():
        path = app.config["DB_PATH"]
        summary = record_summary(path)["overall"]
        open_picks = _scalar(path, """SELECT COUNT(*) FROM picks WHERE
            status IN ('approved','edited','content_generated','scheduled')""")
        model_picks = _scalar(path, "SELECT COUNT(*) FROM picks WHERE source='model'")
        recent = _tracked_picks(path, limit=5)
        performance = _performance(path)
        return render_template("home.html", active="home", summary=summary,
                               model_picks=model_picks, open_picks=open_picks,
                               recent=recent, performance=performance)

    @app.get("/games")
    def games():
        path = app.config["DB_PATH"]
        day = _date_arg(request.args.get("date"))
        sport = _sport_arg(request.args.get("sport"))
        days = max(1, min(21, request.args.get("days", 7, type=int)))
        # Finished and in-progress games are dropped. Their prices are live and
        # there is nothing left to bet, so the page only looks forward.
        cards = board.upcoming_board(sport, days=days, start=day, db_path=path)
        teams = board.next_game_by_team(sport, days=max(days, 14), start=day,
                                        db_path=path)
        tracked = _tracked_picks(path, None, sport=sport, open_only=True)
        return render_template("games.html", active="games", day=day, days=days,
                               sport=sport_registry.get(sport), cards=cards,
                               teams=teams, tracked=tracked)

    @app.post("/sync")
    def sync():
        from ..picker import autopick
        day = _date_arg(request.form.get("date"))
        sport = _sport_arg(request.form.get("sport"))
        days = max(1, min(21, request.form.get("days", 7, type=int)))
        try:
            picker = Picker(sport, db_path=app.config["DB_PATH"])
            games_synced = 0
            for offset in range(days):
                games_synced += picker.sync_games(day + dt.timedelta(days=offset))
            picker.sync_odds(day=day)
            result = autopick.run(sport, days=days, start=day,
                                  db_path=app.config["DB_PATH"])
            flash(f"Refreshed {games_synced} upcoming games. "
                  f"Added {result['added']} qualifying model pick(s).", "success")
        except Exception as exc:
            flash(f"Refresh failed: {exc}", "error")
        return redirect(url_for("games", date=day.isoformat(), sport=sport, days=days))

    @app.get("/model-picks")
    def model_picks():
        path = app.config["DB_PATH"]
        day = _date_arg(request.args.get("date"))
        sport = _sport_arg(request.args.get("sport"))
        days = max(1, min(14, request.args.get("days", 7, type=int)))
        legs = max(2, min(6, request.args.get("legs", 3, type=int)))
        rank = request.args.get("rank", "probability")
        if rank not in {"probability", "value"}:
            rank = "probability"
        min_prob = max(0.01, min(0.99, request.args.get("min_prob", 0.55, type=float)))
        cards = board.upcoming_board(sport, days=days, start=day, db_path=path)
        pool = board.parlay_pool(cards, min_probability=min_prob)
        parlays = optimize(pool, min_legs=legs, max_legs=legs,
                           rank_by=rank, limit=8) if len(pool) >= legs else []
        summary = _model_summary(path)
        # Everything the model has committed by itself, newest first, so the
        # whole record is visible in one place rather than a day at a time.
        auto_picks = _tracked_picks(path, limit=100)
        return render_template("model_picks.html", active="model", day=day, days=days,
                               sport=sport_registry.get(sport), legs=legs,
                               rank=rank, min_prob=min_prob, pool=pool,
                               parlays=parlays, summary=summary,
                               auto_picks=auto_picks,
                               auto_rules=_autopick_rules(),
                               watched=_watched_parlays(path, sport))

    @app.post("/model-picks/track")
    def track_model_parlay():
        """Rebuild a ranked result server-side and persist the selected slip."""
        path = app.config["DB_PATH"]
        day = _date_arg(request.form.get("date"))
        sport = _sport_arg(request.form.get("sport"))
        days = max(1, min(14, request.form.get("days", 7, type=int)))
        legs = max(2, min(6, request.form.get("legs", 3, type=int)))
        rank = request.form.get("rank", "probability")
        if rank not in {"probability", "value"}:
            rank = "probability"
        min_prob = max(0.01, min(0.99,
                       request.form.get("min_prob", 0.55, type=float)))
        index = max(0, request.form.get("rank_index", 0, type=int))
        try:
            cards = board.upcoming_board(sport, days=days, start=day, db_path=path)
            pool = board.parlay_pool(cards, min_probability=min_prob)
            parlays = optimize(pool, min_legs=legs, max_legs=legs,
                               rank_by=rank, limit=8) if len(pool) >= legs else []
            if index >= len(parlays):
                raise ValueError("That ranked parlay is no longer available. Rebuild the list.")
            saved = actions.save_model_parlay(parlays[index], sport, path)
            message = (f"Tracking parlay #{saved['pick_id']} at {saved['name']}."
                       if saved["created"] else
                       f"Parlay #{saved['pick_id']} is already being tracked.")
            flash(message, "success" if saved["created"] else "info")
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("model_picks", date=day.isoformat(), sport=sport,
                                days=days, legs=legs, rank=rank, min_prob=min_prob))

    @app.post("/autopick")
    def run_autopick():
        """Commit every upcoming bet the model is confident enough in."""
        from ..picker import autopick
        path = app.config["DB_PATH"]
        days = max(1, min(14, request.form.get("days", 3, type=int)))
        added = 0
        for result in autopick.run_all(days=days, db_path=path):
            added += result["added"]
        if added:
            flash(f"Auto-pick added {added} pick(s) to the ledger.", "success")
        else:
            flash("No upcoming bet cleared the auto-pick rules.", "info")
        return redirect(url_for("model_picks"))

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
                enabled = [code for code in sport_registry.REGISTRY
                           if request.form.get(f"sport_{code}") == "on"]
                if not enabled:
                    raise ValueError("Keep at least one league enabled.")
                auto_enabled = request.form.get("auto_enabled") == "on"
                auto_confidence = int(request.form["auto_min_confidence"])
                auto_edge = float(request.form["auto_min_edge"])
                auto_games = int(request.form["auto_min_backing_games"])
                auto_max = int(request.form["auto_max_per_day"])
                raw_rate = request.form.get("auto_min_backing_rate", "").strip()
                auto_rate = float(raw_rate) if raw_rate else None
                if not 1 <= auto_confidence <= 10:
                    raise ValueError("Auto-pick confidence must be from 1 to 10.")
                if not 0 <= auto_edge <= 1:
                    raise ValueError("Auto-pick edge must be from 0 to 1.")
                if not 0 <= auto_games <= 20 or not 1 <= auto_max <= 20:
                    raise ValueError("Use 0-20 backing games and 1-20 picks per day.")
                if auto_rate is not None and not 0 <= auto_rate <= 1:
                    raise ValueError("Historical hit rate must be from 0 to 1.")
                config["edge"]["target"] = target
                config["edge"]["max_edge_display"] = max_edge
                config["auto_pick"] = {
                    **config.get("auto_pick", {}),
                    "enabled": auto_enabled,
                    "min_confidence": auto_confidence,
                    "min_edge": auto_edge,
                    "min_backing_games": auto_games,
                    "min_backing_rate": auto_rate,
                    "max_per_day": auto_max,
                }
                for code in sport_registry.REGISTRY:
                    config.setdefault("sports", {}).setdefault(code, {})["enabled"] = (
                        request.form.get(f"sport_{code}") == "on"
                    )
                settings_path = db.ROOT / "config" / "settings.yaml"
                temp_path = settings_path.with_suffix(".yaml.tmp")
                temp_path.write_text(yaml.safe_dump(config, sort_keys=False,
                                     allow_unicode=True), encoding="utf-8")
                temp_path.replace(settings_path)
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
                   limit: int = 30, open_only: bool = False) -> list[dict]:
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
    if open_only:
        sql += """ AND p.status IN ('approved','edited','scheduled')
                   AND g.status='scheduled'
                   AND datetime(g.start_utc) > datetime('now')"""
    sql += " ORDER BY COALESCE(r.graded_utc,p.created_utc) DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in db.query(path, sql, tuple(params))]


def _autopick_rules() -> dict:
    from ..picker import autopick
    return autopick.settings()


def _watched_parlays(path, sport: str) -> list[dict]:
    """Presentation-ready price histories for the Model Picks watchlist."""
    from ..picker.edge import american_from_prob
    watched = actions.tracked_parlays(sport, path)
    for item in watched:
        snapshots = item["snapshots"]
        values = [float(row["decimal_odds"]) for row in snapshots]
        if not values:
            item.update(points="", opening_odds=None, current_odds=None, move=0.0)
            continue
        opening, current = values[0], values[-1]
        low, high = min(values), max(values)
        spread = max(high - low, 0.1)
        points = []
        for index, value in enumerate(values):
            x = 4 + 352 * index / max(len(values) - 1, 1)
            y = 4 + 64 * (high - value + spread * 0.08) / (spread * 1.16)
            points.append(f"{x:.1f},{y:.1f}")
        if len(points) == 1:
            points.append(f"356.0,{points[0].split(',')[1]}")
        item["points"] = " ".join(points)
        item["opening_odds"] = round(american_from_prob(1.0 / opening))
        item["current_odds"] = round(american_from_prob(1.0 / current))
        item["move"] = current / opening - 1.0
    return watched


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


if __name__ == "__main__":
    from waitress import serve
    serve(create_app(), host="127.0.0.1", port=8502, threads=6)
