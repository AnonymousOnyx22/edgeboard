import pytest

from src.db import database as db
from src.web.app import create_app


@pytest.fixture()
def client(tmp_path):
    path = tmp_path / "web.sqlite"
    db.init_db(path)
    app = create_app(path)
    app.testing = True
    return app.test_client()


@pytest.mark.parametrize("path, heading", [
    ("/", b"Your edge"),
    ("/games", b"games"),
    ("/model-picks", b"Model picks"),
    ("/performance", b"Performance"),
    ("/settings", b"Settings"),
])
def test_pages_render(client, path, heading):
    response = client.get(path)
    assert response.status_code == 200
    assert heading in response.data
    assert b"Streamlit" not in response.data
    assert b"Edgeboard" in response.data


def test_stylesheet_is_served(client):
    """Assert the contract, not the palette - colours are meant to change."""
    response = client.get("/static/app.css")
    assert response.status_code == 200
    for token in (b"--bg:", b"--ink:", b"--ease-out:", b"--sidebar:"):
        assert token in response.data


def test_script_is_served(client):
    response = client.get("/static/app.js")
    assert response.status_code == 200
    assert b"IntersectionObserver" in response.data


def test_navigation_collapse_is_wired(client):
    """The rail's toggle and the mobile drawer both need their hooks present."""
    body = client.get("/").data
    assert b'id="navToggle"' in body
    assert b'id="menuButton"' in body
    assert b'id="sidebar"' in body
    assert b'id="scrim"' in body


def test_reveal_styles_only_apply_when_scripting_is_available(client):
    """Content must never be left invisible if app.js fails to run."""
    assert b"document.documentElement.classList.add('js')" in client.get("/").data
    assert b"html.js .metric" in client.get("/static/app.css").data


def test_jargon_is_not_user_facing(client):
    """'Slate' is insider language; the UI says games."""
    for path in ("/", "/games", "/model-picks"):
        assert b"slate" not in client.get(path).data.lower()
