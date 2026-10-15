import webbrowser

import pytest
from starlette.testclient import TestClient

from broker365 import auth, gapi, transport
from broker365.providers import google
from broker365.ui import server

G = "https://graph.microsoft.com/v1.0"
# Dashboard (tests/test_ui.py, tests/test_google.py): session token, origin, API header.
TOKEN = "t0ken-for-tests"
PORT = 8365
BASE = f"http://127.0.0.1:{PORT}"
API = {"X-Broker365": "1"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, httpx_mock):
    """Isolated broker home + workspace, fake token, no retry sleeps, fresh http client.
    Requesting httpx_mock everywhere means no test can ever reach the real network."""
    for var in (
        "HERMES_HOME",
        "BROKER365_ACCOUNT",
        "BROKER365_READ_ONLY",
        "BROKER365_ALLOWED_DIRS",
        "BROKER365_WORKSPACE",
        "BROKER365_GOOGLE_CLIENT_ID",
        "BROKER365_GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BROKER365_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BROKER365_CLIENT_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(auth, "get_token", lambda bundle, username=None: "test-token")
    # Google API calls get a fake token without touching the Google provider's store (its own
    # tests exercise GoogleProvider.get_token directly).
    monkeypatch.setattr(gapi.GoogleClient, "_token", lambda self: "g-token")
    monkeypatch.setattr(transport, "_sleep", lambda s: None)
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: False)  # never a real browser
    monkeypatch.setattr(google, "INTERACTIVE_TIMEOUT", 5)
    monkeypatch.setattr(transport, "_http", None)
    auth._tokens.clear()
    auth._force_refresh.clear()
    google.reset()
    yield


@pytest.fixture
def personal(monkeypatch):
    monkeypatch.setattr(auth, "is_personal", lambda username=None: True)


@pytest.fixture
def business(monkeypatch):
    monkeypatch.setattr(auth, "is_personal", lambda username=None: False)


@pytest.fixture
def static(tmp_path):
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>broker365</title>")
    (d / "app.js").write_text("console.log(1)")
    return d


@pytest.fixture
def client(static):
    """A dashboard client without the session cookie (like a cross-site navigation)."""
    return TestClient(server.create_app(TOKEN, PORT, static), base_url=BASE)


@pytest.fixture
def authed(client):
    client.cookies.set(server.cookie_name(PORT), TOKEN)
    return client
