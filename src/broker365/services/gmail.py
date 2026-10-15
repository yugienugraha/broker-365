"""Gmail (Gmail v1) for a connected Google account: the gmail_* tools.

Sending and replying create a draft unless confirm=true; the user approves the draft and it is
sent with gmail_send_draft. Nothing is ever deleted permanently: gmail_trash moves a message
to the Trash.
"""

from __future__ import annotations

import base64
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any

from broker365 import config, gapi
from broker365.errors import Broker365Error
from broker365.providers import google as google_provider
from broker365.services import _mail, files, gdrive, tool
from broker365.services._shapes import compact, html_to_text, validate_emails
from broker365.transport import seg

BUNDLE = "gmail"
GOOGLE = config.GOOGLE
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
LIST_MAX = 100  # messages summarized per list/search call (one metadata GET each)
WORKERS = 8  # concurrent metadata GETs
DOWNLOAD_MAX = 35 * 1024 * 1024  # larger than any attachment Gmail accepts
SUMMARY_HEADERS = ["From", "To", "Subject", "Date"]
LIST_FIELDS = "messages/id,nextPageToken"
SUMMARY_FIELDS = "id,threadId,labelIds,snippet,internalDate,payload/headers"
_PART = "partId,filename,mimeType,headers,body/attachmentId,body/size"
# The MIME tree of a message down to five levels: enough to find any attachment part.
PARTS_FIELDS = f"payload({_PART},parts({_PART},parts({_PART},parts({_PART},parts({_PART})))))"
SYSTEM_LABELS = {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "SPAM", "TRASH", "SENT", "DRAFT"}
SEND_DRAFT = "gmail_send_draft"
DRAFT_NOTE = _mail.draft_note(SEND_DRAFT, "or discard it with gmail_discard_draft")
UPLOAD_HINT = (
    "Upload it to Google Drive (gdrive_upload) and share a link from gdrive_share instead."
)

# account email -> {lowercase label name or id: label id}, for the process lifetime
_labels: dict[str, dict[str, str]] = {}
_labels_lock = threading.Lock()


def _g() -> gapi.GoogleClient:
    return gapi.client(BUNDLE)


def _msg_url(message_id: str, suffix: str = "") -> str:
    return f"{GMAIL}/messages/{seg(message_id, 'message id')}{suffix}"


def _headers(payload: dict[str, Any] | None) -> dict[str, str]:
    """Message headers by lowercase name (the first of each)."""
    out: dict[str, str] = {}
    for h in (payload or {}).get("headers") or []:
        out.setdefault(str(h.get("name", "")).lower(), str(h.get("value", "")))
    return out


def _addrs(*values: str) -> list[str]:
    """The bare addresses in address header values ('Ann <a@x.com>, b@x.com')."""
    return [a for _, a in getaddresses([v for v in values if v]) if a]


def _summarize(m: dict[str, Any]) -> dict[str, Any]:
    h = _headers(m.get("payload"))
    labels = m.get("labelIds") or []
    return {
        "id": m.get("id"),
        "thread_id": m.get("threadId"),
        "subject": h.get("subject"),
        "from": h.get("from"),
        "received": h.get("date"),
        "is_read": "UNREAD" not in labels,
        "labels": labels,
        "preview": m.get("snippet"),
        "web_link": f"https://mail.google.com/mail/#all/{m.get('id')}",
    }


def _list(params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Summaries of up to `limit` (≤ LIST_MAX) messages: the ids, then their metadata fetched
    concurrently (order kept)."""
    client = _g()
    ids = client.paged(
        f"{GMAIL}/messages",
        {**params, "fields": LIST_FIELDS},
        "messages",
        max(1, min(limit, LIST_MAX)),
    )
    meta = {"format": "metadata", "metadataHeaders": SUMMARY_HEADERS, "fields": SUMMARY_FIELDS}

    def fetch(m: dict[str, Any]) -> dict[str, Any]:
        return _summarize(client.get(_msg_url(m["id"]), params=meta))

    if len(ids) <= 1:
        return [fetch(m) for m in ids]
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(ids))) as pool:
        return list(pool.map(fetch, ids))


def _decode(data: str | None) -> bytes:
    return base64.urlsafe_b64decode((data or "") + "=" * (-len(data or "") % 4))


def _charset(part: dict[str, Any]) -> str:
    ctype = _headers(part).get("content-type", "")
    for param in ctype.split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip('"')
    return "utf-8"


def _text_of(part: dict[str, Any]) -> str:
    data = _decode((part.get("body") or {}).get("data"))
    try:
        return data.decode(_charset(part), errors="replace")
    except LookupError:  # unknown charset name
        return data.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any]):
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def _is_attachment(part: dict[str, Any]) -> bool:
    return (
        bool(part.get("filename"))
        or "attachment" in _headers(part).get("content-disposition", "").lower()
    )


def _body(payload: dict[str, Any]) -> str:
    """The message text: the first text/plain part, else the first text/html as text."""
    parts = [p for p in _walk(payload) if not _is_attachment(p)]
    for mime, convert in (("text/plain", False), ("text/html", True)):
        for p in parts:
            if (p.get("mimeType") or "").lower() == mime and (p.get("body") or {}).get("data"):
                text = _text_of(p)
                return html_to_text(text) if convert else text
    return ""


def _attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": (p.get("body") or {}).get("attachmentId"),
            "part_id": p.get("partId"),
            "name": p.get("filename"),
            "size": (p.get("body") or {}).get("size"),
            "content_type": p.get("mimeType"),
        }
        for p in _walk(payload)
        if _is_attachment(p) and (p.get("body") or {}).get("attachmentId")
    ]


def _no_newlines(value: str, what: str) -> str:
    if "\r" in value or "\n" in value:
        raise Broker365Error(f"The {what} must be a single line (no line breaks)")
    return value


def _build(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    attachments: list[str] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """An RFC 5322 message, base64url-encoded for the Gmail API's `raw`. Addresses are
    validated and header values with line breaks are refused (header injection)."""
    msg = EmailMessage()
    msg["To"] = ", ".join(_mail.required_emails(to))
    if cc_list := validate_emails(cc):
        msg["Cc"] = ", ".join(cc_list)
    if bcc_list := validate_emails(bcc):
        msg["Bcc"] = ", ".join(bcc_list)
    msg["Subject"] = _no_newlines(subject, "subject")
    for name, value in (extra_headers or {}).items():
        msg[name] = _no_newlines(value, name)
    if html:
        msg.set_content(html_to_text(body))
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    for path in attachments or []:
        src = _mail.read_attachment(path, UPLOAD_HINT)
        mime = gdrive.upload_mime(src.name)
        if mime.startswith(("message/", "multipart/")):  # would be parsed, not attached
            mime = "application/octet-stream"
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(src.read_bytes(), maintype=maintype, subtype=subtype, filename=src.name)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _message(raw: str, thread_id: str | None = None) -> dict[str, Any]:
    return compact({"raw": raw, "threadId": thread_id})


def _draft(raw: str, thread_id: str | None = None) -> dict[str, Any]:
    d = _g().post(f"{GMAIL}/drafts", json={"message": _message(raw, thread_id)})
    return _mail.draft_result(
        d.get("id"),
        "https://mail.google.com/mail/#drafts",
        SEND_DRAFT,
        "or discard it with gmail_discard_draft",
    )


def _send(raw: str, thread_id: str | None = None) -> dict[str, Any]:
    return _g().post(f"{GMAIL}/messages/send", json=_message(raw, thread_id))


def _system_label(name: str) -> str | None:
    key = name.strip().upper()
    return key if key in SYSTEM_LABELS or key.startswith("CATEGORY_") else None


def _fetch_labels() -> dict[str, str]:
    labels = (_g().get(f"{GMAIL}/labels", params={"fields": "labels(id,name)"}) or {}).get(
        "labels"
    ) or []
    by_name: dict[str, str] = {}
    for lb in labels:
        by_name[str(lb.get("id", "")).lower()] = lb["id"]
        by_name[str(lb.get("name", "")).lower()] = lb["id"]
    return by_name


def _label_ids(names: list[str]) -> list[str]:
    """Label ids for names or ids. System labels need no lookup; user labels come from a
    per-account cache, re-read once when a name is missing (a label made since)."""
    if all(_system_label(n) for n in names):
        return [_system_label(n) for n in names]  # type: ignore[misc]
    account = google_provider.account_email().lower()
    with _labels_lock:
        by_name = _labels.get(account)
    wanted = [n.strip().lower() for n in names]
    if by_name is None or any(w not in by_name and not _system_label(w) for w in wanted):
        by_name = _fetch_labels()
        with _labels_lock:
            _labels[account] = by_name
    out = []
    for name, key in zip(names, wanted, strict=True):
        label_id = _system_label(name) or by_name.get(key)
        if not label_id:
            known = ", ".join(sorted(set(by_name.values())))
            raise Broker365Error(f"Unknown Gmail label '{name}'. Label ids: {known}")
        out.append(label_id)
    return out


# ---- tools ------------------------------------------------------------------------------


@tool(BUNDLE, GOOGLE)
def gmail_list(label: str = "INBOX", unread_only: bool = False, limit: int = 25) -> list[dict]:
    """List Gmail messages with a label (inbox, sent, draft, starred, important, a user
    label name or id; case-insensitive), newest first; at most 100."""
    params: dict[str, Any] = {"labelIds": _label_ids([label])[0]}
    if unread_only:
        params["q"] = "is:unread"
    return _list(params, limit)


@tool(BUNDLE, GOOGLE)
def gmail_search(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search Gmail with Gmail search syntax, e.g. 'from:alice@example.com has:attachment
    newer_than:7d'; at most 100 results."""
    if not query.strip():
        raise Broker365Error("A search query is required")
    return _list({"q": query}, limit)


@tool(BUNDLE, GOOGLE)
def gmail_read(message_id: str, max_chars: int = 50_000) -> dict[str, Any]:
    """Read one Gmail message: headers, recipients, plain-text body (HTML is converted) and
    the attachment list (name, size, id for gmail_download_attachment)."""
    m = _g().get(_msg_url(message_id), params={"format": "full"})
    payload = m.get("payload") or {}
    h = _headers(payload)
    text = _body(payload)
    return {
        **_summarize(m),
        "to": _addrs(h.get("to", "")),
        "cc": _addrs(h.get("cc", "")),
        "truncated": len(text) > max_chars,
        "body": text[:max_chars],
        "attachments": _attachments(payload),
    }


@tool(BUNDLE, GOOGLE)
def gmail_download_attachment(
    message_id: str, attachment_id: str, dest: str | None = None, overwrite: bool = False
) -> dict[str, Any]:
    """Save an attachment (id or part_id from gmail_read) to the local workspace, or to
    `dest` inside an allowed directory."""
    m = _g().get(_msg_url(message_id), params={"format": "full", "fields": PARTS_FIELDS})
    found = next(
        (
            a
            for a in _attachments(m.get("payload") or {})
            if attachment_id in (a["id"], a["part_id"])
        ),
        None,
    )
    if found and int(found.get("size") or 0) > DOWNLOAD_MAX:
        raise Broker365Error(
            f"{found['name']} is {found['size']} bytes, more than broker365 downloads "
            f"({DOWNLOAD_MAX} bytes). Open it in Gmail instead."
        )
    # Gmail may hand out a different attachmentId per read; fall back to the one given.
    att_id = found["id"] if found else attachment_id
    data = _g().get(_msg_url(message_id, f"/attachments/{seg(att_id, 'attachment id')}"))
    content = _decode((data or {}).get("data"))
    name = (found or {}).get("name") or "attachment"
    return {
        "message_id": message_id,
        "name": name,
        "size": len(content),
        "local_path": files.write_local(dest, name, content, overwrite),
    }


@tool(BUNDLE, GOOGLE, writes=True)
def gmail_draft(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """Save a new Gmail draft without sending it. `attachments` are local workspace paths,
    max 3 MB each. Returns the draft id."""
    return _draft(_build(to, subject, body, cc, bcc, html, attachments))


@tool(BUNDLE, GOOGLE, destructive=True)
def gmail_send(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    attachments: list[str] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Send an email from Gmail. Without confirm=true it only saves a draft for review (then
    send that draft with gmail_send_draft). `attachments` are local workspace paths, max 3 MB
    each."""
    raw = _build(to, subject, body, cc, bcc, html, attachments)
    if not confirm:
        return _draft(raw)
    return {"sent": True, "id": _send(raw).get("id"), "to": to, "subject": subject}


@tool(BUNDLE, GOOGLE, destructive=True)
def gmail_send_draft(draft_id: str) -> dict[str, Any]:
    """Send an existing Gmail draft (from gmail_draft / gmail_send / gmail_reply without
    confirm). Only call this after the user approved the draft's content."""
    if not draft_id:
        raise Broker365Error("A draft id is required")
    sent = _g().post(f"{GMAIL}/drafts/send", json={"id": draft_id})
    return {"sent": True, "draft_id": draft_id, "id": (sent or {}).get("id")}


@tool(BUNDLE, GOOGLE, writes=True)
def gmail_discard_draft(draft_id: str) -> dict[str, Any]:
    """Discard a Gmail draft by moving it to the Trash (restorable for 30 days; broker365
    never deletes permanently)."""
    if not draft_id:
        raise Broker365Error("A draft id is required")
    d = _g().get(f"{GMAIL}/drafts/{seg(draft_id, 'draft id')}", params={"format": "minimal"})
    message_id = ((d or {}).get("message") or {}).get("id")
    if not message_id:
        raise Broker365Error(f"Draft {draft_id} has no message to discard")
    _g().post(_msg_url(message_id, "/trash"))
    return {"discarded": True, "draft_id": draft_id, "message_id": message_id}


@tool(BUNDLE, GOOGLE, destructive=True)
def gmail_reply(
    message_id: str, body: str, reply_all: bool = False, confirm: bool = False
) -> dict[str, Any]:
    """Reply (or reply-all) to a Gmail message in its thread. Without confirm=true it only
    creates a reply draft. The original message is not quoted."""
    me = google_provider.account_email().lower()
    params = {
        "format": "metadata",
        "metadataHeaders": ["From", "Reply-To", "To", "Cc", "Subject", "Message-ID", "References"],
    }
    m = _g().get(_msg_url(message_id), params=params)
    h = _headers(m.get("payload"))
    own = me in {a.lower() for a in _addrs(h.get("from", ""))}
    if own:
        # Our own message (e.g. from Sent): like Gmail, reply to its original recipients.
        to_candidates, cc_candidates = _addrs(h.get("to", "")), _addrs(h.get("cc", ""))
    else:
        to_candidates = _addrs(h.get("reply-to") or h.get("from", ""))
        cc_candidates = _addrs(h.get("to", ""), h.get("cc", ""))
    seen = {me}
    to: list[str] = []
    for a in to_candidates:
        if a.lower() not in seen:
            seen.add(a.lower())
            to.append(a)
    if not to and own and to_candidates:
        to = [me]  # our own note to self (seen live): Gmail replies to yourself as well
    if not to:
        raise Broker365Error("This message has no one else to reply to")
    cc: list[str] = []
    for a in cc_candidates if reply_all else []:
        if a.lower() not in seen:
            seen.add(a.lower())
            cc.append(a)
    subject = h.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}".strip()
    threading_headers: dict[str, str] = {}
    if msg_id := h.get("message-id"):
        threading_headers["In-Reply-To"] = msg_id
        threading_headers["References"] = f"{h.get('references', '')} {msg_id}".strip()
    raw = _build(to, subject, body, cc, extra_headers=threading_headers)
    thread = m.get("threadId")
    if not confirm:
        return {**_draft(raw, thread), "reply_all": reply_all}
    sent = _send(raw, thread)
    return {"sent": True, "id": sent.get("id"), "message_id": message_id, "reply_all": reply_all}


@tool(BUNDLE, GOOGLE, writes=True, audit=("add", "remove", "mark_read", "archive"))
def gmail_label(
    message_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    mark_read: bool | None = None,
    archive: bool = False,
) -> dict[str, Any]:
    """Change a Gmail message's labels: `add`/`remove` label names (or ids), mark_read=true or
    false, archive=true (removes it from the Inbox). Nothing is deleted."""
    to_add, to_remove = list(add or []), list(remove or [])
    if mark_read is not None:
        (to_remove if mark_read else to_add).append("UNREAD")
    if archive:
        to_remove.append("INBOX")
    if not to_add and not to_remove:
        raise Broker365Error("Nothing to change: pass add, remove, mark_read or archive")
    ids = _label_ids(to_add + to_remove)
    if {"TRASH", "SPAM"} & set(ids[: len(to_add)]):
        # Trashing is destructive and needs its own confirmation: keep it out of a writes tool.
        raise Broker365Error("Trash and Spam are not label changes here: use gmail_trash")
    body = {"addLabelIds": ids[: len(to_add)], "removeLabelIds": ids[len(to_add) :]}
    m = _g().post(_msg_url(message_id, "/modify"), json=body)
    return {"id": message_id, "labels": (m or {}).get("labelIds")}


@tool(BUNDLE, GOOGLE, destructive=True)
def gmail_trash(message_id: str) -> dict[str, Any]:
    """Move a Gmail message to the Trash (Gmail empties it after 30 days; broker365 never
    deletes permanently)."""
    m = _g().post(_msg_url(message_id, "/trash"))
    return {"trashed": True, "id": message_id, "labels": (m or {}).get("labelIds")}
