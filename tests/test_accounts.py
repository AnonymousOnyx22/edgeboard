import json
import time

import pytest

from src.db import database as db
from src.web import accounts, billing, gating
from src.web.app import create_app


@pytest.fixture()
def app(tmp_path, monkeypatch):
    for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "EDGEBOARD_SERVICE",
                 "STRIPE_PRICE_PRO_MONTHLY", "STRIPE_PRICE_PRO_YEARLY",
                 "STRIPE_PRICE_SHARP_MONTHLY", "STRIPE_PRICE_SHARP_YEARLY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "x.sqlite"
    db.init_db(path)
    application = create_app(path)
    application.testing = True
    # create_app installs accounts, billing and the webhook's CSRF exemption.
    application.config["EDGEBOARD_SERVICE"] = True
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def token(client):
    client.get("/pricing")
    with client.session_transaction() as sess:
        return sess["_csrf_token"]


def signup(client, email="a@example.com", password="correct horse", age="yes"):
    data = {"_csrf_token": token(client), "email": email, "password": password}
    if age:
        data["age"] = age
    return client.post("/signup", data=data)


def login(client, email="a@example.com", password="correct horse"):
    return client.post("/login", data={"_csrf_token": token(client), "email": email,
                                       "password": password})


def set_plan(app, email, plan, status="active"):
    with db.connect(app.config["DB_PATH"]) as conn:
        user_id = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
        conn.execute("UPDATE subscriptions SET plan=?, status=? WHERE user_id=?",
                     (plan, status, user_id))
        conn.commit()
    return user_id


def feature(app, client, name):
    """has_feature as seen inside a request made by this client."""
    with client.session_transaction() as sess:
        user_id = sess.get("user_id")
    with app.test_request_context("/"):
        from flask import session
        if user_id:
            session["user_id"] = user_id
        return gating.has_feature(name)


# ------------------------------------------------------------------ signup
def test_signup_requires_the_19_plus_box(client):
    response = signup(client, age=None)
    assert response.status_code == 400
    assert b"19 or older" in response.data
    assert not db.query(client.application.config["DB_PATH"], "SELECT 1 FROM users")


def test_signup_creates_a_free_account_and_logs_in(client, app):
    response = signup(client)
    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("user_id")
    row = db.query(app.config["DB_PATH"], """SELECT u.password_hash, s.plan FROM users u
        JOIN subscriptions s ON s.user_id=u.id WHERE u.email='a@example.com'""")[0]
    assert row["plan"] == "free"
    assert "correct horse" not in row["password_hash"]


def test_duplicate_email_is_refused_whatever_the_case(client):
    signup(client)
    client.post("/logout", data={"_csrf_token": token(client)})
    response = signup(client, email="A@Example.com")
    assert response.status_code == 400
    assert b"already has an account" in response.data


def test_signup_rejects_a_short_password_and_a_bad_email(client):
    response = signup(client, email="nope", password="short")
    assert response.status_code == 400
    assert b"name@example.com" in response.data
    assert b"at least 8" in response.data


# ------------------------------------------------------------- login/logout
def test_login_and_logout(client):
    signup(client)
    client.post("/logout", data={"_csrf_token": token(client)})
    with client.session_transaction() as sess:
        assert "user_id" not in sess
    assert login(client).status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("user_id")
    assert client.get("/account").status_code == 200


def test_login_never_redirects_off_site(client):
    signup(client)
    client.post("/logout", data={"_csrf_token": token(client)})
    response = client.post("/login", data={"_csrf_token": token(client),
                                           "email": "a@example.com",
                                           "password": "correct horse",
                                           "next": "//evil.example/steal"})
    assert response.headers["Location"].startswith("/")
    assert "evil" not in response.headers["Location"]


def test_repeated_wrong_passwords_lock_the_email(client):
    signup(client)
    client.post("/logout", data={"_csrf_token": token(client)})
    for _ in range(accounts.MAX_FAILURES):
        assert login(client, password="wrong password").status_code == 401
    # Even the right password is refused while the lock holds.
    response = login(client)
    assert response.status_code == 401
    assert b"Too many wrong passwords" in response.data


def test_csrf_still_guards_the_forms(client):
    assert client.post("/signup", data={"email": "a@example.com"}).status_code == 400


# ------------------------------------------------------------------ gating
def test_owner_mode_unlocks_everything(app, client):
    app.config["EDGEBOARD_SERVICE"] = False
    for name in gating.FEATURES:
        assert feature(app, client, name)
    # No account block, no age gate.
    body = client.get("/pricing").get_data(as_text=True)
    assert 'class="age-gate"' not in body


def test_free_users_are_held_back_from_paid_features(app, client):
    signup(client)
    assert not feature(app, client, "hit_rates")
    assert not feature(app, client, "ev")


def test_pro_unlocks_research_and_sharp_unlocks_everything(app, client):
    signup(client)
    set_plan(app, "a@example.com", "pro")
    assert feature(app, client, "hit_rates")
    assert feature(app, client, "line_shopping")
    assert not feature(app, client, "ev")
    set_plan(app, "a@example.com", "sharp")
    assert all(feature(app, client, name) for name in gating.FEATURES)


def test_a_cancelled_plan_falls_back_to_free(app, client):
    signup(client)
    set_plan(app, "a@example.com", "sharp", status="canceled")
    assert not feature(app, client, "hit_rates")


def test_the_decorator_sends_visitors_to_log_in_and_free_users_to_pricing(app, client):
    @app.get("/_locked")
    @gating.requires("hit_rates")
    def locked():
        return "inside"

    first = client.get("/_locked")
    assert first.status_code == 302 and "/login" in first.headers["Location"]
    signup(client)
    second = client.get("/_locked")
    assert second.status_code == 302 and "/pricing" in second.headers["Location"]
    set_plan(app, "a@example.com", "pro")
    assert client.get("/_locked").data == b"inside"


def test_the_age_gate_shows_until_answered(app, client):
    from flask import render_template_string

    gate = '{% include "_age_gate.html" %}'
    with app.test_request_context("/pricing"):
        assert 'class="age-gate"' in render_template_string(gate)
    with app.test_request_context("/pricing", headers={"Cookie": "edgeboard_age=19"}):
        assert 'class="age-gate"' not in render_template_string(gate)

    response = client.post("/age", data={"_csrf_token": token(client), "age": "yes",
                                         "next": "/pricing"})
    assert response.status_code == 302
    assert "edgeboard_age=19" in response.headers.get("Set-Cookie", "")
    under = client.post("/age", data={"_csrf_token": token(client), "age": "no"})
    assert "/age/under" in under.headers["Location"]


# ----------------------------------------------------------------- pricing
def test_pricing_renders_without_stripe_and_says_so(client):
    body = client.get("/pricing").get_data(as_text=True)
    assert "Payments are not switched on yet" in body
    for words in ("$14.99", "$99", "$29.99", "$199", "No guarantee of profit",
                  "1 866 531 2600"):
        assert words in body
    import re
    text = re.sub(r"<[^>]+>", " ", body).lower()
    for banned in (r"\bguaranteed\b", r"\blocks?\b", r"free money", r"risk free"):
        assert not re.search(banned, text), banned


def test_checkout_without_keys_charges_nothing(client):
    signup(client)
    response = client.post("/billing/checkout", data={"_csrf_token": token(client),
                                                      "plan": "pro", "interval": "monthly"})
    assert response.status_code == 302
    assert "/pricing" in response.headers["Location"]


# ----------------------------------------------------------------- webhook
def post_event(client, event, secret="whsec_test", signature=None):
    payload = json.dumps(event).encode()
    header = signature or billing.sign_payload(payload, secret)
    return client.post("/billing/webhook", data=payload,
                       headers={"Stripe-Signature": header,
                                "Content-Type": "application/json"})


def test_a_signed_webhook_moves_a_user_onto_a_plan(app, client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("STRIPE_PRICE_SHARP_YEARLY", "price_sharp_y")
    signup(client)
    user_id = db.query(app.config["DB_PATH"], "SELECT id FROM users")[0]["id"]

    done = post_event(client, {"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": str(user_id), "customer": "cus_123",
        "subscription": "sub_123",
        "metadata": {"plan": "pro", "interval": "monthly", "user_id": str(user_id)}}}})
    assert done.status_code == 200
    assert feature(app, client, "hit_rates")

    period_end = int(time.time()) + 30 * 24 * 3600
    updated = post_event(client, {"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_123", "customer": "cus_123", "status": "trialing", "metadata": {},
        "current_period_end": period_end,
        "items": {"data": [{"price": {"id": "price_sharp_y"}}]}}}})
    assert updated.status_code == 200
    row = db.query(app.config["DB_PATH"], "SELECT * FROM subscriptions WHERE user_id=?",
                   (user_id,))[0]
    assert (row["plan"], row["interval"], row["status"]) == ("sharp", "yearly", "trialing")
    assert feature(app, client, "ev")

    gone = post_event(client, {"type": "customer.subscription.deleted", "data": {"object": {
        "id": "sub_123", "customer": "cus_123", "status": "canceled",
        "metadata": {"plan": "sharp", "interval": "yearly"}}}})
    assert gone.status_code == 200
    assert not feature(app, client, "ev")


def test_an_unsigned_or_forged_webhook_is_refused(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    event = {"type": "checkout.session.completed", "data": {"object": {}}}
    assert post_event(client, event, secret="whsec_wrong").status_code == 400
    assert post_event(client, event, signature="t=1,v1=deadbeef").status_code == 400


def test_signatures_expire():
    payload = b"{}"
    header = billing.sign_payload(payload, "s", timestamp=1000)
    with pytest.raises(billing.SignatureError):
        billing.verify_signature(payload, header, "s", now=1000 + 10_000)
    assert billing.verify_signature(payload, header, "s", now=1001) == {}
