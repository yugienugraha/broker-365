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
