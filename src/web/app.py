"""Flask application for the local-only Edgeboard website."""
from __future__ import annotations

import datetime as dt
import hmac
import os
import re
import secrets
from pathlib import Path

import yaml
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from ..data import sports as sport_registry
from ..db import database as db
from ..picker import actions, board, players
from ..picker.backtest import record_summary
from ..picker.engine import Picker
from ..picker.grader import grade_all
from ..picker.parlay_builder import optimize
from . import gating
from .billing import CSRF_EXEMPT

# Pages that change the model or spend Odds API credits. In service mode
# only the owner (EDGEBOARD_ADMIN_EMAILS) may reach them.
ADMIN_ENDPOINTS = {"settings", "sync", "run_autopick", "grade",
                   "refresh_players", "track_model_parlay"}


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
    if gating.service_on():
        # A public copy: cookies only over HTTPS and never readable by scripts.
        app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                          SESSION_COOKIE_SAMESITE="Lax")

    from .hitrates_bp import bp as hitrates_bp
    app.register_blueprint(hitrates_bp)

    @app.before_request
    def verify_csrf():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        if request.endpoint in CSRF_EXEMPT:
            return None  # Stripe signs its webhook instead
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("_csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            abort(400, description="The form expired. Reload the page and try again.")
        return None

    @app.before_request
    def owner_only():
        if request.endpoint not in ADMIN_ENDPOINTS or not gating.service_on():
            return None
        admins = {e.strip().lower() for e in
                  os.getenv("EDGEBOARD_ADMIN_EMAILS", "").split(",") if e.strip()}
        user = gating.current_user()
        email = (user["email"] if user else "").lower()
        if not email or email not in admins:
            abort(404)
        return None

    @app.context_processor
    def shared_context():
        from ..data.odds_client import OddsClient
        from ..picker import autopick as autopick_mod
        from ..picker import live as live_mod
        credits = OddsClient.credits()
        open_now = _crawl_picks(app.config["DB_PATH"])
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return {
            "today": dt.date.today().isoformat(),
            "sports": [sport_registry.get(code) for code in sport_registry.enabled_codes()],
            "credits": credits,
            # Club crests and player headshots, resolved in the templates.
            # Prop legs store their market as prop:<stat key>; templates use
            # these to tell a player bet from a game line.
            "bet_label": bet_label,
            "money": money,
            "bankroll": bankroll,
            "pick_state": pick_state,
            "unit_size": unit_size(),
            "live_status": live_mod.describe,
            "is_prop": autopick_mod.is_prop_market,
            "prop_stat": autopick_mod.stat_key_of,
            "stat_label": lambda key, code: sport_registry.get(code)
                .stat_labels.get(key, key or ""),
            "team_logo": sport_registry.team_logo,
            "player_headshot": sport_registry.player_headshot,
            "csrf_token": token,
            "bet_text": bet_text,
            # The crawl along the top of every page: what is in play right now.
            "crawl": _free_slice(open_now)[:24],
            "open_count": len(open_now),
        }

    from .accounts import install as install_accounts
    install_accounts(app)

    # Display filters. Visible numbers use a true minus sign, and dates read
    # as words, so no page has to print a bare hyphen.
    app.jinja_env.filters["odds"] = odds_text
    app.jinja_env.filters["signed"] = signed_text
    app.jinja_env.filters["minus"] = minus_text
    app.jinja_env.filters["nice_date"] = nice_date
    app.jinja_env.filters["kickoff"] = kickoff_text
    app.jinja_env.filters["dashless"] = dashless

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
                               recent=recent, performance=performance,
                               any_live=_kick_live(path))

    @app.get("/games")
    def games():
        # The line board was folded into Today's games and the game pages.
        # Old links and bookmarks still land somewhere useful.
        return redirect(url_for("tickets", sport=_sport_arg(request.args.get("sport"))))

    @app.get("/tickets")
    def tickets():
        """Today's fixtures as cards: who, when, where, and what to bet.

        The board and the player pages are both tables. This is the same slate
        read the way a ticket reads - one card per game - for picking a game
        first and a bet second.
        """
        path = app.config["DB_PATH"]
        day = _date_arg(request.args.get("date"))
        sport = _sport_arg(request.args.get("sport"))
        days = max(1, min(7, request.args.get("days", 1, type=int)))
        cutoff = day + dt.timedelta(days=days)
        # Every game on the day, started ones included: a game that has tipped
        # off is still tonight's game, it is just no longer bettable.
        cards = [card for card in
                 board.upcoming_board(sport, days=days, start=day, db_path=path,
                                      include_started=True)
                 if not card.game_date
                 or dt.date.fromisoformat(card.game_date) < cutoff]
        return render_template("tickets.html", active="tickets", day=day,
                               days=days, sport=sport_registry.get(sport),
                               cards=cards)

    @app.get("/game/<sport>/<int:game_id>")
    def game_detail(sport, game_id):
        """One fixture, opened from its ticket: the bets the model likes.

        Game lines come from the board, which is already built. Player props
        are only shown when they are already cached, because pricing them
        costs Odds API credits and opening a ticket should never spend any.
        """
        if sport not in sport_registry.REGISTRY:
            abort(404)
        path = app.config["DB_PATH"]
        card = board.card_for_game(sport, game_id, db_path=path)
        if card is None:
            abort(404)

        props, props_note = [], ""
        try:
            deep = board.deep_matchup(card, fetch_prices=False, max_players=8)
            props = [c for c in deep.props if c.trustworthy][:12]
            props_note = deep.props_note
        except Exception as exc:
            props_note = f"Player props unavailable: {exc}"

        return render_template("game.html", active="tickets", card=card,
                               sport=sport_registry.get(sport), props=props,
                               props_note=props_note)

    @app.get("/players")
    def players_page():
        path = app.config["DB_PATH"]
        sport = _sport_arg(request.args.get("sport"))
        day = _date_arg(request.args.get("date"))
        days = max(1, min(14, request.args.get("days", 7, type=int)))
        options = players.team_options(sport, path)
        option_by_code = {item["abbrev"].upper(): item for item in options}
        raw_teams = request.args.get("teams") or request.args.get("team") or ""
        selected_teams = []
        for value in raw_teams.split(","):
            code = value.strip().upper()
            if code in option_by_code and code not in selected_teams:
                selected_teams.append(code)

        # Keep one league-wide board behind the unfiltered view. A selected
        # team that was outside its game window gets a focused one-game build.
        entry = players.CACHE.get(sport, days, day, None)
        if not entry or not players.CACHE.is_fresh(entry):
            entry = players.CACHE.start(sport, days, day, path, team=None,
                                        max_games=players.DEFAULT_MAX_GAMES)
        board_data = entry.get("board") if entry else None
        player_rows = board_data.players if board_data is not None else None

        team_groups = []
        any_building = bool(entry and entry.get("status") == "building")
        for code in selected_teams:
            rows = board_data.by_team(code) if board_data is not None else []
            team_entry = entry if rows else players.CACHE.get(sport, days, day, code)
            if not rows and (not team_entry or not players.CACHE.is_fresh(team_entry)):
                # A filter only ever shows this club's next fixture, so the
                # focused build reads one game rather than the six the
                # league-wide board takes. Each game is both rosters' game
                # logs, so this is the difference between a filter that
                # answers and one that sits there.
                team_entry = players.CACHE.start(
                    sport, days, day, path, team=code, max_games=1)
            team_board = team_entry.get("board") if team_entry else None
            if not rows and team_board is not None:
                rows = team_board.by_team(code)
            status = team_entry.get("status") if team_entry else "building"
            any_building = any_building or status == "building"
            team_groups.append({
                "team": option_by_code[code], "players": rows,
                "status": status,
                "message": team_entry.get("message", "") if team_entry else "",
            })

        team_filters = []
        for item in options:
            code = item["abbrev"].upper()
            next_teams = ([value for value in selected_teams if value != code]
                          if code in selected_teams else selected_teams + [code])
            team_filters.append({
                **item, "selected": code in selected_teams,
                "url": url_for("players_page", sport=sport, days=days,
                               date=day.isoformat(), teams=",".join(next_teams)),
            })
        return render_template("players.html", active="players",
                               sport=sport_registry.get(sport), day=day, days=days,
                               entry=entry, board=board_data,
                               player_rows=player_rows,
                               options=options, team_filters=team_filters,
                               selected_teams=selected_teams,
                               team_groups=team_groups,
                               any_building=any_building)

    @app.post("/players/refresh")
    def refresh_players():
        path = app.config["DB_PATH"]
        sport = _sport_arg(request.form.get("sport"))
        day = _date_arg(request.form.get("date"))
        days = max(1, min(14, request.form.get("days", 7, type=int)))
        selected_teams = [value.strip().upper()
                          for value in (request.form.get("teams") or "").split(",")
                          if value.strip()]
        fetch_prices = request.form.get("fetch_prices") == "on"
        for team in selected_teams or [None]:
            players.CACHE.start(sport, days, day, path, team=team,
                                fetch_prices=fetch_prices, force=True)
        flash("Rebuilding the player board. A cold cache can take a moment.", "info")
        return redirect(url_for("players_page", sport=sport, days=days,
                                date=day.isoformat(),
                                teams=",".join(selected_teams)))

    @app.get("/player/<sport>/<athlete_id>")
    def player_detail(sport, athlete_id):
        if sport not in sport_registry.REGISTRY:
            abort(404)
        path = app.config["DB_PATH"]
        view = players.CACHE.find_player(sport, athlete_id)
        if view is None:
            team = (request.args.get("team") or "").strip()
            if not team:
                abort(404, description="Open this player from the players board.")
            try:
                view = players.build_player_view(sport, athlete_id, team, path)
            except Exception as exc:
                flash(f"Could not load that player: {exc}", "error")
                return redirect(url_for("players_page", sport=sport))
        if view is None:
            abort(404, description="Player not found.")
        return render_template("player.html", active="players",
                               sport=sport_registry.get(sport), view=view)

    @app.get("/teams")
    def teams_index():
        path = app.config["DB_PATH"]
        sport = _sport_arg(request.args.get("sport"))
        rows = board.next_game_by_team(sport, days=14, db_path=path)
        return render_template("teams.html", active="teams",
                               sport=sport_registry.get(sport), teams=rows)

    @app.get("/teams/<sport>/<abbrev>")
    def team_detail(sport, abbrev):
        if sport not in sport_registry.REGISTRY:
            abort(404)
        path = app.config["DB_PATH"]
        cached = players.CACHE.find_team(sport, abbrev)
        try:
            view = players.build_team_view(
                sport, abbrev, days=7, db_path=path,
                fetch_prices=(request.args.get("prices") == "1"),
                players=cached)
        except Exception as exc:
            flash(f"Could not load that team: {exc}", "error")
            return redirect(url_for("teams_index", sport=sport))
        if view is None:
            abort(404, description="Unknown team.")
        return render_template("team.html", active="teams",
                               sport=sport_registry.get(sport), view=view)

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
        # The headline record is every league at once, which is the number
        # worth watching. Only the ledger below is filtered, so the two are
        # labelled rather than left to look like they disagree.
        summary = _model_summary(path)
        auto_picks = _tracked_picks(path, limit=200, sport=sport)
        # What is still running: committed, not yet settled. These are the
        # picks with something left to happen, so they lead the page.
        open_picks = [pick for pick in _tracked_picks(path, limit=200)
                      if not pick["result"]]
        # A free plan sees every graded result but only a few open picks.
        shown = _free_slice(open_picks)
        locked_picks = len(open_picks) - len(shown)
        open_picks = shown
        visible = {pick["id"] for pick in shown}
        auto_picks = [pick for pick in auto_picks
                      if pick["result"] or pick["id"] in visible
                      or gating.free_pick_allowance() is None]
        return render_template("model_picks.html", active="model", day=day,
                               days=days, sport=sport_registry.get(sport),
                               summary=summary, open_picks=open_picks,
                               locked_picks=locked_picks,
                               auto_picks=auto_picks, auto_rules=_autopick_rules(),
                               performance=_performance(path),
                               any_live=_kick_live(path))

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
                               picks=_tracked_picks(path, limit=100),
                               any_live=_kick_live(path))

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
                per_unit = float(request.form.get("dollars_per_unit") or 0)
                if not 0 <= per_unit <= 100000:
                    raise ValueError("Use a unit size from 0 to 100000.")
                config["edge"]["target"] = target
                config["edge"]["max_edge_display"] = max_edge
                config.setdefault("units", {})["dollars_per_unit"] = per_unit
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
                    l.athlete_id,l.player,l.reason,
                    g.game_date,g.start_utc,g.status AS game_status,
                    g.status_detail,g.away_score,g.home_score,
                    at.abbrev AS away,ht.abbrev AS home,sp.code AS sport_code,
                    r.result,r.units_won,r.graded_utc
             FROM picks p JOIN pick_legs l ON l.pick_id=p.id AND l.ord=0
             JOIN sports sp ON sp.id=p.sport_id
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


def unit_size(config: dict | None = None) -> float:
    """What one unit is worth in dollars. 0 turns the money display off."""
    cfg = config if config is not None else db.get_config()
    try:
        return max(0.0, float((cfg.get("units") or {}).get("dollars_per_unit", 0)))
    except (TypeError, ValueError):
        return 0.0


def starting_units(config: dict | None = None) -> float:
    """The bankroll the record is measured from, in units."""
    cfg = config if config is not None else db.get_config()
    try:
        return float((cfg.get("units") or {}).get("starting_units", 100))
    except (TypeError, ValueError):
        return 100.0


def bankroll(units_won, config: dict | None = None) -> dict | None:
    """Where the bankroll started and where it stands, or None if unpriced.

    A net figure on its own says nothing about scale: +$156 is a different
    story on a $1,000 bankroll than on $100,000. This is the same record
    stated as a starting and a current balance.
    """
    cfg = config if config is not None else db.get_config()
    size = unit_size(cfg)
    if not size:
        return None
    try:
        won = float(units_won or 0)
    except (TypeError, ValueError):
        return None
    start = starting_units(cfg) * size
    now = start + won * size
    return {
        "start": f"${start:,.2f}",
        "now": f"${now:,.2f}",
        "up": now >= start,
    }


def money(units, config: dict | None = None) -> str:
    """`units` rendered as money, or "" when no unit size is configured.

    Signed the same way the unit figure is, because a record reads as a
    result rather than a balance: "+3.4u" belongs next to "+$34".
    """
    size = unit_size(config)
    if not size or units is None:
        return ""
    try:
        amount = float(units) * size
    except (TypeError, ValueError):
        return ""
    sign = "-" if amount < 0 else "+"
    return f"{sign}${abs(amount):,.2f}"


def pick_state(pick) -> dict:
    """Where a pick stands, as {label, tone}.

    "approved" and "graded" are workflow states - they describe what the
    ledger has done with a pick, not what is happening to the bet. What a
    reader wants is whether it is still to come, running now, or done:

      Open      committed, the game has not started
      Live      the game is under way
      Finished  the game is over, whether or not it has been graded yet
    """
    get = pick.get if hasattr(pick, "get") else (
        lambda k, d=None: pick[k] if k in pick.keys() else d)
    if get("result"):
        return {"label": "Finished", "tone": "green"}
    state = get("game_status")
    if state == "in_progress":
        from ..picker.live import is_stale
        if not is_stale(get("start_utc")):
            return {"label": "Live", "tone": "live"}
    if state in ("in_progress", "final"):
        # Over but unsettled. Almost always a player prop whose box score
        # ESPN has not published yet - saying "Finished" next to an empty
        # result just looks broken, so it says what it is waiting for.
        return {"label": "Settling", "tone": "grey"}
    return {"label": "Open", "tone": "orange"}


def _kick_live(path, sport: str | None = None) -> bool:
    """Start a live refresh off the request thread and say if a game is on.

    The page never waits on ESPN: it renders what the database holds and the
    refresh lands in time for the next load, which is why pages showing a live
    game reload themselves.
    """
    from ..picker import live as live_mod
    try:
        live_mod.refresh_async(path, sport)
        return live_mod.any_live(path, sport)
    except Exception:
        return False


def bet_label(pick) -> str:
    """How a committed bet reads on any page.

    A player prop stores its market as prop:<espn stat key> and its side in
    the team column, which spells out as "Over PROP:RUSHINGYARDS" if a page
    formats it like a game line. Nobody can tell whose bet that is, so a prop
    is named after the player.
    """
    from ..picker import autopick

    market = pick["market"] if hasattr(pick, "keys") else pick.get("market")
    get = pick.get if hasattr(pick, "get") else (
        lambda k, d=None: pick[k] if k in pick.keys() else d)
    side = (get("team") or "").strip()
    line = get("line")

    if autopick.is_prop_market(market):
        stat = sport_registry.get(get("sport_code")).stat_labels.get(
            autopick.stat_key_of(market), autopick.stat_key_of(market))
        player = get("player") or "Unknown player"
        number = f"{line:g}" if line is not None else ""
        if side.lower() == "under":
            return f"{player} under {number} {stat}".replace("  ", " ")
        return f"{player} {number}+ {stat}".replace("  ", " ")

    if line is None:
        return f"{side} {(market or '').upper()}"
    return f"{side} {(market or '').upper()} {line:+g}"


MINUS = "−"


def minus_text(value) -> str:
    """Swap a leading hyphen for a true minus sign."""
    text = "" if value is None else str(value)
    return MINUS + text[1:] if text.startswith("-") else text


def odds_text(price) -> str:
    """American odds as they read on a board: +108, −155."""
    if price is None:
        return ""
    try:
        return minus_text(f"{int(price):+d}")
    except (TypeError, ValueError):
        return str(price)


def signed_text(value, fmt: str = "%+.1f", suffix: str = "") -> str:
    """A signed figure with a true minus sign, e.g. signed(0.042*100, '%+.1f', '%')."""
    if value is None:
        return ""
    try:
        return minus_text(fmt % float(value)) + suffix
    except (TypeError, ValueError):
        return str(value)


def nice_date(value) -> str:
    """'2026-11-01' or a UTC timestamp -> 'Sun, Nov 1'. Anything else passes through."""
    if not value:
        return ""
    if isinstance(value, (dt.date, dt.datetime)):
        day = value
    else:
        try:
            day = dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            return str(value)
    return f"{day:%a}, {day:%b} {day.day}"


def kickoff_text(start_utc) -> str:
    """A UTC start time in local time: 'Sun 7:30 pm'."""
    if not start_utc:
        return ""
    try:
        moment = dt.datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
    except ValueError:
        return str(start_utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    local = moment.astimezone()
    hour = local.hour % 12 or 12
    return f"{local:%a} {hour}:{local:%M} {'am' if local.hour < 12 else 'pm'}"


_SCORE = re.compile(r"^(\S+) (\d+) - (\d+) (\S+)$")


def dashless(text) -> str:
    """Live strings come from ESPN as 'VAN 0 - 0 SJ' and '19:11 - 1st Period'.
    Read them as 'VAN 0, SJ 0' and '19:11, 1st Period'."""
    if not text:
        return ""
    text = str(text).strip()
    match = _SCORE.match(text)
    if match:
        away, away_score, home_score, home = match.groups()
        return f"{away} {away_score}, {home} {home_score}"
    return text.replace(" - ", ", ").replace(" – ", ", ").replace(" — ", ", ")


def bet_text(pick) -> str:
    """How a bet reads out loud: 'DET to win', 'PHI +5.5', 'Over 54.5'.

    bet_label keeps the stored market visible for the ledger and tests; this
    is the plain version for the page.
    """
    from ..picker import autopick

    if hasattr(pick, "get"):
        get = pick.get
    elif hasattr(pick, "keys"):
        get = lambda k, d=None: pick[k]  # noqa: E731 - sqlite rows
    else:
        # Board candidates are objects; their side is `side`, not `team`.
        get = lambda k, d=None: getattr(pick, "side" if k == "team" else k, d)  # noqa: E731
    market = (get("market") or "").lower()
    if autopick.is_prop_market(market):
        return minus_text(bet_label(pick))
    side = (get("team") or "").strip()
    line = get("line")
    if market in ("h2h", "ml", "moneyline"):
        return f"{side} to win"
    if market in ("spread", "spreads") and line is not None:
        return f"{side} {minus_text(f'{line:+g}')}"
    if market in ("total", "totals") and line is not None:
        return f"{side.capitalize()} {line:g}"
    return minus_text(bet_label(pick))


def _free_slice(picks: list[dict]) -> list[dict]:
    """What a free plan may see of the open picks: a few per league per day."""
    allowance = gating.free_pick_allowance()
    if allowance is None:
        return picks
    seen: dict[tuple, int] = {}
    out = []
    for pick in picks:
        key = (pick.get("sport_code"), pick.get("game_date"))
        if seen.get(key, 0) < allowance:
            seen[key] = seen.get(key, 0) + 1
            out.append(pick)
    return out


def _crawl_picks(path, limit: int = 200) -> list[dict]:
    """Every unsettled pick whose game has not finished, soonest first.

    Rendered on every page, so it must never take a page down.
    """
    try:
        rows = _tracked_picks(path, limit=400)
    except Exception:
        return []
    live = [row for row in rows
            if not row.get("result") and row.get("game_status") != "final"
            and row.get("status") in ("approved", "edited", "scheduled")]
    live.sort(key=lambda row: (row.get("game_status") != "in_progress",
                               row.get("start_utc") or ""))
    return live[:limit]


def _autopick_rules() -> dict:
    from ..picker import autopick
    return autopick.settings()


def _model_summary(path) -> dict:
    rows = db.query(path, """SELECT r.result,r.units_won,
        COALESCE(p.units_final,p.units_model,1) AS risk
        FROM results r JOIN picks p ON p.id=r.pick_id WHERE p.source='model'""")
    wins = sum(row["result"] == "W" for row in rows)
    losses = sum(row["result"] == "L" for row in rows)
    units = sum(float(row["units_won"] or 0) for row in rows)
    risk = sum(float(row["risk"]) for row in rows)
    return {"wins": wins, "losses": losses, "decisions": wins + losses,
            "hit_rate": wins / (wins + losses) if wins + losses else 0,
            "units": units, "roi": units / risk if risk else 0}


def _performance(path) -> dict:
    rows = [dict(row) for row in db.query(path, """
        SELECT r.graded_utc,r.units_won,r.result,p.id
        FROM results r JOIN picks p ON p.id=r.pick_id
        ORDER BY r.graded_utc,p.id""")]
    base = starting_units()
    # Plotted as units won or lost, so the starting bankroll sits on the 0 line.
    profit = [0.0]
    labels = ["Start"]
    for row in rows:
        profit.append(profit[-1] + float(row["units_won"] or 0))
        labels.append(f"#{row['id']} {row['result']}")
    values = [base + p for p in profit]
    width, height, pad = 900, 260, 18
    low, high = min(profit), max(profit)
    spread = max(high - low, 2.0)
    if high == low:
        low, high = low - 1, high + 1

    def y_for(v: float) -> float:
        return pad + (height - 2 * pad) * (high - v + spread * 0.08) / (spread * 1.16)

    def x_for(index: int) -> float:
        return pad + (width - 2 * pad) * index / max(len(profit) - 1, 1)

    coords = [(x_for(i), y_for(v)) for i, v in enumerate(profit)]
    zero_y = y_for(0.0)

    # The line is grey up to the last time it crossed 0, then green or red
    # for the run it is on now, by which side of 0 it currently sits.
    positive = profit[-1] >= 0
    start = len(profit) - 1
    while start > 0 and (profit[start - 1] >= 0) == positive:
        start -= 1
    if start == 0:
        past, current = [], coords
    else:
        # Interpolate the exact crossing between the last two sides of 0.
        a, b = profit[start - 1], profit[start]
        t = (0 - a) / (b - a) if b != a else 0.0
        (x0, _), (x1, _) = coords[start - 1], coords[start]
        cross = (x0 + (x1 - x0) * t, zero_y)
        past, current = coords[:start] + [cross], [cross] + coords[start:]

    def fmt(pts):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    ticks = []
    # A tick sitting right on the 0 line would print over its label.
    near_zero = (high - low) * 0.12
    levels = {v for v in (low + (high - low) * i / 3 for i in range(4))
              if abs(v) > near_zero}
    for v in sorted(levels | {0.0}):
        ticks.append({"value": v, "y": round(y_for(v), 1), "label": f"{v:+.2f}" if v else "0"})
    return {"rows": rows, "values": values, "labels": labels,
            "points": fmt(coords), "past_points": fmt(past) if len(past) > 1 else "",
            "current_points": fmt(current), "zero_y": round(zero_y, 1),
            "latest": values[-1], "units": profit[-1],
            "positive": positive, "base": base,
            "width": width, "height": height, "ticks": ticks}


if __name__ == "__main__":
    from waitress import serve
    serve(create_app(), host="127.0.0.1", port=8502, threads=6)
