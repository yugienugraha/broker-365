"""Provider-neutral HTTP plumbing shared by graph.py (Microsoft) and gapi.py (Google).

One pooled httpx client that never follows redirects (so a bearer token can't ride along to
another host), the sleep hook tests patch, Retry-After parsing, retry limits, URL path segment
quoting and the JSON verb helpers. Each provider keeps its own retry loop and host checks.
"""

from __future__ import annotations

import math
import ssl
import time
from typing import Any
from urllib.parse import quote

import httpx

from broker365.errors import Broker365Error

IDEMPOTENT = {"GET", "PUT", "DELETE", "HEAD"}
MAX_RETRIES = 4
NETWORK_RETRIES = 2
MAX_TOTAL_WAIT = 120.0  # cap on time spent retrying one request across all tiers
SAFE = {"GET", "HEAD"}
# The request never reached the server, so resending is safe for any method.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Transient failures after sending: timeouts, resets, a connection dropped mid-response.
_TRANSIENT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def network_retryable(exc: httpx.HTTPError, method: str, idempotent: bool = False) -> bool:
    """Whether a transport failure may be retried: when the request was never sent (any
    method), or for a transient failure of a safe read or a caller-marked idempotent request.
    PUT/DELETE are not retried by default: an upload with conflict rules or If-Match, or an
    upload-session chunk, may already have taken effect. TLS failures are never retried."""
    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, ssl.SSLError):
        return False
    if isinstance(exc, _NOT_SENT):
        return True
    return isinstance(exc, _TRANSIENT) and (idempotent or method in SAFE)


class NetworkRetries:
    """Retry budget for transport failures of one request (shared by graph and gapi)."""

    def __init__(self, start: float, enabled: bool = True) -> None:
        self.start, self.left = start, NETWORK_RETRIES if enabled else 0

    def retry(self, exc: httpx.HTTPError, method: str, idempotent: bool = False) -> bool:
        """Sleep and return True if the failed request should be sent again."""
        if (
            not self.left
            or time.monotonic() - self.start > MAX_TOTAL_WAIT
            or not network_retryable(exc, method, idempotent)
        ):
            return False
        attempt = NETWORK_RETRIES - self.left  # 0-based, as retry_delay expects
        self.left -= 1
        _sleep(retry_delay(None, attempt))
        return True


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
    # quote() leaves "." and ".." alone and httpx then removes them as dot segments, which
    # would turn e.g. DELETE .../events/.. into DELETE on the parent (a whole calendar).
    if not value.strip("."):
        raise Broker365Error(f"Invalid {what}: {value!r}")
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
