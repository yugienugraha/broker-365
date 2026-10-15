"""Thin httpx wrapper over Google's REST APIs (Drive v3 now; Sheets/Docs/Slides/Gmail/People/
Tasks later), modelled on graph.py and sharing its transport (pooled client, sleep hook,
Retry-After parsing, retry limits).

The bearer token (from the Google provider) is only ever sent over https to an exact allow-list
of Google API hosts; any other URL is refused before a request is made. Redirects are never
followed with the token: download redirects are fetched without it (https only), like
graph.fetch. Transport failures surface as NetworkError. Nothing here logs tokens or content.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urljoin, urlsplit

import httpx

from broker365 import config, providers, transport
from broker365.errors import (
    Broker365Error,
    ConsentRequired,
    GoogleApiError,
    GoogleConflictError,
    NetworkError,
    ReauthRequired,
)
from broker365.transport import IDEMPOTENT, MAX_RETRIES, MAX_TOTAL_WAIT

# Exact hosts the token may be sent to (no wildcard: lookalikes must not match).
API_HOSTS = frozenset(
    {
        "www.googleapis.com",
        "sheets.googleapis.com",
        "docs.googleapis.com",
        "slides.googleapis.com",
        "gmail.googleapis.com",
        "people.googleapis.com",
        "tasks.googleapis.com",
    }
)
BASE = "https://www.googleapis.com"
DRIVE = f"{BASE}/drive/v3"
DRIVE_UPLOAD = f"{BASE}/upload/drive/v3/files"

SERVER_ERRORS = {500, 502, 503, 504}
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
SCOPE_REASONS = {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}
# A guarded write (If-Match / requiredRevisionId) that lost the race: a 412, or a 400 with
# this message. Seen live (Docs, Oct 2026): a stale writeControl.requiredRevisionId comes back
# as HTTP 400, status INVALID_ARGUMENT, "The required revision ID '<id>' does not match the
# latest revision." Other FAILED_PRECONDITION/ABORTED answers are not conflicts.
_STALE_REVISION = re.compile(r"required revision id .* does not match", re.IGNORECASE)
MAX_REDIRECTS = 5
PAGE_MAX = 1000  # Drive's largest pageSize
# Page-size parameter and its largest value per API (longest matching URL prefix wins).
PAGE_PARAMS = {
    f"{BASE}/drive/": ("pageSize", 1000),
    f"{BASE}/calendar/": ("maxResults", 2500),
    "https://gmail.googleapis.com/": ("maxResults", 500),
    "https://tasks.googleapis.com/": ("maxResults", 100),
    "https://people.googleapis.com/": ("pageSize", 1000),
}
MULTIPART_MAX = 5 * 1024 * 1024  # larger uploads go through a resumable session
UPLOAD_CHUNK = 256 * 1024 * 32  # 8 MiB; must be a multiple of 256 KiB
DOWNLOAD_MAX = 1024 * 1024 * 1024  # default cap on the bytes of one download
LOGIN_HINT = "Run: broker365 login --provider google"


def check_url(url: str) -> str:
    """`url` if it is https on an allow-listed Google API host; paths starting with '/' are
    taken relative to www.googleapis.com. Anything else raises before a request is sent."""
    if url.startswith("/"):
        url = BASE + url
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.lower() not in API_HOSTS:
        raise Broker365Error("Refusing to send Google credentials to a non-Google-API URL")
    return url


def _error_info(resp: httpx.Response) -> tuple[str, set[str], str, str]:
    """(code, reasons, message, status) from a Google error body. `reasons` holds every
    error.errors[].reason and error.details[].reason (or an OAuth-style string error) for
    membership checks; `code` is the first errors[] reason, else the first details[] reason,
    else error.status."""
    ordered: list[str] = []
    status, message = "", resp.reason_phrase or "error"
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        err = None
    if isinstance(err, dict):
        status = str(err.get("status") or "")
        message = str(err.get("message") or message)
        for key in ("errors", "details"):
            for entry in err.get(key) or []:
                if isinstance(entry, dict) and isinstance(entry.get("reason"), str):
                    ordered.append(entry["reason"])
    elif isinstance(err, str):  # OAuth-style {"error": "invalid_token", ...}
        ordered.append(err)
    code = ordered[0] if ordered else (status or "error")
    return code, set(ordered), (message.splitlines() or ["error"])[0][:500], status


def _token_rejected(resp: httpx.Response) -> bool:
    """401, or a 403 for a missing scope: either may come from a stale memoized token (e.g.
    minted before another process added the scope), so a fresh token is worth one retry."""
    if resp.status_code == 401:
        return True
    return resp.status_code == 403 and bool(_error_info(resp)[1] & SCOPE_REASONS)


def api_error(
    resp: httpx.Response, bundle: str | None = None, guarded: bool = False
) -> Broker365Error:
    """The Broker365Error for an error answer. `bundle` turns a missing-scope 403 into
    ConsentRequired for that bundle; `guarded` (the request carried a revision/eTag
    precondition) turns a failed precondition into GoogleConflictError."""
    code, reasons, message, _ = _error_info(resp)
    status = resp.status_code
    if guarded and (status == 412 or (status == 400 and _STALE_REVISION.search(message))):
        return GoogleConflictError(status, code, message)
    if status == 401:
        return ReauthRequired(f"Google rejected the sign-in for this account. {LOGIN_HINT}")
    if status == 403 and bundle and reasons & SCOPE_REASONS:
        return ConsentRequired(bundle, config.GOOGLE)
    if status == 404:
        message = f"not found or no access ({message})"
    return GoogleApiError(status, code, message)


def _retryable(resp: httpx.Response, method: str, idempotent: bool, server_retries: bool) -> bool:
    """429 and rate-limit 403s were not processed, so any method may retry; 5xx only for
    idempotent requests (a 5xx on a create may already have taken effect) and only when the
    caller doesn't handle 5xx itself (`server_retries=False`, resumable upload chunks)."""
    code = resp.status_code
    if code == 429:
        return True
    if code == 403:
        return bool(_error_info(resp)[1] & RATE_REASONS)
    return server_retries and code in SERVER_ERRORS and (idempotent or method in IDEMPOTENT)


def _send(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    idempotent: bool = False,
    stream: bool = False,
    server_retries: bool = True,
    **kw: Any,
) -> httpx.Response:
    """Send with retries (see _retryable), honouring Retry-After with exponential backoff and
    no new attempt after MAX_TOTAL_WAIT seconds. With `stream`, a successful body is left
    unread for the caller (who must close the response); error bodies are always read."""
    client = transport.http()
    start, attempt = time.monotonic(), 0
    net = transport.NetworkRetries(start, enabled=server_retries)
    while True:
        try:
            req = client.build_request(method, url, headers=headers, **kw)
            resp = client.send(req, stream=stream)
            if stream and resp.is_error:
                resp.read()
        except httpx.HTTPError as exc:
            if net.retry(exc, method, idempotent):  # seen live: occasional ReadTimeouts
                continue
            raise NetworkError(f"Network error talking to Google: {type(exc).__name__}") from exc
        if (
            attempt == MAX_RETRIES
            or time.monotonic() - start > MAX_TOTAL_WAIT
            or not _retryable(resp, method, idempotent, server_retries)
        ):
            return resp
        resp.close()
        transport._sleep(transport.retry_delay(resp, attempt))
        attempt += 1


def _drain(
    resp: httpx.Response, write: Callable[[bytes], Any], max_bytes: int, truncate: bool
) -> bool:
    """Pass a streamed body to `write`, at most max_bytes of it; returns whether it was cut
    short. Over the cap without `truncate` raises instead."""
    total = 0
    try:
        for chunk in resp.iter_bytes():
            room = max_bytes - total
            if len(chunk) > room:
                if not truncate:
                    raise Broker365Error(
                        f"The file is larger than {max_bytes} bytes, the limit for this call"
                    )
                write(chunk[:room])
                return True
            write(chunk)
            total += len(chunk)
    except httpx.HTTPError as exc:
        raise NetworkError(f"Network error talking to Google: {type(exc).__name__}") from exc
    finally:
        resp.close()
    return False


def fetch_stream(url: str) -> httpx.Response:
    """A streaming GET of a download link without the bearer token, following https-only
    redirects. The caller reads and closes the response."""
    for _ in range(MAX_REDIRECTS + 1):
        if urlsplit(url).scheme != "https":
            raise Broker365Error("Refusing to download from a non-https link")
        resp = _send("GET", url, {}, stream=True)
        if resp.is_redirect:
            resp.close()
            url = urljoin(url, resp.headers.get("Location", ""))
            continue
        if resp.is_error:
            raise api_error(resp)
        return resp
    raise Broker365Error("Too many redirects while downloading")


def _multipart(metadata: dict[str, Any], data: bytes, mime_type: str) -> tuple[bytes, str]:
    """A multipart/related body (JSON metadata part, then the media part)."""
    boundary = f"broker365-{uuid.uuid4().hex}"
    head = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n"
    ).encode()
    return (
        head + data + f"\r\n--{boundary}--\r\n".encode(),
        f"multipart/related; boundary={boundary}",
    )


class GoogleClient(transport.JsonVerbs):
    def __init__(self, bundle: str, username: str | None = None):
        self.bundle, self.username = bundle, username

    def _token(self) -> str:
        return providers.get(config.GOOGLE).get_token(self.bundle, self.username)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        idempotent: bool = False,
        stream: bool = False,
        server_retries: bool = True,
        guarded: bool = False,
        **kw: Any,
    ) -> httpx.Response:
        """One API call. A 401 or missing-scope 403 drops the memoized access token and
        retries once with a fresh one (the request was not processed); errors become
        Broker365Error subclasses. `guarded`: the request carries a precondition (revision id),
        so a failed one is a GoogleConflictError. 3xx answers are returned as they are (never
        followed with the token)."""
        url = check_url(url)
        for attempt in range(2):
            hdrs = {**(headers or {}), "Authorization": f"Bearer {self._token()}"}
            resp = _send(
                method,
                url,
                hdrs,
                idempotent=idempotent,
                stream=stream,
                server_retries=server_retries,
                **kw,
            )
            if attempt or not _token_rejected(resp):
                break
            resp.close()
            providers.get(config.GOOGLE).invalidate_token(self.username)
        if resp.is_error:
            raise api_error(resp, self.bundle, guarded)
        return resp

    def paged(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        items_key: str = "files",
        limit: int = 100,
        flags: dict[str, bool] | None = None,
        size_param: str | None = None,
        page_max: int | None = None,
    ) -> list[Any]:
        """Up to `limit` items of `items_key` across nextPageToken pages. Each key of `flags`
        (e.g. "incompleteSearch") is set to True when any page had it true. The page-size
        parameter and its maximum come from PAGE_PARAMS for the URL unless given."""
        match = max((p for p in PAGE_PARAMS if url.startswith(p)), key=len, default=None)
        default_param, default_max = PAGE_PARAMS[match] if match else ("pageSize", PAGE_MAX)
        size_param, page_max = size_param or default_param, page_max or default_max
        out: list[Any] = []
        token = None
        while len(out) < limit:
            p = {**(params or {}), size_param: min(limit - len(out), page_max)}
            if token:
                p["pageToken"] = token
            page = self.get(url, params=p) or {}
            out.extend((page.get(items_key) or [])[: limit - len(out)])
            for key in flags or {}:
                flags[key] = bool(flags[key] or page.get(key))
            token = page.get("nextPageToken")
            if not token:
                break
        return out

    # ---- file content -----------------------------------------------------------------

    def _stream(self, url: str, params: dict[str, Any] | None) -> httpx.Response:
        resp = self.request("GET", url, params=params, stream=True)
        if resp.is_redirect:
            resp.close()
            return fetch_stream(urljoin(url, resp.headers.get("Location", "")))
        return resp

    def download(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        max_bytes: int | None = None,
        *,
        truncate: bool = False,
    ) -> tuple[bytes, bool]:
        """A media/export URL's content in memory: (content, truncated). A redirect is
        followed without the token. Over `max_bytes`: truncated when `truncate`, else an
        error."""
        buf = bytearray()
        cap = DOWNLOAD_MAX if max_bytes is None else max_bytes
        truncated = _drain(self._stream(url, params), buf.extend, cap, truncate)
        return bytes(buf), truncated

    def download_to(
        self,
        url: str,
        out: BinaryIO,
        params: dict[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> None:
        """Stream a media/export URL into the open binary file `out` (error over max_bytes)."""
        cap = DOWNLOAD_MAX if max_bytes is None else max_bytes
        _drain(self._stream(url, params), out.write, cap, truncate=False)

    def upload(
        self,
        source: bytes | Path,
        metadata: dict[str, Any],
        mime_type: str,
        *,
        file_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a Drive file (or replace the content of `file_id`) with `metadata` from
        bytes or a local file. Up to MULTIPART_MAX bytes in one multipart request; larger
        ones in a resumable session, read and sent UPLOAD_CHUNK bytes at a time."""
        method = "PATCH" if file_id else "POST"
        url = f"{DRIVE_UPLOAD}/{transport.seg(file_id, 'file id')}" if file_id else DRIVE_UPLOAD
        # A content update is safe to repeat; a create is not (it could make a duplicate).
        idem = bool(file_id)
        size = source.stat().st_size if isinstance(source, Path) else len(source)
        if size <= MULTIPART_MAX:
            data = source.read_bytes() if isinstance(source, Path) else source
            body, ctype = _multipart(metadata, data, mime_type)
            return self._json(
                method,
                url,
                params={**(params or {}), "uploadType": "multipart"},
                content=body,
                headers={"Content-Type": ctype},
                idempotent=idem,
            )
        start = self.request(
            method,
            url,
            params={**(params or {}), "uploadType": "resumable"},
            json=metadata,
            headers={"X-Upload-Content-Type": mime_type, "X-Upload-Content-Length": str(size)},
            idempotent=idem,
        )
        session = start.headers.get("Location")
        if not session:
            raise Broker365Error("Google did not return an upload session")
        check_url(session)  # the token goes there too
        if not isinstance(source, Path):
            return self._resumable(session, size, lambda o: source[o : o + UPLOAD_CHUNK])
        with source.open("rb") as f:

            def read_at(offset: int) -> bytes:
                f.seek(offset)
                return f.read(UPLOAD_CHUNK)

            return self._resumable(session, size, read_at)

    def _resumable(
        self, session: str, size: int, read_at: Callable[[int], bytes]
    ) -> dict[str, Any]:
        """Send the content in chunks; each 308's Range says how much Google has. A chunk that
        fails (5xx or network) is not resent blindly: the session's status is queried
        (empty PUT, `bytes */size`) and the upload resumes from what Google confirms."""
        offset, failures, query = 0, 0, False
        while True:
            if query:
                chunk, content_range = b"", f"bytes */{size}"
            else:
                chunk = read_at(offset)
                content_range = f"bytes {offset}-{offset + len(chunk) - 1}/{size}"
            try:
                resp = self.request(
                    "PUT",
                    session,
                    content=chunk,
                    headers={"Content-Range": content_range},
                    server_retries=False,
                )
            except (NetworkError, GoogleApiError) as exc:
                status = getattr(exc, "status", None)
                if status in (404, 410):
                    raise Broker365Error("The upload session expired; retry the upload") from None
                if isinstance(exc, GoogleApiError) and status not in SERVER_ERRORS:
                    raise
                if failures == MAX_RETRIES:
                    raise Broker365Error(
                        "The upload kept failing; nothing was saved. Retry the upload later."
                    ) from None
                transport._sleep(transport.retry_delay(None, failures))
                failures, query = failures + 1, True
                continue
            if resp.status_code != 308:  # 200/201: done
                return resp.json()
            rng = resp.headers.get("Range", "")  # "bytes=0-N"; none = nothing received
            try:
                received = int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
            except (IndexError, ValueError):
                raise Broker365Error("Google sent an unreadable upload progress") from None
            if received >= size or (not query and received <= offset):
                raise Broker365Error("The upload did not complete; try again")
            offset, query = received, False


def client(bundle: str, username: str | None = None) -> GoogleClient:
    return GoogleClient(bundle, username)
