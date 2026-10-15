"""Google accounts (Gmail and Google Workspace): OAuth 2.0 for installed apps.

Three sign-in paths share one validator (docs/GOOGLE_PLAN.md §3), all with PKCE (S256), a
random `state` of the form "<flow_id>.<secret>", `access_type=offline`, `prompt=consent` and
`include_granted_scopes=true`:

- CLI on a desktop: a one-shot loopback listener on 127.0.0.1 (`login_interactive`);
- dashboard: Google redirects to the dashboard's own `/oauth/google/callback`
  (`complete_callback`), and the page polls `login_complete` for the stored result;
- chat / headless: the user pastes back the address-bar URL of the (unreachable) loopback
  redirect (`login_complete(..., redirect_url=...)`).

Refresh tokens live in `google_tokens.bin` in the broker home, encrypted like the Microsoft
token cache (DPAPI / Keychain / libsecret, else a mode-600 file). Access tokens are only
memoized in memory. Nothing here logs or returns a token to anyone but API callers.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import http.server
import json
import logging
import math
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from msal_extensions.persistence import PersistenceNotFound
from msal_extensions.token_cache import CrossPlatLock, LockError

from broker365 import audit, auth, config
from broker365.errors import Broker365Error, ConsentRequired, NetworkError, ReauthRequired
from broker365.providers import callback, flows

log = logging.getLogger(__name__)

PROVIDER = config.GOOGLE
LABEL = "Google Workspace"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALLBACK_PATH = "/oauth/google/callback"  # the dashboard's redirect target
FLOW_SECONDS = 600  # how long a started sign-in stays valid
INTERACTIVE_TIMEOUT = 300  # seconds the CLI listener waits for the browser
READ_TIMEOUT = 5  # seconds the listener waits for a connected browser to send its request
DEFAULT_EXPIRES_IN = 3600  # access-token lifetime assumed when Google's is missing or invalid
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
STORE_FILE = "google_tokens.bin"
LOGIN_HINT = "Run: broker365 login --provider google"

# Google reports the "email"/"profile" scopes by their long names in token responses.
_SCOPE_ALIASES = {
    "email": "https://www.googleapis.com/auth/userinfo.email",
    "profile": "https://www.googleapis.com/auth/userinfo.profile",
}
_ERROR_CODE = re.compile(r"[a-z_]{1,40}")

_lock = threading.Lock()  # read-modify-write of the token store (MCP runs tools in threads)
_refresh_lock = threading.Lock()  # one refresh at a time; waiters then find the memo filled
_callback_lock = threading.Lock()  # claim of a flow by the dashboard callback
# email (lowercase) -> (access token, expiry); entries live ≤ auth.TOKEN_MEMO_SECONDS.
_tokens: dict[str, tuple[str, float]] = {}
# ((path, mtime_ns, size) of the store file, its decoded content): decrypting is the slow part.
_cache: tuple[tuple[str, int, int], dict[str, Any]] | None = None
_http: httpx.Client | None = None


def reset() -> None:
    """Forget in-memory state (access-token memo, decoded store, HTTP client), e.g. between
    tests or after the broker home changed."""
    global _cache, _http
    _tokens.clear()
    _cache = None
    _http = None


class _Denied(Broker365Error):
    """The user (or Google) ended the sign-in: the flow can't be finished any more."""


# ---- token store ------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _persistence(path: str, plaintext: bool | None):  # plaintext: part of the cache key only
    return auth._persistence(Path(path))


def _store():
    path = config.broker_home() / STORE_FILE
    return _persistence(str(path), config.env_flag("BROKER365_PLAINTEXT_CACHE"))


def _load() -> dict[str, Any]:
    """The decoded store. Kept in memory while the file's (path, mtime, size) stay the same,
    so callers must not mutate the result (`_modify` works on a copy)."""
    global _cache
    path = config.broker_home() / STORE_FILE
    try:
        st = path.stat()
        key: tuple[str, int, int] | None = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _cache is not None and _cache[0] == key:
        return _cache[1]
    data = _read()
    if key is not None:
        _cache = (key, data)
    return data


def _read() -> dict[str, Any]:
    try:
        raw = _store().load()
    except PersistenceNotFound:
        raw = None
    except Exception:  # noqa: BLE001 - backend failure (locked keychain, corrupt blob…)
        raise Broker365Error(
            f"Could not read the Google token store ({STORE_FILE}). Sign in again: "
            "broker365 login --provider google"
        ) from None
    if not raw:
        return {"accounts": {}}
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        raise Broker365Error(
            f"The Google token store ({STORE_FILE}) is unreadable. Delete it and sign in "
            "again: broker365 login --provider google"
        )
    return data


@contextlib.contextmanager
def _file_lock(path: Path):
    """The cross-process lock of the store (like msal_extensions' token cache lock)."""
    try:
        with CrossPlatLock(f"{path}.lockfile"):
            yield
    except LockError:
        raise Broker365Error(
            f"The Google token store ({STORE_FILE}) is busy (another broker365 process). Try again."
        ) from None


def _modify(transform) -> Any:
    """Read-modify-write the store under the thread and cross-process locks, re-reading it
    inside them (another process may have written); returns what `transform` returns."""
    global _cache
    path = config.broker_home() / STORE_FILE
    with _lock, _file_lock(path):
        _cache = None
        data = _read()  # fresh, so mutating it can't touch the cache
        out = transform(data)
        _store().save(json.dumps(data))
        return out


def _accounts() -> dict[str, dict[str, Any]]:
    return {k: v for k, v in _load()["accounts"].items() if isinstance(v, dict) and v.get("email")}


def _account(username: str | None = None) -> dict[str, Any]:
    """The stored record of `username`, else the default, else the only account."""
    accounts = _accounts()
    wanted = (username or config.default_account(PROVIDER) or "").strip()
    if wanted:
        if rec := accounts.get(wanted.lower()):
            return rec
        raise ReauthRequired(f"Google account {wanted} is not signed in. {LOGIN_HINT}")
    if not accounts:
        raise ReauthRequired(f"Not signed in to Google. {LOGIN_HINT}")
    if len(accounts) > 1:
        names = ", ".join(r["email"] for r in accounts.values())
        raise Broker365Error(
            f"Several Google accounts signed in ({names}). Pick one: "
            "broker365 accounts --provider google --default <email>"
        )
    return next(iter(accounts.values()))


def _describe(rec: dict[str, Any]) -> dict[str, Any]:
    """Public fields of an account; `tenant_id` is the Workspace domain (None for Gmail)."""
    hd = rec.get("hd") or None
    return {
        "username": rec["email"],
        "account_type": "workspace" if hd else "google",
        "tenant_id": hd,
        "provider": PROVIDER,
        "provider_label": LABEL,
    }


# ---- scopes -----------------------------------------------------------------------------


def _norm(scope: str) -> str:
    return _SCOPE_ALIASES.get(scope, scope)


def scopes_for(bundles: list[str]) -> list[str]:
    """Core scopes plus those of `bundles` (validated)."""
    return config.scopes_for(bundles, config.GOOGLE_SCOPE_BUNDLES)


def _granted_bundles(scopes: list[str]) -> list[str]:
    return config.granted_bundles(scopes, config.GOOGLE_SCOPE_BUNDLES, _norm)


# ---- HTTP -------------------------------------------------------------------------------


def _client() -> dict[str, str]:
    cid, secret = config.google_client_id(), config.google_client_secret()
    if not cid or not secret:
        raise Broker365Error(
            "No Google OAuth client configured. Set BROKER365_GOOGLE_CLIENT_ID and "
            "BROKER365_GOOGLE_CLIENT_SECRET (see docs/GOOGLE_PLAN.md §2)."
        )
    return {"client_id": cid, "client_secret": secret}


def _client_http() -> httpx.Client:
    """One pooled client for the token and revoke endpoints (like transport.http())."""
    global _http
    if _http is None:
        _http = httpx.Client(timeout=30)
    return _http


def _post(url: str, form: dict[str, str]) -> httpx.Response:
    try:
        return _client_http().post(url, data=form)
    except httpx.HTTPError as exc:
        # The exception text could echo request details; only its type is shown.
        raise NetworkError(f"Could not reach Google ({type(exc).__name__})") from None


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    """POST to the token endpoint. Returns the JSON body (with `error` on failure)."""
    resp = _post(TOKEN_URL, {**_client(), **form})
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {"error": f"http_{resp.status_code}"}
    if resp.status_code >= 400 and "error" not in body:
        body["error"] = f"http_{resp.status_code}"
    return body


def _error_text(body: dict[str, Any]) -> str:
    desc = body.get("error_description") or body.get("error") or "unknown error"
    return str(desc).splitlines()[0][:300]


def _id_claims(id_token: Any) -> dict[str, Any]:
    """The payload of the ID token from the code exchange. It came straight from Google's
    token endpoint over TLS, in exchange for our code + client secret + PKCE verifier, so
    its signature need not be verified (OpenID Connect Core §3.1.3.7, the code flow)."""
    try:
        payload = str(id_token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        raise Broker365Error("Google returned an unreadable ID token. Try again.") from None
    if not isinstance(claims, dict):
        raise Broker365Error("Google returned an unreadable ID token. Try again.")
    if claims.get("aud") != config.google_client_id():
        raise Broker365Error("Google returned an ID token for another app. Try again.")
    return claims


# ---- flows ------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prepare(bundles: list[str], redirect_uri: str) -> tuple[str, dict[str, Any], str]:
    """(flow_id, flow data, authorize URL) for a new sign-in."""
    scopes = scopes_for(bundles)
    client_id = _client()["client_id"]
    flow_id = flows.new_id()
    secret = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=")
    data = {
        "verifier": verifier,
        "state_secret": secret,
        "redirect_uri": redirect_uri,
        "expires_at": time.time() + FLOW_SECONDS,
    }
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "code_challenge": challenge.decode(),
        "code_challenge_method": "S256",
        "state": f"{flow_id}.{secret}",
    }
    return flow_id, data, f"{AUTH_URL}?{urlencode(query)}"


def _expired(data: dict[str, Any]) -> bool:
    try:
        return float(data.get("expires_at", 0)) < time.time()
    except (TypeError, ValueError):
        return True


def _one(params: dict[str, list[str]], name: str) -> str | None:
    values = params.get(name) or []
    return values[0] if len(values) == 1 else None


def _check_state(flow_id: str, data: dict[str, Any], params: dict[str, list[str]]) -> None:
    """Raise Broker365Error unless the redirect's `state` is this flow's (constant time)."""
    state = _one(params, "state") or ""
    expected = f"{flow_id}.{data.get('state_secret') or ''}"
    if not data.get("state_secret") or not secrets.compare_digest(
        state.encode(), expected.encode()
    ):
        raise Broker365Error(
            "This sign-in response doesn't belong to this sign-in (state mismatch). Paste the "
            "URL from the sign-in you just finished, or start again."
        )


def _check_callback(flow_id: str, data: dict[str, Any], params: dict[str, list[str]]) -> str:
    """Validate the query parameters of a redirect (only code/state/error are read) against
    the flow and return the authorization code."""
    _check_state(flow_id, data, params)
    return _check_response(data, params)


def _check_response(data: dict[str, Any], params: dict[str, list[str]]) -> str:
    """After the state matched: the authorization code, or why there is none."""
    if _expired(data):
        raise _Denied("Sign-in expired, start again.")
    if error := _one(params, "error"):
        if error == "access_denied":
            raise _Denied("Sign-in was cancelled or permission denied.")
        shown = error if _ERROR_CODE.fullmatch(error) else "error"
        raise _Denied(f"Google sign-in failed ({shown}). Start again.")
    code = _one(params, "code")
    if not code:
        raise Broker365Error("The URL has no authorization code. Paste the full address-bar URL.")
    return code


def _check_redirect_url(flow_id: str, data: dict[str, Any], redirect_url: str) -> str:
    """Strictly validate a pasted-back redirect URL; never fetched. Returns the code."""
    expected = urlsplit(str(data.get("redirect_uri") or ""))
    mismatch = Broker365Error(
        f"That is not the broker365 sign-in address: it should start with "
        f"{data.get('redirect_uri')}. Paste the full URL from the browser's address bar."
    )
    try:
        parts = urlsplit(redirect_url.strip())
        port = parts.port
    except ValueError:
        raise mismatch from None
    if (
        parts.scheme != "http"
        or (parts.hostname or "") not in LOOPBACK_HOSTS
        or port is None
        or port != expected.port
        or (parts.path or "/") != (expected.path or "/")
    ):
        raise mismatch
    return _check_callback(flow_id, data, parse_qs(parts.query))


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _memo(key: str, body: dict[str, Any]) -> None:
    """Remember the access token of a token response for at most auth.TOKEN_MEMO_SECONDS
    (and 2 minutes less than it lives); forget the old one otherwise."""
    ttl = min(auth.TOKEN_MEMO_SECONDS, _lifetime(body) - 120)
    if ttl > 0:
        _tokens[key] = (body["access_token"], time.time() + ttl)
    else:
        _tokens.pop(key, None)


def _lifetime(body: dict[str, Any]) -> float:
    """`expires_in` of a token response in seconds; DEFAULT_EXPIRES_IN if missing or invalid."""
    try:
        value = float(body.get("expires_in"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_EXPIRES_IN
    return value if math.isfinite(value) and value > 0 else DEFAULT_EXPIRES_IN


def _has_token(body: dict[str, Any]) -> bool:
    return "error" not in body and isinstance(body.get("access_token"), str)


def _memo_hit(key: str) -> str | None:
    hit = _tokens.get(key)
    return hit[0] if hit and hit[1] > time.time() else None


def forget_token(username: str | None = None) -> None:
    """Drop the memoized access token of `username` (default: the default account) so the
    next get_token refreshes it, e.g. after a Google API rejected it with 401."""
    try:
        key = _account(username)["email"].lower()
    except Broker365Error:
        return
    _tokens.pop(key, None)


def _exchange(code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
    """Trade an authorization code for tokens, store the account, return its description."""
    body = _token_request(
        {
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    if not _has_token(body):
        raise Broker365Error(f"Google sign-in failed: {_error_text(body)}")
    claims = _id_claims(body.get("id_token"))
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        raise Broker365Error("Google did not share the account's email address. Try again.")
    if claims.get("email_verified") is not True:
        raise Broker365Error("Google account email is not verified.")
    key = email.lower()
    sub = claims.get("sub")
    # include_granted_scopes=true: `scope` lists every current grant, not just this sign-in's.
    scope = body.get("scope")
    granted = scope.split() if isinstance(scope, str) else None
    refresh = body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else None

    def apply(data: dict[str, Any]) -> dict[str, Any]:
        old = data["accounts"].get(key)
        old = old if isinstance(old, dict) else {}
        if old.get("sub") and sub and old["sub"] != sub:
            old = {}  # same address, another Google account: nothing carries over
        token = refresh or old.get("refresh_token")
        if not token:
            raise Broker365Error(
                "Google did not return a refresh token. Remove broker365 at "
                "https://myaccount.google.com/connections, then sign in again."
            )
        scopes = granted if granted is not None else list(old.get("scopes", []))
        rec = {
            "email": email,
            "sub": sub or old.get("sub"),
            "refresh_token": token,
            "scopes": scopes,
            "hd": claims.get("hd") or None,
            "email_verified": claims.get("email_verified"),
            "created": old.get("created") or _now_iso(),
        }
        data["accounts"][key] = rec
        return rec

    rec = _modify(apply)
    _memo(key, body)
    config.set_default_account(PROVIDER, email, if_unset=True)
    bundles = _granted_bundles(rec["scopes"])
    audit.record_admin("login", {"provider": PROVIDER, "username": email, "scopes": bundles})
    return {**_describe(rec), "name": claims.get("name"), "scopes": rec["scopes"]}


# ---- loopback listener (CLI) --------------------------------------------------------------


def _wait_for_callback(
    server: http.server.HTTPServer, flow_id: str, data: dict[str, Any], timeout: float
) -> str:
    """Serve requests on `server` until the redirect arrives; return its code."""
    outcome: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        # An idle connection (e.g. a browser's preconnect) is dropped after this many seconds
        # instead of blocking the loop past the overall deadline.
        timeout = READ_TIMEOUT

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parts = urlsplit(self.path)
            if parts.path != "/":
                self._reply(404, "Not found.")
                return
            try:
                outcome["code"] = _check_callback(flow_id, data, parse_qs(parts.query))
                self._reply(200, "Signed in. You can close this tab.")
            except _Denied as exc:
                outcome["error"] = exc
                self._reply(400, f"{exc} You can close this tab.")
            except Broker365Error:
                # Not our sign-in (wrong state): keep waiting for the real redirect.
                self._reply(400, "This sign-in response was not expected. Close this tab.")

        def _reply(self, status: int, message: str) -> None:
            body = callback.page(message).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in callback.HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # the request line holds the authorization code

    server.RequestHandlerClass = Handler
    server.timeout = 0.5
    deadline = time.time() + timeout
    while not outcome and time.time() < deadline:
        server.handle_request()
    if "error" in outcome:
        raise outcome["error"]
    if "code" not in outcome:
        raise Broker365Error("Sign-in timed out: no answer from the browser. Start again.")
    return outcome["code"]


# ---- provider ---------------------------------------------------------------------------


class GoogleProvider:
    name = PROVIDER
    label = LABEL
    revoke_hint = "To revoke completely: https://myaccount.google.com/connections"
    callback_path = CALLBACK_PATH
    # Restricted scopes: Google shows its unverified-app warning for these until verification.
    restricted_bundles = frozenset({"gdrive", "gmail"})

    @property
    def available(self) -> bool:
        return bool(config.google_client_id() and config.google_client_secret())

    def bundles(self) -> dict[str, list[str]]:
        table = config.GOOGLE_SCOPE_BUNDLES
        return {b: list(table[b]) for b in config.user_bundles(table)}

    def default_bundles(self) -> list[str]:
        return list(config.GOOGLE_DEFAULT_BUNDLES)

    def list_accounts(self) -> list[dict[str, Any]]:
        default = (config.default_account(PROVIDER) or "").lower()
        return [
            {**_describe(rec), "default": rec["email"].lower() == default, "tenant_label": "Domain"}
            for rec in _accounts().values()
        ]

    def get_token(self, bundle: str, username: str | None = None) -> str:
        """An access token for `bundle`, refreshed when the memo ran out. ConsentRequired if
        the bundle wasn't granted, ReauthRequired if the refresh token stopped working."""
        if bundle not in config.GOOGLE_SCOPE_BUNDLES:
            raise Broker365Error(f"Unknown Google scope bundle '{bundle}'")
        rec = _account(username)
        if bundle != "core" and bundle not in _granted_bundles(rec.get("scopes", [])):
            raise ConsentRequired(bundle, PROVIDER)
        key = rec["email"].lower()
        if token := _memo_hit(key):
            return token
        with _refresh_lock:  # single flight: a refresh another thread just did is reused
            if token := _memo_hit(key):
                return token
            body = _token_request(
                {"grant_type": "refresh_token", "refresh_token": str(rec.get("refresh_token"))}
            )
            if body.get("error") == "invalid_grant":
                raise ReauthRequired(f"Google sign-in expired or was revoked. {LOGIN_HINT}")
            if not _has_token(body):
                raise Broker365Error(f"Google token refresh failed: {_error_text(body)}")
            _memo(key, body)
            return body["access_token"]

    def has_consent(self, bundle: str, username: str | None = None) -> bool:
        if bundle not in config.GOOGLE_SCOPE_BUNDLES:
            return False
        rec = _account(username)  # not signed in / no default: its own error, not consent
        return bundle == "core" or bundle in _granted_bundles(rec.get("scopes", []))

    def invalidate_token(self, username: str | None = None) -> None:
        forget_token(username)

    def check_token(self, username: str) -> None:
        self.get_token("core", username)

    def login_start(self, bundles: list[str], redirect_uri: str | None = None) -> dict[str, Any]:
        """Start a sign-in. Without `redirect_uri` the user pastes back the URL of the
        loopback redirect (chat/headless); the dashboard passes its own callback URL."""
        pasted = redirect_uri is None
        uri = redirect_uri or f"http://127.0.0.1:{_free_port()}/"
        flow_id, data, url = _prepare(bundles, uri)
        flows.save(PROVIDER, data, flow_id=flow_id)
        warning = (
            "If Google warns that it hasn't verified this app, choose Advanced, then "
            "Go to broker365 (unsafe)."
        )
        if pasted:
            instructions = (
                "Open the link and sign in with your Google account, then approve the "
                f"permissions. {warning} Afterwards the browser opens {uri}… and shows "
                '"This site can\'t be reached". That is expected: copy the full URL from the '
                "address bar and paste it back here. The link is valid for 10 minutes."
            )
        else:
            instructions = (
                "Open the Google sign-in page, pick your account and approve the permissions. "
                f"{warning} This page updates by itself when you're done."
            )
        return {
            "flow_id": flow_id,
            "provider": PROVIDER,
            "kind": "authorize_url",
            "needs_redirect_url": pasted,
            "authorize_url": url,
            "expires_in": FLOW_SECONDS,
            "instructions": instructions,
        }

    def login_complete(
        self,
        flow_id: str,
        data: dict[str, Any],
        wait_seconds: float | None = None,
        redirect_url: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(data.get("result"), dict):  # finished by the dashboard callback
            flows.consume(flow_id)
            return {"status": "signed_in", **data["result"]}
        if data.get("error"):
            flows.consume(flow_id)
            raise Broker365Error(str(data["error"]))
        if _expired(data):
            flows.consume(flow_id)
            raise Broker365Error("Sign-in expired, start again.")
        if not redirect_url:
            return {
                "status": "pending",
                "flow_id": flow_id,
                "provider": PROVIDER,
                "message": (
                    "Waiting for Google sign-in. If the user pasted the address-bar URL, pass "
                    "it as redirect_url."
                ),
            }
        try:
            code = _check_redirect_url(flow_id, data, redirect_url)
        except _Denied:
            flows.consume(flow_id)
            raise
        flows.consume(flow_id)  # single use, whatever the exchange's outcome
        return {"status": "signed_in", **_exchange(code, data["verifier"], data["redirect_uri"])}

    def complete_callback(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """Handle the dashboard's /oauth/google/callback: find the flow by the id in `state`,
        validate it, exchange the code and store the outcome in the flow file for the page's
        polling `login_complete`. Returns the signed-in account; raises Broker365Error."""
        state = _one(params, "state") or ""
        flow_id = state.partition(".")[0]
        with _callback_lock:  # load, check and claim atomically: one request wins a flow
            provider, data = flows.load(flow_id)
            if provider != PROVIDER or "result" in data or "error" in data:
                raise Broker365Error("Unknown or already-used sign-in")
            # Before this check nothing is recorded, so a forged request can't end a flow.
            _check_state(flow_id, data, params)
            keep = {"expires_at": data.get("expires_at")}
            # Claimed: the verifier and state secret are dropped, so any later request with
            # this state fails _check_state and can't overwrite the outcome written below.
            flows.update(flow_id, PROVIDER, keep)
        try:
            code = _check_response(data, params)
            result = _exchange(code, data["verifier"], data["redirect_uri"])
        except Broker365Error as exc:
            flows.update(flow_id, PROVIDER, {**keep, "error": str(exc)})
            raise
        except Exception:
            flows.update(flow_id, PROVIDER, {**keep, "error": "Google sign-in failed."})
            raise
        flows.update(flow_id, PROVIDER, {**keep, "result": result})
        return result

    def login_interactive(self, bundles: list[str], port: int | None = None) -> dict[str, Any]:
        """Browser sign-in on this machine through a one-shot loopback listener."""
        try:
            server = http.server.HTTPServer(
                ("127.0.0.1", port or 0), http.server.BaseHTTPRequestHandler
            )
        except OSError as exc:
            raise Broker365Error(f"Could not listen on 127.0.0.1:{port or 0} ({exc})") from None
        try:
            uri = f"http://127.0.0.1:{server.server_address[1]}/"
            flow_id, data, url = _prepare(bundles, uri)
            print(f"Opening Google sign-in. If no browser opens, visit:\n{url}", file=sys.stderr)
            webbrowser.open(url)
            code = _wait_for_callback(server, flow_id, data, INTERACTIVE_TIMEOUT)
        finally:
            server.server_close()
        return _exchange(code, data["verifier"], uri)

    def logout(self, username: str | None = None) -> str:
        rec = _account(username)
        email = rec["email"]
        token = rec.get("refresh_token")
        if token:
            try:  # best effort: the local sign-out happens anyway
                _post(REVOKE_URL, {"token": token})
            except NetworkError:
                log.warning("Could not reach Google to revoke the token; removed locally")

        def apply(data: dict[str, Any]) -> None:
            data["accounts"].pop(email.lower(), None)

        _modify(apply)
        _tokens.pop(email.lower(), None)
        if (config.default_account(PROVIDER) or "").lower() == email.lower():
            config.set_default_account(PROVIDER, None)
        audit.record_admin("logout", {"provider": PROVIDER, "username": email})
        return email

    def set_default(self, username: str) -> None:
        email = _account(username)["email"]
        config.set_default_account(PROVIDER, email)
        audit.record_admin("set_default", {"provider": PROVIDER, "username": email})
