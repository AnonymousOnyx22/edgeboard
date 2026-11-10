"""Who is looking, what plan they are on, and what that plan unlocks.

Edgeboard runs in one of two modes:

  owner mode    (service off, the default) no accounts, every feature open.
                This is the local tool exactly as it has always been.
  service mode  (service on) visitors get the free tier; an account and a
                paid plan unlock the rest.

The switch is read in this order, first one set wins:
  app.config["EDGEBOARD_SERVICE"]   tests and embedding
  EDGEBOARD_SERVICE env var         "1"/"true" on, "0"/"false" off
  settings.yaml service.enabled
"""
from __future__ import annotations

import datetime as dt
import functools
import os

from flask import current_app, flash, g, redirect, request, session, url_for

from ..db import database as db

FEATURES = {
    "all_picks": "Every model pick in all five leagues",
    "hit_rates": "The full hit rate grid",
    "line_shopping": "Best price across books",
    "alerts": "Alerts when the model posts a pick",
    "ev": "Positive EV and no vig fair odds",
    "line_movement": "Line movement",
    "parlays": "Parlay builder",
    "export": "Data export",
}

PRO_FEATURES = {"all_picks", "hit_rates", "line_shopping", "alerts"}
SHARP_FEATURES = PRO_FEATURES | {"ev", "line_movement", "parlays", "export"}

# Display defaults. settings.yaml service.plans.<plan>.monthly / .yearly
# override the prices; the Stripe price ids come from the environment.
PLANS = {
    "free": {"name": "Free", "features": frozenset(), "monthly": 0.0, "yearly": 0.0},
    "pro": {"name": "Pro", "features": frozenset(PRO_FEATURES),
            "monthly": 14.99, "yearly": 99.0},
    "sharp": {"name": "Sharp", "features": frozenset(SHARP_FEATURES),
              "monthly": 29.99, "yearly": 199.0},
}
PLAN_ORDER = ("free", "pro", "sharp")

# The free tier still shows the model working, just not all of it.
FREE_PICKS_PER_LEAGUE_PER_DAY = 2

ACTIVE_STATUSES = {"active", "trialing", "past_due"}


def _truthy(value) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return None


def service_settings(config: dict | None = None) -> dict:
    """The settings.yaml `service:` block, never None."""
    if config is None:
        try:
            config = db.get_config()
        except Exception:
            config = {}
    return (config or {}).get("service") or {}


def service_on(config: dict | None = None) -> bool:
    """Is Edgeboard running as a paid service?"""
    try:
        override = current_app.config.get("EDGEBOARD_SERVICE")
    except RuntimeError:
        override = None
    if override is not None:
        return bool(override)
    from_env = _truthy(os.getenv("EDGEBOARD_SERVICE"))
    if from_env is not None:
        return from_env
    return bool(service_settings(config).get("enabled", False))


def plan_prices(config: dict | None = None) -> dict:
    """{plan: {"name", "features", "monthly", "yearly"}} with overrides applied."""
    overrides = service_settings(config).get("plans") or {}
    out = {}
    for code, base in PLANS.items():
        entry = dict(base)
        for interval in ("monthly", "yearly"):
            try:
                value = (overrides.get(code) or {}).get(interval)
                if value is not None:
                    entry[interval] = float(value)
            except (TypeError, ValueError):
                pass
        out[code] = entry
    return out


def unlocking_plan(feature: str) -> str:
    """The cheapest plan that includes a feature: 'pro' or 'sharp'."""
    for code in PLAN_ORDER:
        if feature in PLANS[code]["features"]:
            return code
    return "sharp"


def _db_path() -> str:
    return current_app.config["DB_PATH"]


def current_user():
    """The logged in user as a dict, or None. Cached for the request."""
    if "edgeboard_user" in g:
        return g.edgeboard_user
    user = None
    user_id = session.get("user_id")
    if user_id:
        rows = db.query(_db_path(), """
            SELECT u.id, u.email, u.created_utc, u.stripe_customer_id,
                   s.plan, s.interval, s.status, s.current_period_end,
                   s.stripe_subscription_id
            FROM users u LEFT JOIN subscriptions s ON s.user_id=u.id
            WHERE u.id=?""", (user_id,))
        user = dict(rows[0]) if rows else None
        if user is None:
            session.pop("user_id", None)
    g.edgeboard_user = user
    return user


def plan_of(user) -> str:
    """'free', 'pro' or 'sharp'. A lapsed or cancelled plan is free."""
    if not user:
        return "free"
    plan = user.get("plan") if hasattr(user, "get") else None
    if plan not in PLANS or plan == "free":
        return "free"
    if (user.get("status") or "") not in ACTIVE_STATUSES:
        return "free"
    period_end = user.get("current_period_end")
    if period_end:
        try:
            end = dt.datetime.fromisoformat(str(period_end).replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=dt.timezone.utc)
            # A day of grace covers a renewal webhook arriving late.
            if end + dt.timedelta(days=1) < dt.datetime.now(dt.timezone.utc):
                return "free"
        except ValueError:
            pass
    return plan


def current_plan() -> str:
    """The plan for this request; owner mode counts as everything."""
    if not service_on():
        return "sharp"
    return plan_of(current_user())


def has_feature(name: str) -> bool:
    if not service_on():
        return True
    return name in PLANS[plan_of(current_user())]["features"]


def free_pick_allowance() -> int | None:
    """How many picks a day per league this visitor sees; None means all."""
    return None if has_feature("all_picks") else FREE_PICKS_PER_LEAGUE_PER_DAY


def requires(feature: str):
    """Gate a view. Visitors are sent to log in, free users to pricing."""
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if has_feature(feature):
                return view(*args, **kwargs)
            if current_user() is None:
                return redirect(url_for("accounts.login", next=request.full_path.rstrip("?")))
            plan = PLANS[unlocking_plan(feature)]["name"]
            flash(f"{FEATURES.get(feature, 'That')} comes with {plan}.", "info")
            return redirect(url_for("billing.pricing", want=feature))
        return wrapped
    return decorator


def age_confirmed() -> bool:
    return request.cookies.get(AGE_COOKIE) == "19"


AGE_COOKIE = "edgeboard_age"


def account_context() -> dict:
    """Register with app.context_processor."""
    on = service_on()
    return {
        "service_on": on,
        "current_user": current_user() if on else None,
        "plan": current_plan(),
        "plans": PLANS,
        "has_feature": has_feature,
        "unlocking_plan": unlocking_plan,
        "feature_names": FEATURES,
        "show_age_gate": on and not age_confirmed() and request.endpoint not in (
            "accounts.confirm_age", "accounts.underage", "billing.webhook", "static"),
    }
