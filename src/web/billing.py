"""Plans and payments: the pricing page, Stripe Checkout, the customer
portal, and the webhook that keeps subscriptions in step with Stripe.

Stripe is optional. Without the library or the keys the pricing page still
renders and says plainly that payments are not switched on yet.

The webhook is verified with Stripe's signing scheme directly (an HMAC of
"<timestamp>.<body>"), so it needs no library and can be tested offline.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import time

from flask import (Blueprint, abort, current_app, flash, redirect, render_template,
                   request, url_for)

from ..db import database as db
from . import gating

bp = Blueprint("billing", __name__)

# Endpoints the app's CSRF check must skip: Stripe signs these itself.
CSRF_EXEMPT = frozenset({"billing.webhook"})

SIGNATURE_TOLERANCE = 300  # seconds


class SignatureError(ValueError):
    pass


def _path() -> str:
    return current_app.config["DB_PATH"]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ------------------------------------------------------------------ config
def secret_key() -> str:
    return os.getenv("STRIPE_SECRET_KEY", "")


def webhook_secret() -> str:
    return os.getenv("STRIPE_WEBHOOK_SECRET", "")


def price_id(plan: str, interval: str) -> str:
    """STRIPE_PRICE_PRO_MONTHLY and friends, else settings.yaml
    service.stripe_prices.<plan>.<interval>."""
    from_env = os.getenv(f"STRIPE_PRICE_{plan.upper()}_{interval.upper()}", "")
    if from_env:
        return from_env
    prices = gating.service_settings().get("stripe_prices") or {}
    return str((prices.get(plan) or {}).get(interval) or "")


def plan_for_price(price: str) -> tuple[str, str] | None:
    """Map a Stripe price id back to (plan, interval)."""
    for plan in ("pro", "sharp"):
        for interval in ("monthly", "yearly"):
            if price and price == price_id(plan, interval):
                return plan, interval
    return None


def _stripe():
    """The stripe module configured with the secret key, or None."""
    if not secret_key():
        return None
    try:
        import stripe  # noqa: PLC0415 - optional dependency
    except ImportError:
        return None
    stripe.api_key = secret_key()
    return stripe


def payments_ready() -> bool:
    return _stripe() is not None and all(
        price_id(plan, interval) for plan in ("pro", "sharp")
        for interval in ("monthly", "yearly"))


# ----------------------------------------------------------------- pricing
@bp.get("/pricing")
def pricing():
    from .app import _model_summary  # noqa: PLC0415 - avoids a circular import
    try:
        record = _model_summary(_path())
    except Exception:
        record = None
    user = gating.current_user()
    return render_template(
        "pricing.html", active="pricing", prices=gating.plan_prices(),
        features=gating.FEATURES, record=record, payments_ready=payments_ready(),
        user=user, user_plan=gating.plan_of(user) if user else None,
        want=request.args.get("want"))


@bp.post("/billing/checkout")
def checkout():
    plan = request.form.get("plan")
    interval = request.form.get("interval")
    if plan not in ("pro", "sharp") or interval not in ("monthly", "yearly"):
        abort(400, description="Pick a plan and how often to pay.")
    user = gating.current_user()
    if user is None:
        flash("Make a free account first, then pick your plan.", "info")
        return redirect(url_for("accounts.signup", next=url_for("billing.pricing")))
    stripe = _stripe()
    price = price_id(plan, interval)
    if stripe is None or not price:
        flash("Payments are not switched on yet. Nothing has been charged.", "info")
        return redirect(url_for("billing.pricing"))

    subscription_data = {"metadata": {"user_id": str(user["id"]), "plan": plan,
                                      "interval": interval}}
    if interval == "yearly":
        subscription_data["trial_period_days"] = 7
    params = {
        "mode": "subscription",
        "line_items": [{"price": price, "quantity": 1}],
        "success_url": url_for("accounts.account", checkout="done", _external=True),
        "cancel_url": url_for("billing.pricing", _external=True),
        "client_reference_id": str(user["id"]),
        "metadata": {"user_id": str(user["id"]), "plan": plan, "interval": interval},
        "subscription_data": subscription_data,
        "allow_promotion_codes": True,
    }
    if user.get("stripe_customer_id"):
        params["customer"] = user["stripe_customer_id"]
    else:
        params["customer_email"] = user["email"]
    try:
        checkout_session = stripe.checkout.Session.create(**params)
    except Exception as exc:  # Stripe errors carry a readable message
        flash(f"Stripe could not start checkout: {getattr(exc, 'user_message', None) or exc}",
              "error")
        return redirect(url_for("billing.pricing"))
    return redirect(checkout_session.url, code=303)


@bp.post("/billing/portal")
def portal():
    user = gating.current_user()
    if user is None:
        return redirect(url_for("accounts.login", next=url_for("accounts.account")))
    stripe = _stripe()
    if stripe is None or not user.get("stripe_customer_id"):
        flash("There is no billing to manage on this account yet.", "info")
        return redirect(url_for("accounts.account"))
    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"],
            return_url=url_for("accounts.account", _external=True))
    except Exception as exc:
        flash(f"Stripe could not open billing: {getattr(exc, 'user_message', None) or exc}",
              "error")
        return redirect(url_for("accounts.account"))
    return redirect(portal_session.url, code=303)


# ----------------------------------------------------------------- webhook
def verify_signature(payload: bytes, header: str, secret: str,
                     tolerance: int = SIGNATURE_TOLERANCE, now: float | None = None) -> dict:
    """Check a Stripe-Signature header and return the parsed event."""
    if not secret:
        raise SignatureError("no webhook secret configured")
    parts = {}
    signatures = []
    for item in (header or "").split(","):
        key, _, value = item.strip().partition("=")
        if key == "v1":
            signatures.append(value)
        elif key:
            parts[key] = value
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError as exc:
        raise SignatureError("no timestamp") from exc
    if abs((now or time.time()) - timestamp) > tolerance:
        raise SignatureError("timestamp outside tolerance")
    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise SignatureError("signature mismatch")
    return json.loads(payload)


def sign_payload(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a Stripe-Signature header. Used by tests and local replay."""
    timestamp = int(timestamp or time.time())
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload,
                      hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _period_end(obj: dict) -> str | None:
    value = obj.get("current_period_end")
    if value is None:
        items = ((obj.get("items") or {}).get("data") or [])
        value = items[0].get("current_period_end") if items else None
    if value is None:
        return None
    return dt.datetime.fromtimestamp(int(value), dt.timezone.utc).isoformat()


def _user_for(customer: str | None, metadata: dict | None) -> int | None:
    if customer:
        rows = db.query(_path(), "SELECT id FROM users WHERE stripe_customer_id=?", (customer,))
        if rows:
            return rows[0]["id"]
    user_id = (metadata or {}).get("user_id")
    if user_id and str(user_id).isdigit():
        rows = db.query(_path(), "SELECT id FROM users WHERE id=?", (int(user_id),))
        if rows:
            return rows[0]["id"]
    return None


def _save_subscription(user_id: int, plan: str, interval: str | None, status: str,
                       subscription_id: str | None, period_end: str | None) -> None:
    db.execute(_path(), """
        INSERT INTO subscriptions (user_id, plan, interval, status,
                                   stripe_subscription_id, current_period_end, updated_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            plan=excluded.plan, interval=excluded.interval, status=excluded.status,
            stripe_subscription_id=COALESCE(excluded.stripe_subscription_id,
                                            subscriptions.stripe_subscription_id),
            current_period_end=COALESCE(excluded.current_period_end,
                                        subscriptions.current_period_end),
            updated_utc=excluded.updated_utc""",
        (user_id, plan, interval, status, subscription_id, period_end, _now_iso()))


def handle_event(event: dict) -> str:
    """Apply one Stripe event. Returns a short note of what happened."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed":
        metadata = obj.get("metadata") or {}
        user_id = _user_for(None, {"user_id": obj.get("client_reference_id")
                                   or metadata.get("user_id")})
        if user_id is None:
            return "no matching user"
        if obj.get("customer"):
            db.execute(_path(), "UPDATE users SET stripe_customer_id=? WHERE id=?",
                       (obj["customer"], user_id))
        plan = metadata.get("plan")
        if plan not in ("pro", "sharp"):
            return "checkout without a plan"
        _save_subscription(user_id, plan, metadata.get("interval"),
                           "trialing" if metadata.get("interval") == "yearly" else "active",
                           obj.get("subscription"), None)
        return f"user {user_id} on {plan}"

    if kind in ("customer.subscription.created", "customer.subscription.updated",
                "customer.subscription.deleted"):
        metadata = obj.get("metadata") or {}
        user_id = _user_for(obj.get("customer"), metadata)
        if user_id is None:
            return "no matching user"
        if obj.get("customer"):
            db.execute(_path(), """UPDATE users SET stripe_customer_id=?
                                   WHERE id=? AND stripe_customer_id IS NULL""",
                       (obj["customer"], user_id))
        items = ((obj.get("items") or {}).get("data") or [])
        mapped = plan_for_price(((items[0].get("price") or {}).get("id")) if items else "")
        plan = metadata.get("plan") or (mapped[0] if mapped else None)
        interval = metadata.get("interval") or (mapped[1] if mapped else None)
        if plan not in ("pro", "sharp"):
            return "subscription for an unknown price"
        status = "canceled" if kind.endswith("deleted") else (obj.get("status") or "active")
        _save_subscription(user_id, plan, interval, status, obj.get("id"), _period_end(obj))
        return f"user {user_id} {status} on {plan}"

    return "ignored"


@bp.post("/billing/webhook")
def webhook():
    payload = request.get_data()
    try:
        event = verify_signature(payload, request.headers.get("Stripe-Signature", ""),
                                 webhook_secret())
    except (SignatureError, json.JSONDecodeError):
        return "invalid signature", 400
    return {"received": True, "result": handle_event(event)}
