"""Sign-in status and chat-friendly login, so an agent can connect an account without the
user touching a terminal."""

from __future__ import annotations

from typing import Any

from broker365 import config, providers
from broker365.errors import Broker365Error
from broker365.services import tool


@tool("core")
def account_status() -> dict[str, Any]:
    """Show which accounts are connected (each with its `provider`, e.g. microsoft) and
    whether their sign-in is still valid."""
    out: dict[str, Any] = {
        "home": str(config.broker_home()),
        "workspace": str(config.workspace()),
        "read_only": config.read_only(),
    }
    accounts: list[dict[str, Any]] = []
    errors: list[str] = []
    for provider in providers.all():
        try:
            found = provider.list_accounts()
        except Broker365Error as exc:
            errors.append(str(exc))
            continue
        for acct in found:
            try:
                provider.check_token(acct["username"])
                acct["sign_in"] = "valid"
            except Broker365Error as exc:
                acct["sign_in"] = str(exc)
        accounts += found
    out = {**out, "signed_in": bool(accounts), "accounts": accounts}
    if errors:
        out["error"] = "; ".join(errors)
    return out


@tool("core")
def account_login_start(
    scopes: list[str] | None = None, provider: str | None = providers.DEFAULT
) -> dict[str, Any]:
    """Start a sign-in and relay `instructions` to the user. If `kind` is "device_code", they
    open `verification_uri` and enter `user_code`; then call account_login_complete with
    `flow_id`. If `kind` is "authorize_url", they open `authorize_url`, sign in and paste back
    the address-bar URL they land on; then call account_login_complete with `flow_id` and that
    `redirect_url`. provider: microsoft (default) or google. scopes: bundle names of that
    provider (an unknown name returns the provider's list); leave empty for the provider's
    defaults, which depend on the provider."""
    return providers.login_start(provider, scopes)


@tool("core")
def account_login_complete(
    flow_id: str, wait_seconds: int = 60, redirect_url: str | None = None
) -> dict[str, Any]:
    """Finish a sign-in from account_login_start. For a "device_code" flow, waits up to
    `wait_seconds` for the user and returns status 'pending' (call again) or 'signed_in'.
    For an "authorize_url" flow (needs_redirect_url, e.g. Google), pass the full URL the user
    pasted back as `redirect_url`. Leave redirect_url empty for device-code flows (e.g.
    Microsoft)."""
    return providers.login_complete(
        flow_id, wait_seconds=max(1, min(wait_seconds, 100)), redirect_url=redirect_url
    )
