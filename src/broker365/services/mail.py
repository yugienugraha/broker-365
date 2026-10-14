"""Outlook mail. Sending, replying and forwarding create a draft unless confirm=true."""

from __future__ import annotations

import base64
import html
import re
from typing import Any, Literal

from broker365 import graph
from broker365.errors import Broker365Error
from broker365.services import files, tool
from broker365.services._shapes import addr, recipients

__all__ = ["html_to_text", "recipients"]

ATTACHMENT_MAX = 3 * 1024 * 1024
LIST_SELECT = "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview,webLink"
DRAFT_NOTE = (
    "Draft created, not sent. Show it to the user; once they approve, call "
    "mail_send_draft with this draft_id (or move it to deleteditems to discard)."
)
_DROP = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.I | re.S)
_BREAK = re.compile(r"<br\s*/?>|</(p|div|li|tr|h[1-6]|table|blockquote)\s*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def _g() -> graph.GraphClient:
    return graph.client("mail")


def _msg_path(message_id: str) -> str:
    return f"/me/messages/{graph.seg(message_id, 'message id')}"


def html_to_text(content: str) -> str:
    text = _BREAK.sub("\n", _DROP.sub("", content))
    text = html.unescape(_TAG.sub("", text)).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _to(addrs: list[str]) -> list[dict[str, Any]]:
    if not addrs:
        raise Broker365Error("At least one recipient is required")
    return recipients(addrs)


def _summarize(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": m.get("id"),
        "subject": m.get("subject"),
        "from": addr(m.get("from")),
        "received": m.get("receivedDateTime"),
        "is_read": m.get("isRead"),
        "has_attachments": m.get("hasAttachments"),
        "preview": m.get("bodyPreview"),
        "web_link": m.get("webLink"),
    }


def _draft_result(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "sent": False,
        "draft_id": d.get("id"),
        "web_link": d.get("webLink"),
        "note": DRAFT_NOTE,
    }


def _file_attachments(paths: list[str] | None) -> list[dict[str, Any]]:
    out = []
    for p in paths or []:
        src = files.read_local(p)
        if src.stat().st_size > ATTACHMENT_MAX:
            raise Broker365Error(
                f"{src.name} is larger than 3 MB. Upload it to OneDrive (files_upload) and "
                "send a link from files_share instead."
            )
        out.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": src.name,
                "contentBytes": base64.b64encode(src.read_bytes()).decode("ascii"),
            }
        )
    return out


def _message(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None,
    body_type: str,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "HTML" if body_type == "html" else "Text", "content": body},
        "toRecipients": _to(to),
    }
    if cc:
        msg["ccRecipients"] = recipients(cc)
    if attachments:
        msg["attachments"] = _file_attachments(attachments)
    return msg


def _save_draft(msg: dict[str, Any]) -> dict[str, Any]:
    return _draft_result(_g().post("/me/messages", json=msg))


def _respond(
    message_id: str, action: str, payload: dict[str, Any], confirm: bool, **sent: Any
) -> dict[str, Any]:
    """reply/replyAll/forward a message, or create the matching draft (createReply, …)."""
    base = _msg_path(message_id)
    if not confirm:
        create = f"create{action[0].upper()}{action[1:]}"
        return _draft_result(_g().post(f"{base}/{create}", json=payload))
    _g().post(f"{base}/{action}", json=payload)
    return {"sent": True, **sent, "message_id": message_id}


# ---- tools ----------------------------------------------------------------------------


@tool("mail")
def mail_list(folder: str = "inbox", limit: int = 25, unread_only: bool = False) -> list[dict]:
    """List messages in a mail folder, newest first. `folder` is a folder id or a well-known
    name (inbox, sentitems, drafts, archive, deleteditems, junkemail)."""
    params = {"$select": LIST_SELECT, "$orderby": "receivedDateTime desc"}
    if unread_only:
        # Graph requires $orderby properties to appear first in $filter (InefficientFilter)
        params["$filter"] = "receivedDateTime ge 1900-01-01T00:00:00Z and isRead eq false"
    path = f"/me/mailFolders/{graph.seg(folder, 'folder')}/messages"
    return [_summarize(m) for m in _g().paged(path, params=params, limit=limit)]


@tool("mail")
def mail_search(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search all mail (subject, body, sender, …) with Outlook search syntax, e.g.
    'invoice from:alice@contoso.com'. Results are ranked by relevance, not date."""
    q = query.replace("\\", "").replace('"', '\\"')
    params = {"$search": f'"{q}"', "$select": LIST_SELECT}
    return [_summarize(m) for m in _g().paged("/me/messages", params=params, limit=limit)]


@tool("mail")
def mail_read(message_id: str, max_chars: int = 50_000) -> dict[str, Any]:
    """Read one message: headers, recipients, plain-text body (HTML is converted) and the
    attachment list (name, size, id)."""
    params = {"$expand": "attachments($select=id,name,size,contentType)"}
    m = _g().get(_msg_path(message_id), params=params)
    body = m.get("body") or {}
    text = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        text = html_to_text(text)
    return {
        **_summarize(m),
        "to": [addr(r) for r in m.get("toRecipients", [])],
        "cc": [addr(r) for r in m.get("ccRecipients", [])],
        "conversation_id": m.get("conversationId"),
        "truncated": len(text) > max_chars,
        "body": text[:max_chars],
        "attachments": [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "size": a.get("size"),
                "content_type": a.get("contentType"),
            }
            for a in m.get("attachments", [])
        ],
    }


@tool("mail", writes=True)
def mail_draft(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    body_type: Literal["text", "html"] = "text",
) -> dict[str, Any]:
    """Save a new message in Drafts without sending it. Returns the draft id and web link."""
    return _save_draft(_message(to, subject, body, cc, body_type))


@tool("mail", destructive=True)
def mail_send_draft(draft_id: str) -> dict[str, Any]:
    """Send an existing draft (from mail_draft / mail_send / mail_reply / mail_forward without
    confirm). Only call this after the user approved the draft's content."""
    _g().post(f"{_msg_path(draft_id)}/send")
    return {"sent": True, "draft_id": draft_id}


@tool("mail", destructive=True)
def mail_send(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    body_type: Literal["text", "html"] = "text",
    attachments: list[str] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Send an email. Without confirm=true it only saves a draft for review (then send that
    draft with mail_send_draft). `attachments` are local workspace paths, max 3 MB each."""
    msg = _message(to, subject, body, cc, body_type, attachments)
    if not confirm:
        return _save_draft(msg)
    _g().post("/me/sendMail", json={"message": msg, "saveToSentItems": True})
    return {"sent": True, "to": to, "subject": subject}


@tool("mail", destructive=True)
def mail_reply(
    message_id: str, body: str, reply_all: bool = False, confirm: bool = False
) -> dict[str, Any]:
    """Reply (or reply-all) to a message. Without confirm=true it only creates a reply draft."""
    action = "replyAll" if reply_all else "reply"
    return _respond(message_id, action, {"comment": body}, confirm, reply_all=reply_all)


@tool("mail", destructive=True)
def mail_forward(
    message_id: str, to: list[str], comment: str = "", confirm: bool = False
) -> dict[str, Any]:
    """Forward a message to new recipients with an optional comment. Without confirm=true it
    only creates a forward draft."""
    payload = {"comment": comment, "toRecipients": _to(to)}
    return _respond(message_id, "forward", payload, confirm, to=to)


@tool("mail", writes=True)
def mail_move(message_id: str, destination_folder: str) -> dict[str, Any]:
    """Move a message to another folder (folder id or well-known name such as archive,
    deleteditems, junkemail, inbox). Returns the message's new id."""
    m = _g().post(f"{_msg_path(message_id)}/move", json={"destinationId": destination_folder})
    return {"moved": True, "id": (m or {}).get("id"), "folder": destination_folder}


@tool("mail", writes=True)
def mail_mark_read(message_id: str, is_read: bool = True) -> dict[str, Any]:
    """Mark a message as read (or unread with is_read=false)."""
    _g().patch(_msg_path(message_id), json={"isRead": is_read})
    return {"id": message_id, "is_read": is_read}


@tool("mail")
def mail_folders(limit: int = 100) -> list[dict[str, Any]]:
    """List top-level mail folders with unread and total item counts."""
    params = {"$select": "id,displayName,unreadItemCount,totalItemCount"}
    return [
        {
            "id": f.get("id"),
            "name": f.get("displayName"),
            "unread": f.get("unreadItemCount"),
            "total": f.get("totalItemCount"),
        }
        for f in _g().paged("/me/mailFolders", params=params, limit=limit)
    ]
