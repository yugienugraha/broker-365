import base64
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from broker365 import config
from broker365.errors import Broker365Error, PathNotAllowed
from broker365.services import _mail, mail
from broker365.services._shapes import html_to_text
from tests.conftest import G


def test_attachment_on_unc_share_rejected_without_contacting_it(monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.setattr(config, "_WINDOWS", True)
    monkeypatch.setattr(config, "allowed_dirs", lambda: [tmp_path])
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: pytest.fail(f"resolved {self}"))
    with pytest.raises(PathNotAllowed, match="UNC"):
        mail.mail_send(["a@b.co"], "s", "b", attachments=["\\\\evil.example\\s\\a.pdf"])


MSG = {
    "id": "AAMk/1=",
    "subject": "Hi",
    "from": {"emailAddress": {"name": "Alice", "address": "alice@contoso.com"}},
    "receivedDateTime": "2026-10-14T08:00:00Z",
    "isRead": False,
    "bodyPreview": "hello",
}


def _url(path: str) -> re.Pattern:
    return re.compile(re.escape(f"{G}{path}") + r"(\?.*)?$")


def _body(req) -> dict:
    return json.loads(req.content)


def _query(req) -> dict:
    return parse_qs(urlsplit(str(req.url)).query)


def test_list_params_and_unread_filter(httpx_mock):
    httpx_mock.add_response(url=_url("/me/mailFolders/inbox/messages"), json={"value": [MSG]})
    out = mail.mail_list(unread_only=True, limit=5)
    assert out[0]["from"] == "Alice <alice@contoso.com>"
    q = _query(httpx_mock.get_requests()[0])
    assert q["$filter"] == ["receivedDateTime ge 1900-01-01T00:00:00Z and isRead eq false"]
    assert q["$orderby"] == ["receivedDateTime desc"]
    assert q["$top"] == ["5"]
    assert "bodyPreview" in q["$select"][0]


def test_search_escapes_quotes(httpx_mock):
    httpx_mock.add_response(url=_url("/me/messages"), json={"value": []})
    mail.mail_search('say "hi"')
    q = _query(httpx_mock.get_requests()[0])
    assert q["$search"] == ['"say \\"hi\\""']
    assert "$orderby" not in q


def test_read_converts_html_and_quotes_id(httpx_mock):
    msg = {
        **MSG,
        "body": {
            "contentType": "html",
            "content": "<html><style>x{}</style><p>Hello&amp;bye</p><br>line2</html>",
        },
        "toRecipients": [{"emailAddress": {"address": "me@x.com"}}],
        "attachments": [{"id": "att1", "name": "a.pdf", "size": 10}],
    }
    httpx_mock.add_response(url=_url("/me/messages/AAMk%2F1%3D"), json=msg)
    out = mail.mail_read("AAMk/1=", max_chars=8)
    assert out["body"] == "Hello&by"
    assert out["truncated"] is True
    assert out["to"] == ["me@x.com"]
    assert out["attachments"][0]["name"] == "a.pdf"


def test_html_to_text():
    assert html_to_text("<p>a</p><p>b&nbsp;c</p><script>x</script>") == "a\nb c"


def test_send_without_confirm_creates_draft(httpx_mock):
    httpx_mock.add_response(
        method="POST", url=f"{G}/me/messages", json={"id": "d1", "webLink": "https://outlook/d1"}
    )
    out = mail.mail_send(["bob@contoso.com"], "S", "B")
    assert out == {
        "sent": False,
        "draft_id": "d1",
        "web_link": "https://outlook/d1",
        "note": mail.DRAFT_NOTE,
    }
    reqs = httpx_mock.get_requests()
    assert len(reqs) == 1 and not str(reqs[0].url).endswith("sendMail")


def test_send_with_confirm_and_attachment(httpx_mock):
    (config.workspace() / "r.txt").write_bytes(b"report")
    httpx_mock.add_response(method="POST", url=f"{G}/me/sendMail", status_code=202)
    out = mail.mail_send(
        ["bob@contoso.com"],
        "S",
        "<b>B</b>",
        cc=["c@contoso.com"],
        body_type="html",
        attachments=["r.txt"],
        confirm=True,
    )
    assert out["sent"] is True
    body = _body(httpx_mock.get_requests()[0])
    assert body["saveToSentItems"] is True
    msg = body["message"]
    assert msg["body"]["contentType"] == "HTML"
    assert msg["ccRecipients"][0]["emailAddress"]["address"] == "c@contoso.com"
    att = msg["attachments"][0]
    assert att["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert att["name"] == "r.txt"
    assert base64.b64decode(att["contentBytes"]) == b"report"


def test_attachment_outside_sandbox(tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("x")
    with pytest.raises(PathNotAllowed):
        mail.mail_send(["bob@contoso.com"], "S", "B", attachments=[str(f)], confirm=True)


def test_attachment_too_large(monkeypatch):
    monkeypatch.setattr(_mail, "ATTACHMENT_MAX", 3)
    (config.workspace() / "big.bin").write_bytes(b"1234")
    with pytest.raises(Broker365Error, match="files_share"):
        mail.mail_send(["bob@contoso.com"], "S", "B", attachments=["big.bin"], confirm=True)


@pytest.mark.parametrize("bad", ["bob", "bob@", "a b@x.com", "x@y", "<a@b.com>"])
def test_invalid_email_rejected(bad):
    with pytest.raises(Broker365Error, match="Invalid email"):
        mail.mail_draft([bad], "S", "B")


def test_reply_draft_vs_send(httpx_mock):
    httpx_mock.add_response(
        method="POST", url=f"{G}/me/messages/m1/createReplyAll", json={"id": "d2"}
    )
    httpx_mock.add_response(method="POST", url=f"{G}/me/messages/m1/reply", status_code=202)
    assert mail.mail_reply("m1", "ok", reply_all=True)["draft_id"] == "d2"
    assert mail.mail_reply("m1", "ok", confirm=True)["sent"] is True
    assert [_body(r) for r in httpx_mock.get_requests()] == [{"comment": "ok"}] * 2


def test_forward_draft(httpx_mock):
    httpx_mock.add_response(
        method="POST", url=f"{G}/me/messages/m1/createForward", json={"id": "d3"}
    )
    assert mail.mail_forward("m1", ["z@contoso.com"], "fyi")["sent"] is False
    body = _body(httpx_mock.get_requests()[0])
    assert body["toRecipients"][0]["emailAddress"]["address"] == "z@contoso.com"


def test_move_and_mark_read(httpx_mock):
    httpx_mock.add_response(method="POST", url=f"{G}/me/messages/m1/move", json={"id": "m2"})
    httpx_mock.add_response(method="PATCH", url=f"{G}/me/messages/m2", json={})
    assert mail.mail_move("m1", "archive")["id"] == "m2"
    mail.mail_mark_read("m2", False)
    reqs = httpx_mock.get_requests()
    assert _body(reqs[0]) == {"destinationId": "archive"}
    assert _body(reqs[1]) == {"isRead": False}


def test_folders(httpx_mock):
    httpx_mock.add_response(
        url=_url("/me/mailFolders"),
        json={
            "value": [
                {"id": "f", "displayName": "Inbox", "unreadItemCount": 2, "totalItemCount": 9}
            ]
        },
    )
    assert mail.mail_folders() == [{"id": "f", "name": "Inbox", "unread": 2, "total": 9}]


def test_send_draft_sends_the_reviewed_draft(httpx_mock):
    httpx_mock.add_response(method="POST", url=f"{G}/me/messages/D%2F1/send", status_code=202)
    assert mail.mail_send_draft("D/1") == {"sent": True, "draft_id": "D/1"}
