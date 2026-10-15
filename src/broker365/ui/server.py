"""Starlette app behind `broker365 ui`. Bound to loopback. All access control lives in the
`guard` middleware, so no route can forget it: every request must carry the right Host header
(DNS rebinding); `GET /?token=` performs the one-time handshake that sets the session cookie;
every other path needs that cookie, and /api/* additionally needs the `X-Broker365: 1` header
(no cross-site forms). Endpoints are sync and run in a threadpool; they never return tokens.

The cookie is named per port (`broker365_session_<port>`) so dashboards on different ports don't
overwrite each other. Browsers don't isolate cookies by port, though: any other service on
localhost that the browser visits could read it (HttpOnly + SameSite=Strict only stop scripts and
cross-site requests). The token is random per run and dies with the process, so stop the
dashboard (Ctrl+C) when you're not using it."""

from __future__ import annotations

import inspect
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from broker365 import __version__, audit, config, hermes, providers, services
from broker365.errors import Broker365Error
from broker365.services import account


def url_host(host: str) -> str:
    """How a bound interface appears in a URL / Host header (IPv6 bracketed)."""
    if host in ("127.0.0.1", "localhost", ""):
        return "127.0.0.1"
    host = host.lower()
    return f"[{host}]" if ":" in host else host


def cookie_name(port: int) -> str:
    return f"broker365_session_{port}"


LOOPBACK = ("127.0.0.1", "localhost", "::1")
STATIC = {"app.js": "text/javascript", "app.css": "text/css", "favicon.svg": "image/svg+xml"}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
UNAUTHORIZED = (
    "Not authorised. Open the dashboard with the URL printed by `broker365 ui` "
    "(it contains a one-time token).\n"
)


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _field(body: dict[str, Any], key: str, kind: type, required: bool = True) -> Any:
    if key not in body:
        if required:
            raise Broker365Error(f"'{key}' is required")
        return None
    value = body[key]
    if not isinstance(value, kind) or (kind is str and not value.strip()):
        raise Broker365Error(f"'{key}' must be a {'non-empty string' if kind is str else kind}")
    return value


def _file(path: Path, media_type: str) -> Response:
    if not path.is_file():
        return PlainTextResponse("Not Found\n", status_code=404)
    return FileResponse(path, media_type=media_type)


# ---- API handlers (sync; take `body` (parsed JSON object) and/or `query` as needed) ----


def overview() -> dict[str, Any]:
    return {
        "version": __version__,
        "client_id": config.client_id(),
        **account.account_status(),
        "hermes": hermes.status(),
        "tools_count": len(services.available_tools()),
    }


def _provider(body: dict[str, Any]) -> str | None:
    """The optional `provider` field; None means the default (or, with a username, the
    provider that account belongs to)."""
    return _field(body, "provider", str, required=False)


def scopes(query: dict[str, str]) -> dict[str, Any]:
    return {"bundles": providers.get(query.get("provider")).bundles()}


def login_start(body: dict[str, Any]) -> dict[str, Any]:
    bundles = _field(body, "scopes", list, required=False)
    if bundles is not None and not all(isinstance(b, str) for b in bundles):
        raise Broker365Error("'scopes' must be a list of bundle names")
    # account_login_start checks the bundles against the provider (providers.validate_bundles)
    return account.account_login_start(bundles, provider=_provider(body))


def login_complete(body: dict[str, Any]) -> dict[str, Any]:
    return account.account_login_complete(
        _field(body, "flow_id", str),
        wait_seconds=5,
        redirect_url=_field(body, "redirect_url", str, required=False),
    )


def set_default(body: dict[str, Any]) -> dict[str, Any]:
    p, username = providers.resolve(_field(body, "username", str), _provider(body))
    p.set_default(username)
    return {"accounts": providers.all_accounts()}


def logout(body: dict[str, Any]) -> dict[str, Any]:
    p, username = providers.resolve(_field(body, "username", str), _provider(body))
    return {"signed_out": p.logout(username)}


def get_settings() -> dict[str, Any]:
    return config.settings_view()


def post_settings(body: dict[str, Any]) -> dict[str, Any]:
    read_only = _field(body, "read_only", bool, required=False)
    add = _field(body, "add_dir", str, required=False)
    remove = _field(body, "remove_dir", str, required=False)
    reset = _field(body, "reset_excel_probe", bool, required=False)
    changed = config.change_settings(
        read_only,
        [add] if add else (),
        [remove] if remove else (),
        reset_excel_probe=bool(reset),
    )
    if changed:
        audit.record_admin("settings", changed)
    return config.settings_view()


def hermes_status() -> dict[str, Any]:
    return hermes.status()


def hermes_install() -> dict[str, Any]:
    return hermes.install()


def tools() -> list[dict[str, Any]]:
    return services.tool_catalog()


def activity(query: dict[str, str]) -> list[dict[str, Any]]:
    try:
        limit = int(query.get("limit", "100"))
    except ValueError:
        raise Broker365Error("'limit' must be an integer") from None
    return audit.read(max(1, min(limit, 1000)))


def api(fn: Callable[..., Any]) -> Callable:
    """Wrap a sync handler: parse/validate the JSON body (POST), pass `body` and/or `query`
    if the handler takes them, map errors. Access checks are done by the `guard` middleware."""
    params = inspect.signature(fn).parameters

    async def endpoint(request: Request) -> Response:
        kwargs: dict[str, Any] = {}
        if "query" in params:
            kwargs["query"] = dict(request.query_params)
        if request.method == "POST":
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return _err(415, "Content-Type must be application/json")
            raw = await request.body()
            try:
                body = json.loads(raw) if raw.strip() else {}
            except ValueError:
                return _err(400, "Request body is not valid JSON")
            if not isinstance(body, dict):
                return _err(400, "Request body must be a JSON object")
            if "body" in params:
                kwargs["body"] = body
        elif "body" in params:
            kwargs["body"] = {}
        try:
            return JSONResponse(await run_in_threadpool(fn, **kwargs))
        except Broker365Error as exc:
            return _err(400, str(exc))
        except Exception:  # noqa: BLE001 - never leak internals to the page
            return _err(500, "Internal error")

    return endpoint


# ---- app ------------------------------------------------------------------------------


def create_app(
    token: str, port: int, static_dir: Path | None = None, host: str | None = None
) -> Starlette:
    static = static_dir or Path(__file__).parent / "static"
    cookie = cookie_name(port)
    names = {"127.0.0.1", "localhost", "[::1]"}
    if host and host not in LOOPBACK:
        names.add(url_host(host))
    hosts = {f"{n}:{port}" for n in names}
    if port == 80:  # browsers omit the default port from the Host header
        hosts |= names

    def valid(value: str | None) -> bool:
        return bool(value) and secrets.compare_digest(value.encode(), token.encode())

    def check(request: Request) -> Response | None:
        """The response that refuses (or completes the handshake for) this request, or None
        to let it through to the router."""
        if request.headers.get("host", "").lower() not in hosts:
            return PlainTextResponse("Invalid Host header\n", status_code=400)
        path = request.url.path
        is_api = path == "/api" or path.startswith("/api/")
        has_session = valid(request.cookies.get(cookie))
        if path == "/" and request.method == "GET" and "token" in request.query_params:
            if valid(request.query_params.get("token")):
                response = RedirectResponse("/", status_code=303)
                response.set_cookie(cookie, token, httponly=True, samesite="strict", path="/")
                return response
            if has_session:  # stale token, live session: drop it from the URL
                return RedirectResponse("/", status_code=303)
        if not has_session:
            if is_api:
                return _err(401, "Not authorised: open the URL printed by `broker365 ui`")
            return PlainTextResponse(UNAUTHORIZED, status_code=401)
        if is_api and request.headers.get("x-broker365") != "1":
            return _err(403, "Missing X-Broker365 header")
        return None

    async def guard(request: Request, call_next: Callable) -> Response:
        response = check(request) or await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    async def index(request: Request) -> Response:
        return _file(static / "index.html", "text/html; charset=utf-8")

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in STATIC:
            return PlainTextResponse("Not Found\n", status_code=404)
        return _file(static / name, STATIC[name])

    routes = [
        Route("/", index),
        Route("/static/{name}", asset),
        Route("/api/overview", api(overview)),
        Route("/api/providers", api(providers.catalog)),
        Route("/api/scopes", api(scopes)),
        Route("/api/login/start", api(login_start), methods=["POST"]),
        Route("/api/login/complete", api(login_complete), methods=["POST"]),
        Route("/api/accounts/default", api(set_default), methods=["POST"]),
        Route("/api/accounts/logout", api(logout), methods=["POST"]),
        Route("/api/settings", api(get_settings)),
        Route("/api/settings", api(post_settings), methods=["POST"]),
        Route("/api/hermes", api(hermes_status)),
        Route("/api/hermes/install", api(hermes_install), methods=["POST"]),
        Route("/api/tools", api(tools)),
        Route("/api/activity", api(activity)),
    ]
    return Starlette(routes=routes, middleware=[Middleware(BaseHTTPMiddleware, dispatch=guard)])
