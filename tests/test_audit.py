import json

import pytest

from broker365 import config, services
from broker365.errors import Broker365Error
from tests.conftest import G


def entries():
    path = config.broker_home() / "audit.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_write_tool_is_audited_without_content(httpx_mock):
    httpx_mock.add_response(
        method="PUT",
        url=f"{G}/me/drive/root:/notes.txt:/content?%40microsoft.graph.conflictBehavior=fail",
        json={"id": "N1", "name": "notes.txt"},
    )
    services.call("files_write_text", remote_path="/notes.txt", content="TOP SECRET BODY")
    [entry] = entries()
    assert entry["tool"] == "files_write_text" and entry["ok"] is True
    assert entry["target"] == {"remote_path": "/notes.txt"}
    assert entry["result"]["id"] == "N1"
    assert "TOP SECRET" not in (config.broker_home() / "audit.log").read_text()


def test_failed_write_is_audited_with_error_type(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{G}/me/messages",
        status_code=403,
        json={"error": {"code": "ErrorAccessDenied", "message": "no"}},
    )
    with pytest.raises(Broker365Error):
        services.call("mail_send", to=["a@b.co", "c@d.co"], subject="s", body="secret body")
    [entry] = entries()
    assert entry["ok"] is False and entry["error"] == "GraphError"
    assert entry["target"] == {"to_count": 2}  # no subject/body recorded


def test_read_tools_are_not_audited(httpx_mock):
    httpx_mock.add_response(url=f"{G}/me/drive/root/children?%24top=100", json={"value": []})
    services.call("files_list", path="/")
    assert entries() == []


def test_audit_failure_never_fails_a_completed_write(httpx_mock, monkeypatch):
    from broker365 import audit

    def broken_append(entry):
        raise PermissionError("read-only home")

    httpx_mock.add_response(method="POST", url=f"{G}/me/messages/D1/send", status_code=202)
    monkeypatch.setattr(audit, "_append", broken_append)
    assert services.call("mail_send_draft", draft_id="D1") == {"sent": True, "draft_id": "D1"}


def test_audit_records_attachments_and_share_link(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{G}/me/drive/items/X1/createLink",
        json={"link": {"webUrl": "https://1drv.ms/x", "type": "view", "scope": "anonymous"}},
    )
    services.call("files_share", item="X1", scope="anonymous")
    [entry] = entries()
    assert entry["target"]["scope"] == "anonymous"
    assert entry["result"]["url"] == "https://1drv.ms/x"


def test_admin_actions_are_audited_from_shared_functions(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from broker365 import audit, auth, hermes
    from broker365.cli import app
    from tests.test_auth import BOB, SIGNED_IN, FakeApp

    fake = FakeApp(accounts=[BOB], device_results=[SIGNED_IN])
    monkeypatch.setattr(auth, "app", lambda: fake)
    flow = auth.device_flow_start(["files"])
    auth.device_flow_complete(flow["flow_id"], wait_seconds=1)
    auth.set_default("BOB@outlook.com")
    auth.logout()
    (tmp_path / "hh").mkdir()
    (tmp_path / "hh" / "config.yaml").write_text("model: x\n")
    hermes.install(str(tmp_path / "hh"), dry_run=True)  # dry run: nothing changed, not audited
    hermes.install(str(tmp_path / "hh"))
    d = tmp_path / "docs"
    d.mkdir()
    result = CliRunner().invoke(app, ["settings", "--read-only", "--allow-dir", str(d)])
    assert result.exit_code == 0, result.output
    CliRunner().invoke(app, ["settings"])  # no change, not audited

    got = [(e["tool"], e["target"], e["ok"]) for e in audit.read()]
    assert got == [
        ("admin:settings", {"read_only": True, "dirs_added": [str(d.resolve())]}, True),
        ("admin:hermes_install", {"hermes_home": str(tmp_path / "hh")}, True),
        ("admin:logout", {"provider": "microsoft", "username": "bob@outlook.com"}, True),
        ("admin:set_default", {"provider": "microsoft", "username": "bob@outlook.com"}, True),
        (
            "admin:login",
            {"provider": "microsoft", "username": "bob@outlook.com", "scopes": ["files"]},
            True,
        ),
    ]
    assert all(e["ts"] for e in audit.read())
    assert "secret-device-code" not in (config.broker_home() / "audit.log").read_text()


@pytest.mark.parametrize("block", [7, 64, 1 << 16])
def test_read_returns_tail_of_large_log(monkeypatch, block):
    from broker365 import audit

    monkeypatch.setattr(audit, "_BLOCK", block)
    path = config.broker_home() / "audit.log"
    lines = [json.dumps({"tool": "t", "n": i, "note": "é" * (i % 5)}) for i in range(10_000)]
    lines[-3] = "{broken"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert [e["n"] for e in audit.read(5)] == [9999, 9998, 9996, 9995, 9994]
    assert audit.read(0) == []
    path.write_text('{"n": 1}\n{"n": 2}', encoding="utf-8")  # no trailing newline
    assert audit.read(5) == [{"n": 2}, {"n": 1}]
