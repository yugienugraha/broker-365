import pytest

from broker365 import auth, graph

G = "https://graph.microsoft.com/v1.0"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Isolated broker home + workspace, fake token, no retry sleeps, fresh http client."""
    for var in (
        "HERMES_HOME",
        "BROKER365_ACCOUNT",
        "BROKER365_READ_ONLY",
        "BROKER365_ALLOWED_DIRS",
        "BROKER365_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BROKER365_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BROKER365_CLIENT_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(auth, "get_token", lambda bundle, username=None: "test-token")
    monkeypatch.setattr(graph, "_sleep", lambda s: None)
    monkeypatch.setattr(graph, "_http", None)
    auth._tokens.clear()
    yield


@pytest.fixture
def personal(monkeypatch):
    monkeypatch.setattr(auth, "is_personal", lambda username=None: True)


@pytest.fixture
def business(monkeypatch):
    monkeypatch.setattr(auth, "is_personal", lambda username=None: False)
