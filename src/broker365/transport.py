"""Provider-neutral HTTP plumbing shared by graph.py (Microsoft) and gapi.py (Google).

One pooled httpx client that never follows redirects (so a bearer token can't ride along to
another host), the sleep hook tests patch, Retry-After parsing, retry limits, URL path segment
quoting and the JSON verb helpers. Each provider keeps its own retry loop and host checks.
"""

from __future__ import annotations

import math
import time
from typing import Any
from urllib.parse import quote

import httpx

from broker365.errors import Broker365Error

IDEMPOTENT = {"GET", "PUT", "DELETE", "HEAD"}
MAX_RETRIES = 4
MAX_TOTAL_WAIT = 120.0  # cap on time spent retrying one request across all tiers

_sleep = time.sleep  # patched in tests
_http: httpx.Client | None = None


def http() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False)
    return _http


def retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    """Seconds to wait before retry number `attempt` (0-based): Retry-After (seconds) when the
    server sent a usable one, else (or without a response) exponential backoff; never more
    than a minute."""
    try:
        delay = float(resp.headers.get("Retry-After", "") if resp is not None else "")
    except ValueError:
        delay = math.nan
    if math.isnan(delay):
        delay = 2.0**attempt
    return min(max(delay, 0.0), 60.0)


def seg(value: str | None, what: str = "id") -> str:
    """One URL path segment (message/event/file id, …), validated and fully quoted."""
    if not value:
        raise Broker365Error(f"A {what} is required")
    return quote(value, safe="")


class JsonVerbs:
    """get/post/patch/put over the subclass's `request(method, url, **kw) -> httpx.Response`,
    returning the decoded JSON body (None for 204 / empty bodies)."""

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        raise NotImplementedError

    def _json(self, method: str, url: str, **kw: Any) -> Any:
        resp = self.request(method, url, **kw)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def get(self, url: str, **kw: Any) -> Any:
        return self._json("GET", url, **kw)

    def post(self, url: str, json: Any = None, **kw: Any) -> Any:
        return self._json("POST", url, json=json, **kw)

    def patch(self, url: str, json: Any = None, **kw: Any) -> Any:
        return self._json("PATCH", url, json=json, **kw)

    def put(self, url: str, json: Any = None, **kw: Any) -> Any:
        return self._json("PUT", url, json=json, **kw)
