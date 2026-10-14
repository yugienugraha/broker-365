import yaml

from broker365 import hermes

EXISTING = """\
# Hermes config
model: gpt
mcp_servers:
    time:
        command: "uvx"
        args: ["mcp-server-time"]
display:
  theme: dark
"""


def test_append_when_no_section():
    out = hermes.patched_config("model: gpt\n")
    cfg = yaml.safe_load(out)
    assert cfg["model"] == "gpt"
    assert cfg["mcp_servers"]["m365"]["args"] == ["-m", "broker365", "mcp"]
    assert "BROKER365_HOME" in cfg["mcp_servers"]["m365"]["env"]


def test_insert_matches_existing_indent_and_keeps_comments():
    out = hermes.patched_config(EXISTING)
    cfg = yaml.safe_load(out)
    assert set(cfg["mcp_servers"]) == {"time", "m365"}
    assert cfg["display"]["theme"] == "dark"
    assert out.startswith("# Hermes config")
    assert hermes._has_server(out) and not hermes._has_server(EXISTING)


def test_empty_inline_mapping():
    cfg = yaml.safe_load(hermes.patched_config("mcp_servers: {}\nx: 1\n"))
    assert "m365" in cfg["mcp_servers"] and cfg["x"] == 1


def test_install_is_idempotent_and_backs_up(tmp_path):
    (tmp_path / "config.yaml").write_text(EXISTING)
    first = hermes.install(str(tmp_path))
    assert first["mcp_server"] == "added"
    assert (tmp_path / "skills/productivity/microsoft-365/SKILL.md").exists()
    second = hermes.install(str(tmp_path))
    assert "already present" in second["mcp_server"]
    assert len(list(tmp_path.glob("config.yaml.bak-*"))) == 1


def test_windows_path_round_trips(monkeypatch):
    monkeypatch.setattr(hermes.sys, "executable", r"C:\Users\me\venv\Scripts\python.exe")
    cfg = yaml.safe_load(hermes.patched_config(""))
    assert cfg["mcp_servers"]["m365"]["command"] == r"C:\Users\me\venv\Scripts\python.exe"


def test_explicit_home_without_config_is_an_error(tmp_path):
    import pytest

    from broker365.errors import Broker365Error

    with pytest.raises(Broker365Error, match="No config.yaml"):
        hermes.hermes_home(str(tmp_path))
