import asyncio
import json

import pytest
from typer.testing import CliRunner

from broker365 import mcp_server, services
from broker365.cli import app
from tests.conftest import G

runner = CliRunner()


def test_tools_lists_registry():
    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 0
    assert "files_list" in result.output and "account_login_start" in result.output


def test_call_runs_tool(httpx_mock):
    httpx_mock.add_response(url=f"{G}/me/drive/root/children?%24top=100", json={"value": []})
    result = runner.invoke(app, ["call", "files_list", '{"path": "/"}'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_call_rejects_bad_args():
    result = runner.invoke(app, ["call", "files_list", '{"nope": 1}'])
    assert result.exit_code == 1
    assert "Bad arguments" in result.output


def test_call_passes_a_name_argument_through(monkeypatch):
    # gdrive_upload takes `name`; it must not collide with call()'s own tool-name parameter.
    seen = {}
    t = services.load_all()["gdrive_upload"]
    monkeypatch.setitem(
        services.TOOLS, "gdrive_upload", t.__class__(**{**t.__dict__, "fn": _spy(t.fn, seen)})
    )
    services.call("gdrive_upload", local_path="x.csv", name="Report")
    assert seen == {"local_path": "x.csv", "name": "Report"}


def _spy(fn, seen):
    import functools

    @functools.wraps(fn)
    def wrapper(**kwargs):
        seen.update(kwargs)

    return wrapper


def test_call_unknown_tool():
    result = runner.invoke(app, ["call", "does_not_exist"])
    assert result.exit_code == 1


def test_settings_read_only(monkeypatch):
    result = runner.invoke(app, ["settings", "--read-only"])
    assert result.exit_code == 0 and json.loads(result.output)["read_only"] is True
    assert "files_delete" not in runner.invoke(app, ["tools"]).output


def test_admin_consent_url():
    result = runner.invoke(app, ["admin-consent-url", "contoso.com"])
    assert "contoso.com/adminconsent?client_id=11111111" in result.output


def test_mcp_server_registers_all_tools_with_annotations():
    server = mcp_server.build_server()
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {t.name for t in services.available_tools()}
    assert tools["files_delete"].annotations.destructive_hint is True
    assert tools["files_list"].annotations.read_only_hint is True
    assert "path" in tools["files_list"].input_schema["properties"]


def test_mcp_enforces_read_only_at_call_time(monkeypatch):
    server = mcp_server.build_server()  # built while writes are allowed
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    with pytest.raises(Exception, match="read-only"):
        asyncio.run(server.call_tool("files_delete", {"item": "ABC!1"}))


def test_tools_catalog_matches_cli_and_flags_hidden(monkeypatch):
    catalog = {t["name"]: t for t in services.tool_catalog()}
    assert catalog["files_delete"] == {
        "name": "files_delete",
        "provider": "microsoft",
        "bundle": "files",
        "writes": True,
        "destructive": True,
        "description": catalog["files_delete"]["description"],
        "hidden": False,
    }
    assert (
        catalog["files_delete"]["description"]
        and "\n" not in catalog["files_delete"]["description"]
    )
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    catalog = {t["name"]: t for t in services.tool_catalog()}
    assert catalog["files_delete"]["hidden"] and not catalog["files_list"]["hidden"]
    output = runner.invoke(app, ["tools"]).output
    assert "files_delete" not in output and catalog["files_list"]["description"] in output


def test_settings_prints_effective_view(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    out = json.loads(runner.invoke(app, ["settings", "--allow-dir", str(d)]).output)
    assert out == {
        "read_only": False,
        "read_only_env": False,
        "allowed_dirs": [str(d.resolve())],
        "workspace": out["workspace"],
        "excel_backend": {},
    }
    out = json.loads(runner.invoke(app, ["settings", "--disallow-dir", str(d)]).output)
    assert out["allowed_dirs"] == []


def test_scopes_help_lists_user_bundles():
    from broker365 import config

    assert config.user_bundles()[0] == "files" and "core" not in config.user_bundles()
    assert "files-shared" in runner.invoke(app, ["login", "--help"]).output
