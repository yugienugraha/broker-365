import base64
import email
import json
from email import policy

import pytest

from broker365 import audit, config, services
from broker365.errors import Broker365Error, PathNotAllowed, ReadOnlyMode
from broker365.providers import google
from broker365.services import gmail
from tests.test_gdrive import at, sent

GMAIL = gmail.GMAIL


@pytest.fixture(autouse=True)
def account(monkeypatch):
    """The signed-in Gmail address (from the provider's stored record) and a cold label
    cache for every test."""
    monkeypatch.setattr(google, "account_email", lambda username=None: "Me@gmail.com")
    monkeypatch.setattr(gmail, "_labels", {})


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def parse_raw(request) -> email.message.EmailMessage:
    body = json.loads(request.content)
    raw = (body.get("message") or body)["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + "==="), policy=policy.default)


def headers(**h):
    return [{"name": k.replace("_", "-"), "value": v} for k, v in h.items()]


def meta_message(id_="M1", **h):
    return {
        "id": id_,
        "threadId": "T1",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Hi there",
        "payload": {"headers": headers(**h)},
    }


# ---- list / search / read ---------------------------------------------------------------


def test_list_gets_ids_then_metadata(httpx_mock):
    httpx_mock.add_response(
        **at("GET", f"{GMAIL}/messages?"), json={"messages": [{"id": "M1"}, {"id": "M2"}]}
    )
    httpx_mock.add_response(
        **at("GET", f"{GMAIL}/messages/M1?"),
        json=meta_message("M1", From="Ann <ann@x.com>", Subject="Lunch", Date="Mon"),
    )
    httpx_mock.add_response(**at("GET", f"{GMAIL}/messages/M2?"), json=meta_message("M2"))
    out = gmail.gmail_list(unread_only=True, limit=2)
    assert out[0]["subject"] == "Lunch" and out[0]["from"] == "Ann <ann@x.com>"
    assert out[0]["is_read"] is False and out[0]["thread_id"] == "T1"
    listing = sent(httpx_mock, "GET", f"{GMAIL}/messages?")[0].url.params
    assert listing["labelIds"] == "INBOX" and listing["q"] == "is:unread"
    assert listing["maxResults"] == "2" and listing["fields"] == "messages/id,nextPageToken"
    meta = sent(httpx_mock, "GET", f"{GMAIL}/messages/M1")[0].url.params
    assert meta["format"] == "metadata" and meta["fields"] == gmail.SUMMARY_FIELDS
    assert meta.get_list("metadataHeaders") == ["From", "To", "Subject", "Date"]
    assert [m["id"] for m in out] == ["M1", "M2"]  # order kept despite concurrent fetches


def test_search_passes_gmail_query(httpx_mock):
    httpx_mock.add_response(**at("GET", f"{GMAIL}/messages?"), json={})
    assert gmail.gmail_search("from:ann has:attachment") == []
    assert sent(httpx_mock, "GET", GMAIL)[0].url.params["q"] == "from:ann has:attachment"


def full_message(payload):
    return {"id": "M1", "threadId": "T1", "labelIds": [], "payload": payload}


def test_read_prefers_text_plain_in_alternative(httpx_mock):
    payload = {
        "mimeType": "multipart/mixed",
        "headers": headers(From="a@x.com", To="b@x.com, C <c@x.com>", Subject="S"),
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("Plain café".encode())},
                     "headers": headers(Content_Type="text/plain; charset=UTF-8")},
                    {"mimeType": "text/html", "body": {"data": b64(b"<p>HTML</p>")}},
                ],
            },
            {
                "partId": "1",
                "mimeType": "application/pdf",
                "filename": "q.pdf",
                "body": {"attachmentId": "ATT1", "size": 1234},
            },
        ],
    }  # fmt: skip
    httpx_mock.add_response(**at("GET", f"{GMAIL}/messages/M1"), json=full_message(payload))
    out = gmail.gmail_read("M1")
    assert out["body"] == "Plain café"
    assert out["to"] == ["b@x.com", "c@x.com"]
    assert out["attachments"] == [
        {"id": "ATT1", "part_id": "1", "name": "q.pdf", "size": 1234,
         "content_type": "application/pdf"}
    ]  # fmt: skip


def test_read_html_only_is_converted(httpx_mock):
    payload = {
        "mimeType": "text/html",
        "headers": headers(Subject="S"),
        "body": {"data": b64(b"<p>Hello</p><p>World&nbsp;!</p><script>x</script>")},
    }
    httpx_mock.add_response(**at("GET", f"{GMAIL}/messages/M1"), json=full_message(payload))
    out = gmail.gmail_read("M1", max_chars=7)
    assert out["body"] == "Hello\nW" and out["truncated"] is True


def test_read_latin1_charset(httpx_mock):
    payload = {
        "mimeType": "text/plain",
        "headers": headers(Content_Type='text/plain; charset="iso-8859-1"'),
        "body": {"data": b64("Grüße".encode("latin-1"))},
    }
    httpx_mock.add_response(**at("GET", f"{GMAIL}/messages/M1"), json=full_message(payload))
    assert gmail.gmail_read("M1")["body"] == "Grüße"


# ---- attachments ------------------------------------------------------------------------


def attachment_message(filename):
    return full_message(
        {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "partId": "1",
                    "mimeType": "text/plain",
                    "filename": filename,
                    "body": {"attachmentId": "ATT1", "size": 3},
                }
            ],
        }
    )


def test_download_attachment_sanitises_name(httpx_mock):
    httpx_mock.add_response(
        **at("GET", f"{GMAIL}/messages/M1?"), json=attachment_message("../../evil.txt")
    )
    httpx_mock.add_response(
        **at("GET", f"{GMAIL}/messages/M1/attachments/ATT1"), json={"data": b64(b"abc")}
    )
    out = gmail.gmail_download_attachment("M1", "1")  # part id works too
    assert out["local_path"] == str(config.workspace() / "_.._evil.txt")
    assert open(out["local_path"], "rb").read() == b"abc"


def test_download_attachment_sandbox(httpx_mock, tmp_path):
    httpx_mock.add_response(**at("GET", f"{GMAIL}/messages/M1?"), json=attachment_message("a.txt"))
    httpx_mock.add_response(
        **at("GET", f"{GMAIL}/messages/M1/attachments/ATT1"), json={"data": b64(b"x")}
    )
    with pytest.raises(PathNotAllowed):
        gmail.gmail_download_attachment("M1", "ATT1", dest=str(tmp_path / "out" / "a.txt"))


# ---- composing ----------------------------------------------------------------------------


def test_draft_builds_mime_with_cc_bcc_html_and_attachment(httpx_mock):
    (config.workspace() / "r.pdf").write_bytes(b"%PDF")
    httpx_mock.add_response(**at("POST", f"{GMAIL}/drafts"), json={"id": "D1"})
    out = gmail.gmail_draft(
        ["a@x.com"], "Report", "<p>See <b>this</b></p>", cc=["c@x.com"], bcc=["b@x.com"],
        html=True, attachments=["r.pdf"],
    )  # fmt: skip
    assert out == {
        "sent": False,
        "draft_id": "D1",
        "web_link": "https://mail.google.com/mail/#drafts",
        "note": gmail.DRAFT_NOTE,
    }
    msg = parse_raw(sent(httpx_mock, "POST", GMAIL)[0])
    assert msg["To"] == "a@x.com" and msg["Cc"] == "c@x.com" and msg["Bcc"] == "b@x.com"
    assert msg["Subject"] == "Report"
    body = msg.get_body(("plain",)).get_content().strip()
    assert body == "See this"
    assert "<b>this</b>" in msg.get_body(("html",)).get_content()
    (att,) = msg.iter_attachments()
    assert att.get_filename() == "r.pdf" and att.get_content() == b"%PDF"
    assert att.get_content_type() == "application/pdf"


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"subject": "Hi\r\nBcc: spy@evil.com"}, "single line"),
        ({"subject": "Hi\nX: y"}, "single line"),
        ({"to": ["a@x.com\r\nBcc: spy@evil.com"]}, "Invalid email"),
        ({"to": []}, "At least one recipient"),
        ({"cc": ["not an address"]}, "Invalid email"),
    ],
)
def test_header_injection_and_bad_addresses_refused(kwargs, match, httpx_mock):
    args = {"to": ["a@x.com"], "subject": "S", "body": "b", **kwargs}
    with pytest.raises(Broker365Error, match=match):
        gmail.gmail_draft(**args)
    assert httpx_mock.get_requests() == []


def test_attachment_outside_sandbox_refused(tmp_path, httpx_mock):
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    with pytest.raises(PathNotAllowed):
        gmail.gmail_draft(["a@x.com"], "S", "b", attachments=[str(secret)])
    assert httpx_mock.get_requests() == []


def test_send_is_draft_first(httpx_mock):
    httpx_mock.add_response(**at("POST", f"{GMAIL}/drafts"), json={"id": "D2"})
    out = gmail.gmail_send(["a@x.com"], "S", "b")
    assert out["sent"] is False and out["draft_id"] == "D2" and "gmail_send_draft" in out["note"]
    assert sent(httpx_mock, "POST", f"{GMAIL}/messages/send") == []


def test_send_with_confirm_and_send_draft(httpx_mock):
    httpx_mock.add_response(**at("POST", f"{GMAIL}/messages/send"), json={"id": "S1"})
    assert gmail.gmail_send(["a@x.com"], "S", "b", confirm=True)["sent"] is True
    assert "raw" in json.loads(sent(httpx_mock, "POST", GMAIL)[0].content)
    httpx_mock.add_response(**at("POST", f"{GMAIL}/drafts/send"), json={"id": "S2"})
    assert gmail.gmail_send_draft("D2") == {"sent": True, "draft_id": "D2", "id": "S2"}
    assert json.loads(sent(httpx_mock, "POST", f"{GMAIL}/drafts/send")[0].content) == {"id": "D2"}


# ---- reply ------------------------------------------------------------------------------


def reply_setup(httpx_mock, **h):
    httpx_mock.add_response(**at("GET", f"{GMAIL}/messages/M1"), json=meta_message("M1", **h))


def test_reply_draft_threads(httpx_mock):
    reply_setup(
        httpx_mock, From="Ann <ann@x.com>", To="me@gmail.com", Subject="Plan",
        Message_ID="<m1@x.com>", References="<m0@x.com>",
    )  # fmt: skip
    httpx_mock.add_response(**at("POST", f"{GMAIL}/drafts"), json={"id": "D3"})
    out = gmail.gmail_reply("M1", "Sounds good")
    assert out["draft_id"] == "D3" and out["sent"] is False
    req = sent(httpx_mock, "POST", f"{GMAIL}/drafts")[0]
    assert json.loads(req.content)["message"]["threadId"] == "T1"
    msg = parse_raw(req)
    assert msg["To"] == "ann@x.com" and msg["Cc"] is None
    assert msg["Subject"] == "Re: Plan"
    assert msg["In-Reply-To"] == "<m1@x.com>"
    assert msg["References"] == "<m0@x.com> <m1@x.com>"


def test_reply_all_excludes_self_and_keeps_re(httpx_mock):
    reply_setup(
        httpx_mock, From="ann@x.com", To="me@gmail.com, bob@x.com",
        Cc="ME@GMAIL.COM, carl@x.com, ann@x.com", Subject="RE: Plan",
    )  # fmt: skip
    httpx_mock.add_response(**at("POST", f"{GMAIL}/messages/send"), json={"id": "S3"})
    out = gmail.gmail_reply("M1", "ok", reply_all=True, confirm=True)
    assert out["sent"] is True and out["reply_all"] is True
    req = sent(httpx_mock, "POST", f"{GMAIL}/messages/send")[0]
    assert json.loads(req.content)["threadId"] == "T1"
    msg = parse_raw(req)
    assert msg["To"] == "ann@x.com" and msg["Cc"] == "bob@x.com, carl@x.com"
    assert msg["Subject"] == "RE: Plan"
    assert sent(httpx_mock, "GET", f"{GMAIL}/profile") == []  # self comes from the provider


# ---- labels -----------------------------------------------------------------------------


LABELS = {
    "labels": [
        {"id": "INBOX", "name": "INBOX"},
        {"id": "UNREAD", "name": "UNREAD"},
        {"id": "Label_7", "name": "Clients/Acme"},
    ]
}


def test_label_names_resolve_to_ids(httpx_mock):
    httpx_mock.add_response(**at("GET", f"{GMAIL}/labels"), json=LABELS)
    for _ in range(2):
        httpx_mock.add_response(
            **at("POST", f"{GMAIL}/messages/M1/modify"), json={"labelIds": ["Label_7"]}
        )
    out = gmail.gmail_label("M1", add=["clients/acme"], mark_read=True, archive=True)
    assert out["labels"] == ["Label_7"]
    body = json.loads(sent(httpx_mock, "POST", GMAIL)[0].content)
    assert body == {"addLabelIds": ["Label_7"], "removeLabelIds": ["UNREAD", "INBOX"]}
    lookup = sent(httpx_mock, "GET", f"{GMAIL}/labels")
    assert lookup[0].url.params["fields"] == "labels(id,name)"
    gmail.gmail_label("M1", remove=["Clients/Acme"])  # cached: no second labels.list
    assert len(sent(httpx_mock, "GET", f"{GMAIL}/labels")) == 1


def test_system_labels_need_no_lookup(httpx_mock):
    httpx_mock.add_response(**at("POST", f"{GMAIL}/messages/M1/modify"), json={"labelIds": []})
    gmail.gmail_label("M1", add=["starred", "CATEGORY_PERSONAL"], mark_read=False)
    assert sent(httpx_mock, "GET") == []
    body = json.loads(sent(httpx_mock, "POST", GMAIL)[0].content)
    assert body == {"addLabelIds": ["STARRED", "CATEGORY_PERSONAL", "UNREAD"], "removeLabelIds": []}


def test_label_cache_refreshes_once_on_miss(httpx_mock):
    httpx_mock.add_response(**at("GET", f"{GMAIL}/labels"), json=LABELS)
    newer = {"labels": [*LABELS["labels"], {"id": "Label_9", "name": "New"}]}
    httpx_mock.add_response(**at("GET", f"{GMAIL}/labels"), json=newer)
    for _ in range(2):
        httpx_mock.add_response(**at("POST", f"{GMAIL}/messages/M1/modify"), json={})
    gmail.gmail_label("M1", add=["Clients/Acme"])
    gmail.gmail_label("M1", add=["New"])  # made since the cache was filled
    assert len(sent(httpx_mock, "GET", f"{GMAIL}/labels")) == 2
    assert json.loads(sent(httpx_mock, "POST", GMAIL)[1].content)["addLabelIds"] == ["Label_9"]


def test_unknown_label_lists_known_ones(httpx_mock):
    httpx_mock.add_response(**at("GET", f"{GMAIL}/labels"), json=LABELS)
    with pytest.raises(Broker365Error, match="Unknown Gmail label 'Nope'.*Label_7"):
        gmail.gmail_label("M1", add=["Nope"])
    assert sent(httpx_mock, "POST", GMAIL) == []


def test_trash_never_deletes(httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{GMAIL}/messages/M1/trash"), json={"labelIds": ["TRASH"]}
    )
    assert gmail.gmail_trash("M1")["trashed"] is True
    assert sent(httpx_mock, "DELETE") == []


# ---- registry, audit --------------------------------------------------------------------


def test_flags_and_read_only(monkeypatch):
    tools = services.load_all()
    destructive = {n for n, t in tools.items() if n.startswith("gmail_") and t.destructive}
    assert destructive == {"gmail_send", "gmail_send_draft", "gmail_reply", "gmail_trash"}
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    visible = {t.name for t in services.available_tools()}
    assert {"gmail_list", "gmail_read", "gmail_download_attachment"} <= visible
    assert not {"gmail_draft", "gmail_send", "gmail_label", "gmail_trash"} & visible
    with pytest.raises(ReadOnlyMode):
        services.call("gmail_send_draft", draft_id="D1")


def test_audit_counts_recipients_and_hides_content(httpx_mock):
    httpx_mock.add_response(**at("POST", f"{GMAIL}/drafts"), json={"id": "D9"})
    services.call(
        "gmail_send", to=["a@x.com", "b@x.com"], bcc=["c@x.com"],
        subject="Secret subject", body="Secret body",
    )  # fmt: skip
    entry = audit.read(1)[0]
    assert entry["tool"] == "gmail_send"
    assert entry["target"] == {"to_count": 2, "bcc_count": 1}
    assert entry["result"] == {"draft_id": "D9"}
    log = (config.broker_home() / "audit.log").read_text(encoding="utf-8")
    assert "Secret" not in log and "a@x.com" not in log


def test_download_uses_parts_mask_and_size_cap(httpx_mock, monkeypatch):
    monkeypatch.setattr(gmail, "DOWNLOAD_MAX", 2)
    httpx_mock.add_response(**at("GET", f"{GMAIL}/messages/M1?"), json=attachment_message("a.txt"))
    with pytest.raises(Broker365Error, match="more than broker365 downloads"):
        gmail.gmail_download_attachment("M1", "ATT1")
    req = sent(httpx_mock, "GET", GMAIL)[0]
    assert req.url.params["fields"] == gmail.PARTS_FIELDS
    assert sent(httpx_mock, "GET", f"{GMAIL}/messages/M1/attachments") == []


def test_attachment_content_type_from_shared_guess(httpx_mock):
    (config.workspace() / "notes.md").write_text("# hi")
    httpx_mock.add_response(**at("POST", f"{GMAIL}/drafts"), json={"id": "D1"})
    gmail.gmail_draft(["a@x.com"], "S", "b", attachments=["notes.md"])
    (att,) = parse_raw(sent(httpx_mock, "POST", GMAIL)[0]).iter_attachments()
    assert att.get_content_type() == "text/markdown"  # not platform-dependent


@pytest.mark.parametrize("given, label_id", [("inbox", "INBOX"), ("Starred", "STARRED")])
def test_list_label_names_are_case_insensitive(given, label_id, httpx_mock):
    httpx_mock.add_response(**at("GET", f"{GMAIL}/messages?"), json={})
    gmail.gmail_list(label=given)
    assert sent(httpx_mock, "GET", GMAIL)[0].url.params["labelIds"] == label_id
    assert sent(httpx_mock, "GET", f"{GMAIL}/labels") == []


def test_list_user_label_name_is_resolved(httpx_mock):
    httpx_mock.add_response(**at("GET", f"{GMAIL}/labels"), json=LABELS)
    httpx_mock.add_response(**at("GET", f"{GMAIL}/messages?"), json={})
    gmail.gmail_list(label="clients/acme")
    listing = [r for r in sent(httpx_mock, "GET", f"{GMAIL}/messages")][0]
    assert listing.url.params["labelIds"] == "Label_7"


def test_reply_to_own_message_goes_to_its_recipients(httpx_mock):
    reply_setup(
        httpx_mock, From="Me <me@gmail.com>", To="ann@x.com, me@gmail.com",
        Cc="bob@x.com", Subject="Plan",
    )  # fmt: skip
    httpx_mock.add_response(**at("POST", f"{GMAIL}/drafts"), json={"id": "D4"})
    gmail.gmail_reply("M1", "follow-up", reply_all=True)
    msg = parse_raw(sent(httpx_mock, "POST", f"{GMAIL}/drafts")[0])
    assert msg["To"] == "ann@x.com" and msg["Cc"] == "bob@x.com"


def test_reply_to_a_note_to_self_goes_to_self(httpx_mock):
    # Seen live: a mail sent to yourself is replied to yourself, as in Gmail's UI.
    reply_setup(httpx_mock, From="me@gmail.com", To="ME@gmail.com", Subject="Note to self")
    httpx_mock.add_response(**at("POST", f"{GMAIL}/drafts"), json={"id": "D5"})
    gmail.gmail_reply("M1", "x")
    assert parse_raw(sent(httpx_mock, "POST", f"{GMAIL}/drafts")[0])["To"] == "me@gmail.com"


def test_reply_without_any_recipient_is_refused(httpx_mock):
    reply_setup(httpx_mock, From="me@gmail.com", Subject="Unaddressed")
    with pytest.raises(Broker365Error, match="no one else to reply to"):
        gmail.gmail_reply("M1", "x")
    assert sent(httpx_mock, "POST") == []


@pytest.mark.parametrize("name", ["fwd.eml", "bundle.mht"])
def test_message_and_multipart_attachments_are_octet_stream(name, httpx_mock, monkeypatch):
    from broker365.services import gdrive

    (config.workspace() / name).write_bytes(b"Subject: x\r\n\r\nbody")
    mime = "message/rfc822" if name.endswith(".eml") else "multipart/related"
    monkeypatch.setattr(gdrive, "upload_mime", lambda n: mime)
    httpx_mock.add_response(**at("POST", f"{GMAIL}/drafts"), json={"id": "D1"})
    gmail.gmail_draft(["a@x.com"], "S", "b", attachments=[name])
    (att,) = parse_raw(sent(httpx_mock, "POST", GMAIL)[0]).iter_attachments()
    assert att.get_content_type() == "application/octet-stream"
    assert att.get_content() == b"Subject: x\r\n\r\nbody"


def test_discard_draft_moves_it_to_trash(httpx_mock):
    httpx_mock.add_response(
        **at("GET", f"{GMAIL}/drafts/D1"), json={"id": "D1", "message": {"id": "MX"}}
    )
    httpx_mock.add_response(**at("POST", f"{GMAIL}/messages/MX/trash"), json={})
    out = gmail.gmail_discard_draft("D1")
    assert out == {"discarded": True, "draft_id": "D1", "message_id": "MX"}
    assert sent(httpx_mock, "GET", GMAIL)[0].url.params["format"] == "minimal"
    assert sent(httpx_mock, "DELETE") == []
    assert services.load_all()["gmail_discard_draft"].writes
    assert not services.load_all()["gmail_discard_draft"].destructive
    assert "gmail_discard_draft" in gmail.DRAFT_NOTE


@pytest.mark.parametrize("label", ["TRASH", "spam"])
def test_label_refuses_trash_and_spam(httpx_mock, label):
    with pytest.raises(Broker365Error, match="gmail_trash"):
        gmail.gmail_label("M1", add=[label])
    assert httpx_mock.get_requests() == []


def test_reply_to_header_pointing_at_self_is_not_a_note_to_self(httpx_mock):
    reply_setup(httpx_mock, From="forms@service.com", Reply_To="me@gmail.com", Subject="Form")
    with pytest.raises(Broker365Error, match="no one else to reply to"):
        gmail.gmail_reply("M1", "x")
