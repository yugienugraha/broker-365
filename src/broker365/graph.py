"""Thin httpx wrapper over Microsoft Graph v1.0.

Bearer auth from broker365.auth, @odata.nextLink paging, Retry-After handling, and large-file
upload sessions. Absolute URLs are only followed when they point at graph.microsoft.com;
pre-authenticated upload/download URLs are fetched without the Authorization header so the
token never leaves for another host. Transport failures surface as NetworkError.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from broker365 import auth, config
from broker365.errors import Broker365Error, GraphError, NetworkError

MAX_RETRIES = 4
RETRY_STATUSES = {429, 503, 504}
IDEMPOTENT = {"GET", "PUT", "DELETE", "HEAD"}
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
UPLOAD_CHUNK = 320 * 1024 * 32  # 10 MiB; must be a multiple of 320 KiB
PAGE_MAX = 100
# 423 = resource locked (open in Excel, or a Workbook API write moments ago: measured ~20 s
# in Oct 2026). The request was not processed, so any method may be retried while it lasts.
LOCK_WAIT, LOCK_RETRY = 60.0, 5.0
GRAPH_HOST = urlsplit(config.GRAPH_BASE).netloc

_sleep = time.sleep  # patched in tests
_http: httpx.Client | None = None


def http() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False)
    return _http


def item_ref(item: str, drive: str = "/me/drive") -> str:
    """Drive item reference from an id or an absolute path ('/Docs/a.xlsx') inside `drive`
    (the user's OneDrive by default; '/drives/{id}' or '/sites/{id}/drive' for SharePoint)."""
    if not item:
        raise Broker365Error("A file id or path is required")
    if item.startswith("/"):
        path = item.rstrip("/")
        return f"{drive}/root" if not path else f"{drive}/root:{quote(path)}:"
    return f"{drive}/items/{quote(item, safe='!')}"


def seg(value: str, what: str = "id") -> str:
    """One URL path segment (message/event/task id, …), validated and fully quoted."""
    if not value:
        raise Broker365Error(f"A {what} is required")
    return quote(value, safe="")


def odata_str(value: str) -> str:
    """Escape a value for use inside an OData single-quoted string literal."""
    return value.replace("'", "''")


def _send(
    method: str, url: str, headers: dict[str, str], *, idempotent: bool = False, **kw: Any
) -> httpx.Response:
    """Send with retries. Non-idempotent requests are only retried on 429 (not processed);
    a 503/504 on POST may already have taken effect (e.g. sendMail) unless the caller marks
    it `idempotent` (e.g. create-with-conflictBehavior=fail)."""
    retry_on = RETRY_STATUSES if idempotent or method in IDEMPOTENT else {429}
    attempt, lock_deadline = 0, time.monotonic() + LOCK_WAIT
    while True:
        try:
            resp = http().request(method, url, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise NetworkError(f"Network error talking to Microsoft: {type(exc).__name__}") from exc
        if resp.status_code == 423 and time.monotonic() < lock_deadline:
            _sleep(LOCK_RETRY)
            continue
        if resp.status_code not in retry_on or attempt == MAX_RETRIES:
            break
        try:
            delay = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            delay = 2.0**attempt
        _sleep(min(delay, 60.0))
        attempt += 1
    if resp.is_error:
        raise GraphError.from_response(resp)
    return resp


def fetch(url: str) -> bytes:
    """GET a pre-authenticated URL (download link) without sending the bearer token."""
    resp = _send("GET", url, {})
    if resp.is_redirect:
        resp = _send("GET", resp.headers["Location"], {})
    return resp.content


class GraphClient:
    def __init__(self, bundle: str):
        self.bundle = bundle

    @staticmethod
    def _url(path: str) -> str:
        if path.startswith("https://"):
            if urlsplit(path).netloc != GRAPH_HOST:
                raise Broker365Error("Refusing to send Graph credentials to a non-Graph host")
            return path
        return config.GRAPH_BASE + path

    def request(
        self, method: str, path: str, *, headers: dict[str, str] | None = None, **kw: Any
    ) -> httpx.Response:
        hdrs = {"Authorization": f"Bearer {auth.get_token(self.bundle)}", **(headers or {})}
        return _send(method, self._url(path), hdrs, **kw)

    def _json(self, method: str, path: str, **kw: Any) -> Any:
        resp = self.request(method, path, **kw)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def get(self, path: str, **kw: Any) -> Any:
        return self._json("GET", path, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> Any:
        return self._json("POST", path, json=json, **kw)

    def patch(self, path: str, json: Any = None, **kw: Any) -> Any:
        return self._json("PATCH", path, json=json, **kw)

    def delete(self, path: str, **kw: Any) -> None:
        self.request("DELETE", path, **kw)

    def paged(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        limit: int = 100,
        headers: dict[str, str] | None = None,
    ) -> list:
        return list(self.iter_pages(path, params, limit, headers))

    def iter_pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        limit: int = 100,
        headers: dict[str, str] | None = None,
    ) -> Iterator:
        """Yield up to `limit` items across @odata.nextLink pages ($top defaults to limit)."""
        count, url = 0, path
        p: dict[str, Any] | None = {"$top": min(limit, PAGE_MAX), **(params or {})}
        while url and count < limit:
            page = self.get(url, params=p, headers=headers)
            for value in page.get("value", []):
                yield value
                count += 1
                if count >= limit:
                    return
            url, p = page.get("@odata.nextLink"), None

    # ---- file content -----------------------------------------------------------------

    def download(
        self, item: str, params: dict[str, Any] | None = None, drive: str = "/me/drive"
    ) -> bytes:
        """Return file bytes. Graph answers /content with a 302 to a pre-authenticated URL,
        which is followed without the bearer token."""
        resp = self.request("GET", f"{item_ref(item, drive)}/content", params=params)
        if resp.is_redirect:
            return fetch(resp.headers["Location"])
        return resp.content

    def upload(
        self,
        data: bytes,
        *,
        remote_path: str | None = None,
        item_id: str | None = None,
        conflict: str = "replace",
        if_match: str | None = None,
        drive: str = "/me/drive",
    ) -> dict[str, Any]:
        """Upload bytes to a new path or over an existing item (optionally eTag-guarded);
        files over 4 MB go through an upload session."""
        if bool(remote_path) == bool(item_id):
            raise Broker365Error("Pass exactly one of remote_path or item_id")
        target = item_ref(remote_path or item_id or "", drive)
        headers = {"If-Match": if_match} if if_match else {}
        if len(data) <= SIMPLE_UPLOAD_MAX:
            params = {"@microsoft.graph.conflictBehavior": conflict} if remote_path else None
            resp = self.request(
                "PUT", f"{target}/content", content=data, headers=headers, params=params
            )
            return resp.json()
        session = self.post(
            f"{target}/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": conflict}},
            headers=headers,
        )
        upload_url, size = session["uploadUrl"], len(data)
        resp = None
        for start in range(0, size, UPLOAD_CHUNK):
            chunk = data[start : start + UPLOAD_CHUNK]
            end = start + len(chunk) - 1
            resp = _send(
                "PUT", upload_url, {"Content-Range": f"bytes {start}-{end}/{size}"}, content=chunk
            )
        assert resp is not None
        return resp.json()


def client(bundle: str) -> GraphClient:
    return GraphClient(bundle)
