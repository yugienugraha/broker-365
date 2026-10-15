"""Helpers shared by the Google Sheets, Docs and Slides tools: one `Native` descriptor per
product (client, URLs, finding a file by id or path, revision-guarded batchUpdate,
replaceAllText), consent checks and small response helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from broker365 import config, gapi, providers
from broker365.errors import Broker365Error, GoogleApiError, GoogleConflictError
from broker365.services import gdrive
from broker365.transport import seg


def has_consent(bundle: str) -> bool:
    return providers.get(config.GOOGLE).has_consent(bundle)


def new_id(prefix: str) -> str:
    """An object id for Slides/Docs requests (5-50 chars of [A-Za-z0-9_-])."""
    return f"b365_{prefix}_{uuid.uuid4().hex[:16]}"


def reply(replies: list[dict[str, Any]], i: int, kind: str) -> dict[str, Any]:
    """The `kind` part of batchUpdate reply `i` ({} when missing)."""
    return ((replies[i] if i < len(replies) else None) or {}).get(kind) or {}


def run_text(elements: list[dict[str, Any]] | None) -> str:
    """The text of Docs paragraph elements / Slides text elements (their textRuns)."""
    return "".join((e.get("textRun") or {}).get("content", "") for e in elements or [])


class _Client(gapi.GoogleClient):
    """A product API client: a 404 (or "operation not supported" 400) on the product's own
    URLs means the id is missing or a different file type, which is explained with the hint."""

    def __init__(self, native: Native):
        super().__init__(native.bundle)
        self.native = native

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            return super().request(method, url, **kw)
        except GoogleApiError as e:
            wrong = e.status == 404 or (
                e.status == 400 and "not supported for this document" in e.message.lower()
            )
            if not wrong or not url.startswith(self.native.base_url):
                raise
            raise Broker365Error(
                f"Not found, or not a {self.native.what} (check the id). {self.native.hint}"
            ) from None


@dataclass(frozen=True)
class Native:
    bundle: str  # Google scope bundle, e.g. "gsheets"
    base_url: str  # e.g. https://sheets.googleapis.com/v4/spreadsheets
    mime: str  # Drive mimeType of the native file
    what: str  # "Google Sheet"
    office_tools: str  # where the Microsoft equivalent lives, for the hint

    @property
    def hint(self) -> str:
        return f"Convert it first with gdrive_upload(convert=true), or use {self.office_tools}."

    def client(self) -> gapi.GoogleClient:
        return _Client(self)

    def url(self, file_id: str, suffix: str = "") -> str:
        return f"{self.base_url}/{seg(file_id, 'file id')}{suffix}"

    def resolve(
        self, item: str, verify: bool = False, fields: str = gdrive.PROBE_FIELDS
    ) -> dict[str, Any]:
        """{"id": …} of a file by id or absolute path. A path (or an id with `verify`) is
        looked up in Drive (`fields` for a path) and must be this native type; a bare id goes
        straight to the product API, which rejects other types (see _Client)."""
        if not item:
            raise Broker365Error("A file id or absolute path is required")
        if not item.startswith("/") and not verify:
            return {"id": item}
        meta = gdrive.resolve(item, fields)
        if meta.get("mimeType") != self.mime:
            raise Broker365Error(
                f"'{meta.get('name')}' is not a {self.what} ({meta.get('mimeType')}). {self.hint}"
            )
        return meta

    def update(
        self, file_id: str, requests: list[dict[str, Any]], revision_id: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """batchUpdate → (replies, new revision id). With `revision_id` nothing is written if
        the file changed since: GoogleConflictError (a ConflictError) instead."""
        body: dict[str, Any] = {"requests": requests}
        if revision_id:
            body["writeControl"] = {"requiredRevisionId": revision_id}
        url = self.url(file_id, ":batchUpdate")
        try:
            out = self.client().post(url, json=body, guarded=bool(revision_id)) or {}
        except GoogleConflictError as e:
            raise GoogleConflictError(
                e.status,
                e.code,
                f"the {self.what} changed since revision {revision_id}; nothing was changed. "
                "Read it again and retry.",
            ) from None
        return out.get("replies") or [], (out.get("writeControl") or {}).get("requiredRevisionId")

    def replace_all(
        self,
        file_id: str,
        pairs: list[tuple[str, str]],
        match_case: bool = True,
        revision_id: str | None = None,
    ) -> tuple[list[int], str | None]:
        """replaceAllText for each (find, replace) in one batchUpdate → (occurrences changed
        per pair, new revision id)."""
        requests = [
            {
                "replaceAllText": {
                    "containsText": {"text": find, "matchCase": match_case},
                    "replaceText": replace,
                }
            }
            for find, replace in pairs
        ]
        replies, revision = self.update(file_id, requests, revision_id)
        counts = [
            int(reply(replies, i, "replaceAllText").get("occurrencesChanged") or 0)
            for i in range(len(pairs))
        ]
        return counts, revision
