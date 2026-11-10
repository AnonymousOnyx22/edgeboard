"""Accounts: sign up, log in, log out, and the account page.

Users live in the same SQLite file as everything else, in tables created
here the first time the blueprint is registered. Passwords are hashed with
werkzeug. Repeated wrong passwords lock that email out for a while.
"""
from __future__ import annotations

import datetime as dt
import re

from flask import (Blueprint, abort, current_app, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import database as db
from . import gating

bp = Blueprint("accounts", __name__)

MAX_FAILURES = 5
LOCKOUT = dt.timedelta(minutes=15)
MIN_PASSWORD = 8
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY,
    email               TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash       TEXT NOT NULL,
    created_utc         TEXT NOT NULL,
    age_confirmed_utc   TEXT NOT NULL,
    stripe_customer_id  TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id                      INTEGER PRIMARY KEY,
    user_id                 INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    plan                    TEXT NOT NULL DEFAULT 'free',
    interval                TEXT,
    status                  TEXT NOT NULL DEFAULT 'inactive',
    stripe_subscription_id  TEXT,
    current_period_end      TEXT,
    updated_utc             TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    email         TEXT PRIMARY KEY COLLATE NOCASE,
    failures      INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT
);
"""


def ensure_tables(db_path) -> None:
    conn = db.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@bp.record_once
def _create_tables(state) -> None:
    ensure_tables(state.app.config["DB_PATH"])


def install(app) -> None:
    """Register accounts, billing and the account template context."""
    from . import billing

    app.register_blueprint(bp)
    app.register_blueprint(billing.bp)
    app.context_processor(gating.account_context)


def _path() -> str:
    return current_app.config["DB_PATH"]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _safe_next(value: str | None) -> str:
    """Only ever bounce back to a page on this site."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return url_for("home")


def _start_session(user_id: int) -> None:
    # A fresh session on login, so a session fixed before it cannot be reused.
    session.clear()
    session["user_id"] = user_id
    session.permanent = True


# ------------------------------------------------------------------ lockout
def _locked_until(email: str) -> dt.datetime | None:
    rows = db.query(_path(), "SELECT locked_until FROM login_attempts WHERE email=?", (email,))
    if not rows or not rows[0]["locked_until"]:
        return None
    until = dt.datetime.fromisoformat(rows[0]["locked_until"])
    return until if until > _now() else None


def _record_failure(email: str) -> None:
    conn = db.connect(_path())
    try:
        row = conn.execute("SELECT failures FROM login_attempts WHERE email=?",
                           (email,)).fetchone()
        failures = (row["failures"] if row else 0) + 1
        locked = (_now() + LOCKOUT).isoformat() if failures >= MAX_FAILURES else None
        conn.execute("""INSERT INTO login_attempts (email, failures, locked_until)
                        VALUES (?, ?, ?)
                        ON CONFLICT(email) DO UPDATE SET failures=excluded.failures,
                                                          locked_until=excluded.locked_until""",
                     (email, 0 if locked else failures, locked))
        conn.commit()
    finally:
        conn.close()


def _clear_failures(email: str) -> None:
    db.execute(_path(), "DELETE FROM login_attempts WHERE email=?", (email,))


# ------------------------------------------------------------------- routes
@bp.route("/signup", methods=["GET", "POST"])
def signup():
    next_url = _safe_next(request.values.get("next") or url_for("billing.pricing"))
    if gating.current_user():
        return redirect(next_url)
    errors: dict[str, str] = {}
    email = (request.form.get("email") or "").strip().lower()
    if request.method == "POST":
        password = request.form.get("password") or ""
        if not EMAIL.match(email):
            errors["email"] = "Enter an email address like name@example.com."
        if len(password) < MIN_PASSWORD:
            errors["password"] = f"Use at least {MIN_PASSWORD} characters."
        if request.form.get("age") != "yes":
            errors["age"] = "Edgeboard is only for people 19 or older."
        if not errors and db.query(_path(), "SELECT 1 FROM users WHERE email=?", (email,)):
            errors["email"] = "That email already has an account. Log in instead."
        if not errors:
            now = _now().isoformat()
            user_id = db.execute(_path(), """INSERT INTO users
                (email, password_hash, created_utc, age_confirmed_utc) VALUES (?, ?, ?, ?)""",
                (email, generate_password_hash(password), now, now))
            db.execute(_path(), """INSERT INTO subscriptions (user_id, plan, status, updated_utc)
                                   VALUES (?, 'free', 'active', ?)""", (user_id, now))
            _start_session(user_id)
            flash("Your account is ready. You are on the free plan.", "success")
            response = redirect(next_url)
            response.set_cookie(gating.AGE_COOKIE, "19", max_age=365 * 24 * 3600,
                                samesite="Lax", httponly=True)
            return response
    status = 400 if errors else 200
    return render_template("signup.html", active=None, errors=errors, email=email,
                           next_url=next_url), status


@bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = _safe_next(request.values.get("next"))
    if gating.current_user():
        return redirect(next_url)
    error = ""
    email = (request.form.get("email") or "").strip().lower()
    if request.method == "POST":
        password = request.form.get("password") or ""
        locked = _locked_until(email) if email else None
        if locked:
            minutes = max(1, int((locked - _now()).total_seconds() // 60) + 1)
            error = (f"Too many wrong passwords for this email. Try again in "
                     f"{minutes} minute{'s' if minutes != 1 else ''}.")
        else:
            rows = db.query(_path(), "SELECT id, password_hash FROM users WHERE email=?", (email,))
            if rows and check_password_hash(rows[0]["password_hash"], password):
                _clear_failures(email)
                _start_session(rows[0]["id"])
                return redirect(next_url)
            _record_failure(email)
            error = "That email and password do not match an account."
    return render_template("login.html", active=None, error=error, email=email,
                           next_url=next_url), (401 if error else 200)


@bp.post("/logout")
def logout():
    session.clear()
    flash("You are logged out.", "info")
    return redirect(url_for("home"))


@bp.get("/account")
def account():
    user = gating.current_user()
    if user is None:
        return redirect(url_for("accounts.login", next=url_for("accounts.account")))
    plan = gating.plan_of(user)
    return render_template("account.html", active=None, user=user, user_plan=plan,
                           prices=gating.plan_prices(),
                           checkout_done=request.args.get("checkout") == "done")


# ------------------------------------------------------------------ age gate
@bp.post("/age")
def confirm_age():
    next_url = _safe_next(request.form.get("next"))
    if request.form.get("age") != "yes":
        return redirect(url_for("accounts.underage"))
    response = redirect(next_url)
    response.set_cookie(gating.AGE_COOKIE, "19", max_age=365 * 24 * 3600,
                        samesite="Lax", httponly=True)
    return response


@bp.get("/age/under")
def underage():
    if not gating.service_on():
        abort(404)
    return render_template("account.html", active=None, user=None, user_plan="free",
                           prices=gating.plan_prices(), underage=True,
                           checkout_done=False), 403
