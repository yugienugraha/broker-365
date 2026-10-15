"""Identity providers: Microsoft and Google (docs/GOOGLE_PLAN.md §5).

A provider owns sign-in, token refresh, sign-out and its own accounts. Everything above this
layer (services, CLI, dashboard) talks to providers through the `Provider` protocol and the
registry below instead of calling a provider's auth module directly.
"""

from __future__ import annotations

from typing import Any, Protocol

from broker365 import config
from broker365.errors import Broker365Error, ReauthRequired
from broker365.providers import flows
from broker365.providers.google import GoogleProvider
from broker365.providers.microsoft import MicrosoftProvider

DEFAULT = config.MICROSOFT


class Provider(Protocol):
    name: str  # registry key, e.g. "microsoft"; also stored in settings, flow files, audit
    label: str  # human-readable, e.g. "Microsoft 365"
    revoke_hint: str  # where the user can revoke the app entirely (shown after sign-out)
    available: bool  # False when it can't be used (e.g. no OAuth client configured)
    # Path of the dashboard route the provider's sign-in redirects to (GET, exempt from the
    # session cookie, protected by state + PKCE), or None when it has no redirect sign-in.
    callback_path: str | None
    restricted_bundles: frozenset[str]  # bundles that trigger an unverified-app warning

    def bundles(self) -> dict[str, list[str]]:
        """User-selectable scope bundles (no implicit "core") mapped to their scopes."""
        ...

    def default_bundles(self) -> list[str]:
        """Bundles to request when the user picks none."""
        ...

    def list_accounts(self) -> list[dict[str, Any]]:
        """Signed-in accounts: username, account_type, tenant_id (None when there is none),
        tenant_label (what tenant_id is called, e.g. "Tenant" or "Domain"), default, provider,
        provider_label."""
        ...

    def get_token(self, bundle: str, username: str | None = None) -> str: ...

    def invalidate_token(self, username: str | None = None) -> None:
        """Make the next get_token for `username` (the account get_token would pick when
        None) return a freshly issued access token instead of any cached one, e.g. after an
        API rejected the token (401) or lacked a scope granted since. Other accounts keep
        their cached tokens."""
        ...

    def check_token(self, username: str) -> None:
        """Raise Broker365Error (with a user-facing reason) if `username`'s sign-in is no
        longer usable."""
        ...

    def login_start(self, bundles: list[str], redirect_uri: str | None = None) -> dict[str, Any]:
        """Begin a chat/dashboard sign-in (state kept with `flows.save`). `redirect_uri` (only
        passed when `callback_path` is set) is the dashboard's callback URL; without it the
        user finishes another way (device code, pasted-back URL). Returns:
        flow_id, provider, kind ("device_code" | "authorize_url"), instructions (user-facing
        text written by the provider), needs_redirect_url (bool), and per kind:
        device_code -> verification_uri, user_code, expires_in, message;
        authorize_url -> authorize_url, expires_in."""
        ...

    def login_complete(
        self,
        flow_id: str,
        data: dict[str, Any],
        wait_seconds: float | None = None,
        redirect_url: str | None = None,
    ) -> dict[str, Any]:
        """Finish (or poll) a flow from login_start: {"status": "pending" | "signed_in", ...}.
        `data` is the flow's stored state, already read with `flows.load`."""
        ...

    def complete_callback(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """Handle a GET on `callback_path` (its parsed query): validate, finish the flow and
        return the signed-in account; raise Broker365Error otherwise."""
        ...

    def login_interactive(self, bundles: list[str], port: int | None = None) -> dict[str, Any]:
        """Browser sign-in on this machine (CLI)."""
        ...

    def logout(self, username: str | None = None) -> str:
        """Sign out `username` (any case; the provider matches it) and return the stored name."""
        ...

    def set_default(self, username: str) -> None: ...


_PROVIDERS: dict[str, Provider] = {p.name: p for p in (MicrosoftProvider(), GoogleProvider())}


def get(name: str | None = None) -> Provider:
    """The provider called `name` (default: Microsoft). Unknown or unconfigured names raise
    Broker365Error with a message fit for the user."""
    key = (name or DEFAULT).strip().lower()
    if key not in _PROVIDERS:
        raise Broker365Error(f"Unknown provider '{name}'. Known: {', '.join(_PROVIDERS)}")
    return _PROVIDERS[key]


def all() -> list[Provider]:
    """Every available provider, default first."""
    return list(_PROVIDERS.values())


def catalog() -> list[dict[str, Any]]:
    """Every known provider for pickers: [{name, label, available}]."""
    return [
        {"name": p.name, "label": p.label, "available": p.available} for p in _PROVIDERS.values()
    ]


def _listings() -> tuple[list[tuple[Provider, list[dict[str, Any]]]], list[str]]:
    """([(provider, its accounts)], [errors]) for every available provider. A provider whose
    listing fails (Broker365Error) is skipped so the others stay manageable."""
    listed: list[tuple[Provider, list[dict[str, Any]]]] = []
    errors: list[str] = []
    for p in _PROVIDERS.values():
        try:
            listed.append((p, p.list_accounts()))
        except Broker365Error as exc:
            errors.append(str(exc))
    return listed, errors


def all_accounts() -> list[dict[str, Any]]:
    """Accounts of every available provider (each with its `provider`). Providers whose
    listing fails are left out."""
    return [a for _, accts in _listings()[0] for a in accts]


def resolve(
    username: str | None = None, provider: str | None = None
) -> tuple[Provider, str | None]:
    """The provider to act on and the username to pass it (the provider's own spelling when
    it was looked up). An explicit `provider` wins without any lookup. Otherwise `username`
    is looked up (case-insensitively) among the accounts of every provider that lists
    successfully; with no username, the default provider."""
    if provider or not username:
        return get(provider), username
    wanted = username.strip().lower()
    found = [
        (p, a["username"])
        for p, accts in _listings()[0]
        for a in accts
        if (a.get("username") or "").lower() == wanted
    ]
    if not found:
        raise ReauthRequired(f"Account {username} is not signed in. Run: broker365 login")
    if len({p.name for p, _ in found}) > 1:
        labels = ", ".join(dict.fromkeys(p.label for p, _ in found))
        raise Broker365Error(
            f"Account {username} is signed in with several providers ({labels}). "
            "Say which provider to use."
        )
    return found[0]


def validate_bundles(provider_name: str | None, bundles: list[str]) -> list[str]:
    """`bundles` if every one is a scope bundle of that provider (or the implicit "core"),
    else a Broker365Error that lists the provider's bundles."""
    known = get(provider_name).bundles()
    if unknown := [b for b in bundles if b not in known and b != "core"]:
        raise Broker365Error(
            f"Unknown scope bundle(s): {', '.join(unknown)}. Known: {', '.join(known)}"
        )
    return list(bundles)


def choose_bundles(provider_name: str | None, bundles: list[str] | None) -> list[str]:
    """`bundles` validated for the provider, or its default bundles when none are given."""
    p = get(provider_name)
    return validate_bundles(p.name, bundles) if bundles else p.default_bundles()


def login_start(
    provider_name: str | None, bundles: list[str] | None = None, redirect_uri: str | None = None
) -> dict[str, Any]:
    """Start a sign-in with `provider_name` (default provider when None) for `bundles` (its
    defaults when empty). `redirect_uri` is for the dashboard only: never take it from an
    agent, which must not choose where the authorization code is sent."""
    return get(provider_name).login_start(
        choose_bundles(provider_name, bundles), redirect_uri=redirect_uri
    )


def login_complete(
    flow_id: str, wait_seconds: float | None = None, redirect_url: str | None = None
) -> dict[str, Any]:
    """Finish a flow started by any provider; the flow file records which one. The file is
    read once here and its data handed to the provider."""
    provider, data = flows.load(flow_id)
    return get(provider).login_complete(
        flow_id, data, wait_seconds=wait_seconds, redirect_url=redirect_url
    )
