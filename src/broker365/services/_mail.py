"""Mail policy shared by Outlook (mail.py) and Gmail (gmail.py): required recipients, the
attachment size limit and the draft-first result (tools draft unless confirm=true; the
approved draft is sent with the provider's *_send_draft tool)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from broker365.errors import Broker365Error
from broker365.services import files
from broker365.services._shapes import validate_emails

ATTACHMENT_MAX = 3 * 1024 * 1024  # per attached file


def required_emails(addrs: list[str] | None, what: str = "recipient") -> list[str]:
    """Validated addresses; at least one."""
    out = validate_emails(addrs)
    if not out:
        raise Broker365Error(f"At least one {what} is required")
    return out


def read_attachment(path: str, upload_hint: str) -> Path:
    """A local file to attach: inside the sandbox and at most ATTACHMENT_MAX bytes, else an
    error that suggests `upload_hint` (share a link to the file instead)."""
    src = files.read_local(path)
    if src.stat().st_size > ATTACHMENT_MAX:
        raise Broker365Error(f"{src.name} is larger than 3 MB. {upload_hint}")
    return src


def draft_note(send_tool: str, discard: str | None = None) -> str:
    extra = f" ({discard})" if discard else ""
    return (
        "Draft created, not sent. Show it to the user; once they approve, call "
        f"{send_tool} with this draft_id{extra}."
    )


def draft_result(
    draft_id: str | None, web_link: str | None, send_tool: str, discard: str | None = None
) -> dict[str, Any]:
    """What a send/reply returns when it only saved a draft."""
    return {
        "sent": False,
        "draft_id": draft_id,
        "web_link": web_link,
        "note": draft_note(send_tool, discard),
    }
