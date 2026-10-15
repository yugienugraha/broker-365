"""MSAL public-client auth: interactive (loopback + PKCE), device code, silent refresh.

Tokens live only in the local MSAL cache: OS-encrypted (DPAPI / Keychain / libsecret) when
available, otherwise a mode-600 file. Nothing here ever logs or returns a token to callers
other than graph.py.
"""

from __future__ import annotations

import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import msal
from msal_extensions import FilePersistence, PersistedTokenCache, build_encrypted_persistence
from msal_extensions.persistence import PersistenceNotFound

from broker365 import audit, config
from broker365.errors import Broker365Error, ConsentRequired, ReauthRequired
from broker365.providers import flows

log = logging.getLogger(__name__)

_CONSENT_MARKERS = ("consent_required", "AADSTS65001", "interaction_required")
PROVIDER = config.MICROSOFT  # name in default_accounts, flow files and audit entries


# ---- cache / app ----------------------------------------------------------------------


def _private_file(path: Path) -> None:
    """Create `path` with owner-only permissions if it doesn't exist yet."""
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on Windows
        pass


def _persistence(path: Path):
    if not config.env_flag("BROKER365_PLAINTEXT_CACHE"):
        try:
            p = build_encrypted_persistence(str(path))
            try:
                p.load()  # surfaces missing libsecret / locked keychain early
            except PersistenceNotFound:
                pass
            return p
        except Exception as exc:  # noqa: BLE001 - any backend failure → file fallback
            log.warning("Encrypted token cache unavailable (%s); using mode-600 file", exc)
    _private_file(path)
    return FilePersistence(str(path))


@lru_cache(maxsize=1)
def _app(client_id: str, home: str) -> msal.PublicClientApplication:
    cache = PersistedTokenCache(_persistence(Path(home) / "token_cache.bin"))
    return msal.PublicClientApplication(client_id, authority=config.AUTHORITY, token_cache=cache)


def app() -> msal.PublicClientApplication:
    cid = config.client_id()
    if cid == config.PLACEHOLDER_CLIENT_ID:
        raise Broker365Error(
            "No client ID configured. Set BROKER365_CLIENT_ID or DEFAULT_CLIENT_ID in config.py "
            "(see docs/AZURE_SETUP.md)."
        )
    return _app(cid, str(config.broker_home()))


# ---- accounts -------------------------------------------------------------------------


def _tenant(account: dict[str, Any]) -> str:
    """Tenant id from home_account_id ('<oid>.<tid>'). MSAL's `realm` is the authority segment
    used at sign-in ('common' here), so it can't tell personal from work accounts."""
    home = account.get("home_account_id") or ""
    if "." in home:
        return home.rpartition(".")[2]
    realm = account.get("realm") or ""
    return "" if realm in ("common", "organizations", "consumers") else realm


def _account_type(tenant_id: str | None) -> str:
    return "personal" if tenant_id == config.MSA_TENANT_ID else "work_or_school"


def describe(account: dict[str, Any]) -> dict[str, Any]:
    tid = _tenant(account)
    return {
        "username": account.get("username"),
        "account_type": _account_type(tid),
        "tenant_id": tid,
    }


def list_accounts() -> list[dict[str, Any]]:
    default = (config.default_account(PROVIDER) or "").lower()
    return [
        {**describe(a), "default": (a.get("username") or "").lower() == default}
        for a in app().get_accounts()
    ]


def current_account(username: str | None = None) -> dict[str, Any]:
    accounts = app().get_accounts()
    wanted = (
        username
        or os.environ.get("BROKER365_ACCOUNT", "").strip()
        or config.default_account(PROVIDER)
    )
    if wanted:
        for a in accounts:
            if (a.get("username") or "").lower() == wanted.lower():
                return a
        raise ReauthRequired(f"Account {wanted} is not signed in. Run: broker365 login")
    if not accounts:
        raise ReauthRequired("Not signed in. Run: broker365 login")
    if len(accounts) > 1:
        names = ", ".join(a.get("username", "?") for a in accounts)
        raise Broker365Error(
            f"Several accounts signed in ({names}). Pick one: broker365 accounts --default <user>"
        )
    return accounts[0]


def is_personal_account(account: dict[str, Any]) -> bool:
    return _tenant(account) == config.MSA_TENANT_ID


def is_personal(username: str | None = None) -> bool:
    return is_personal_account(current_account(username))


def set_default(username: str) -> None:
    # store MSAL's spelling so later comparisons are exact
    name = current_account(username)["username"]
    config.set_default_account(PROVIDER, name)
    _tokens.clear()
    audit.record_admin("set_default", {"provider": PROVIDER, "username": name})


def logout(username: str | None = None) -> str:
    acct = current_account(username)
    app().remove_account(acct)
    _tokens.clear()
    name = acct.get("username", "")
    if (config.default_account(PROVIDER) or "").lower() == name.lower():
        config.set_default_account(PROVIDER, None)
    audit.record_admin("logout", {"provider": PROVIDER, "username": name})
    return name


# ---- tokens ---------------------------------------------------------------------------


# In-process memo so paging/chunked calls don't re-read the persisted cache per request.
# Entries live at most TOKEN_MEMO_SECONDS so account switches in other processes apply soon.
TOKEN_MEMO_SECONDS = 300
_tokens: dict[tuple[str, str | None], tuple[str, float]] = {}


def get_token(bundle: str, username: str | None = None) -> str:
    """Return an access token for `bundle`, refreshing silently. Raises ConsentRequired if
    the bundle hasn't been granted and ReauthRequired if the refresh token is unusable."""
    key = (bundle, username)
    if (hit := _tokens.get(key)) and hit[1] > time.time():
        return hit[0]
    scopes = config.scopes_for([bundle] if bundle != "core" else [])
    # _with_error: plain acquire_token_silent folds refresh errors into None, which would
    # make a missing-consent failure indistinguishable from an expired sign-in.
    result = app().acquire_token_silent_with_error(scopes, account=current_account(username))
    if result and "access_token" in result:
        ttl = min(TOKEN_MEMO_SECONDS, float(result.get("expires_in", 0)) - 120)
        if ttl > 0:
            _tokens[key] = (result["access_token"], time.time() + ttl)
        return result["access_token"]
    if result and any(m in json.dumps(result) for m in _CONSENT_MARKERS):
        raise ConsentRequired(bundle)
    raise ReauthRequired("Sign-in expired or revoked. Run: broker365 login")


def _finish_login(result: dict[str, Any]) -> dict[str, Any]:
    if "access_token" not in result:
        desc = result.get("error_description") or result.get("error") or "unknown error"
        raise Broker365Error(f"Sign-in failed: {desc.splitlines()[0]}")
    claims = result.get("id_token_claims", {})
    username = claims.get("preferred_username") or claims.get("email") or ""
    if username:
        config.set_default_account(PROVIDER, username, if_unset=True)
    _tokens.clear()
    granted = result.get("scope", "").split()
    audit.record_admin(
        "login",
        {"provider": PROVIDER, "username": username, "scopes": _bundles_granted(granted)},
    )
    return {
        "username": username,
        "name": claims.get("name"),
        "account_type": _account_type(claims.get("tid")),
        "tenant_id": claims.get("tid"),
        "scopes": result.get("scope", "").split(),
    }


def _bundles_granted(scopes: list[str]) -> list[str]:
    """Scope bundles fully covered by the granted scopes (names only, for the audit log)."""
    have = {s.rsplit("/", 1)[-1].lower() for s in scopes}
    return [
        b for b in config.user_bundles() if {s.lower() for s in config.SCOPE_BUNDLES[b]} <= have
    ]


def login_interactive(bundles: list[str], port: int | None = None) -> dict[str, Any]:
    result = app().acquire_token_interactive(
        config.scopes_for(bundles), prompt="select_account", port=port, timeout=300
    )
    return _finish_login(result)


def device_flow_start(bundles: list[str]) -> dict[str, Any]:
    """Begin device-code sign-in. Returns the URL/code to show the user and a flow_id
    to pass to device_flow_complete (possibly from a different process)."""
    flow = app().initiate_device_flow(scopes=config.scopes_for(bundles))
    if "user_code" not in flow:
        raise Broker365Error(f"Could not start device sign-in: {flow.get('error_description')}")
    flow_id = flows.save(PROVIDER, flow)
    return {
        "flow_id": flow_id,
        "verification_uri": flow["verification_uri"],
        "user_code": flow["user_code"],
        "expires_in": flow.get("expires_in"),
        "message": flow.get("message"),
    }


def device_flow_complete(
    flow_id: str, wait_seconds: float | None = None, flow: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Wait for the user to finish device-code sign-in. With `wait_seconds`, gives up early
    and returns {"status": "pending"} so callers with short tool timeouts can poll again.
    `flow` is the flow's data when the caller already loaded it (providers.login_complete)."""
    if flow is None:
        provider, flow = flows.load(flow_id)
        if provider != PROVIDER:
            raise Broker365Error(f"Flow {flow_id} is not a Microsoft sign-in")
    expires = flow.get("expires_at", 0)
    deadline = expires if wait_seconds is None else min(expires, time.time() + wait_seconds)
    result = app().acquire_token_by_device_flow(
        flow, exit_condition=lambda f: time.time() > deadline
    )
    if result.get("error") in ("authorization_pending", "slow_down") and time.time() < expires:
        return {"status": "pending", "flow_id": flow_id, "user_code": flow.get("user_code")}
    flows.consume(flow_id)
    return {"status": "signed_in", **_finish_login(result)}
