import pytest

from broker365 import config
from broker365.errors import Broker365Error


def test_home_prefers_broker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER365_HOME", str(tmp_path / "b"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert config.broker_home() == tmp_path / "b"


def test_home_falls_back_to_hermes(tmp_path, monkeypatch):
    monkeypatch.delenv("BROKER365_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert config.broker_home() == tmp_path / "broker365"


def test_scopes_include_core_and_dedupe():
    assert config.scopes_for(["files", "files"]) == ["User.Read", "Files.ReadWrite"]


def test_unknown_bundle_rejected():
    with pytest.raises(Broker365Error):
        config.scopes_for(["nope"])
