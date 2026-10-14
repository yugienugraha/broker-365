"""Sign-in status and chat-friendly device-code login, so an agent can connect an account
without the user touching a terminal."""

from __future__ import annotations

from typing import Any

from broker365 import auth, config
from broker365.errors import Broker365Error
from broker365.services import tool


@tool("core")
def account_status() -> dict[str, Any]:
    """Show which Microsoft accounts are connected and whether their sign-in is still valid."""
    out: dict[str, Any] = {
        "home": str(config.broker_home()),
        "workspace": str(config.workspace()),
        "read_only": config.read_only(),
    }
    try:
        accounts = auth.list_accounts()
    except Broker365Error as exc:
        return {**out, "signed_in": False, "accounts": [], "error": str(exc)}
    for acct in accounts:
        try:
            auth.get_token("core", acct["username"])
            acct["sign_in"] = "valid"
        except Broker365Error as exc:
            acct["sign_in"] = str(exc)
    return {**out, "signed_in": bool(accounts), "accounts": accounts}


@tool("core")
def account_login_start(scopes: list[str] | None = None) -> dict[str, Any]:
    """Start a Microsoft sign-in. Show the user `verification_uri` and `user_code`, then call
    account_login_complete with `flow_id`. scopes: files, mail, calendar, contacts, tasks,
    files-shared, sharepoint, onenote (default: files)."""
    return auth.device_flow_start(scopes or ["files"])


@tool("core")
def account_login_complete(flow_id: str, wait_seconds: int = 60) -> dict[str, Any]:
    """Wait up to `wait_seconds` for the user to finish signing in. Returns status 'pending'
    (call again) or 'signed_in'."""
    return auth.device_flow_complete(flow_id, wait_seconds=max(1, min(wait_seconds, 100)))
