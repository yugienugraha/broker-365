"""Microsoft 365 / personal Microsoft accounts: a thin adapter over broker365.auth (MSAL)."""

from __future__ import annotations

from typing import Any

from broker365 import auth, config
from broker365.errors import Broker365Error


class MicrosoftProvider:
    name = config.MICROSOFT
    label = "Microsoft 365"
    revoke_hint = (
        "To revoke the app entirely: https://myapps.microsoft.com "
        "(work) or https://account.live.com/consent/Manage (personal)."
    )
    available = True  # the built-in public client always works (no secret to configure)
    callback_path = None  # device code / MSAL's own loopback: no redirect to the dashboard
    restricted_bundles: frozenset[str] = frozenset()

    def bundles(self) -> dict[str, list[str]]:
        return {b: list(config.SCOPE_BUNDLES[b]) for b in config.user_bundles()}

    def default_bundles(self) -> list[str]:
        return config.user_bundles()[:1]

    def list_accounts(self) -> list[dict[str, Any]]:
        return [
            {**a, "provider": self.name, "provider_label": self.label, "tenant_label": "Tenant"}
            for a in auth.list_accounts()
        ]

    def get_token(self, bundle: str, username: str | None = None) -> str:
        return auth.get_token(bundle, username)

    def has_consent(self, bundle: str, username: str | None = None) -> bool:
        # MSAL only tells by trying (get_token raises ConsentRequired), so don't pre-empt it.
        return True

    def invalidate_token(self, username: str | None = None) -> None:
        auth.forget_tokens(username)

    def check_token(self, username: str) -> None:
        auth.get_token("core", username)

    def login_start(self, bundles: list[str], redirect_uri: str | None = None) -> dict[str, Any]:
        """Device-code sign-in; `redirect_uri` is ignored (callback_path is None, so it is never
        passed)."""
        flow = auth.device_flow_start(bundles)
        uri, code = flow.get("verification_uri"), flow.get("user_code")
        return {
            **flow,
            "provider": self.name,
            "kind": "device_code",
            "needs_redirect_url": False,
            "instructions": (
                f"Open {uri} and enter the code {code}, then sign in with your Microsoft "
                "account (work, school or personal) and approve the permissions."
            ),
        }

    def login_complete(
        self,
        flow_id: str,
        data: dict[str, Any],
        wait_seconds: float | None = None,
        redirect_url: str | None = None,
    ) -> dict[str, Any]:
        if redirect_url:
            raise Broker365Error(
                "redirect_url is only for Google sign-in. Microsoft sign-in finishes once the "
                "user enters the code; call again with just flow_id."
            )
        return auth.device_flow_complete(flow_id, wait_seconds=wait_seconds, flow=data)

    def complete_callback(self, params: dict[str, list[str]]) -> dict[str, Any]:
        raise Broker365Error("Microsoft sign-in has no redirect callback")

    def login_interactive(self, bundles: list[str], port: int | None = None) -> dict[str, Any]:
        return auth.login_interactive(bundles, port)

    def logout(self, username: str | None = None) -> str:
        return auth.logout(username)

    def set_default(self, username: str) -> None:
        auth.set_default(username)
