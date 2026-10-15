"""Exception types. Every message is safe to show to the user/agent (no tokens, no content)."""

from __future__ import annotations

import httpx

# Registry key of the default identity provider; config.MICROSOFT is this same value. It lives
# here so error messages can tell it apart without importing config (which imports this module).
DEFAULT_PROVIDER = "microsoft"


class Broker365Error(Exception):
    """Base error; `str(err)` is a user-facing explanation."""


class ReauthRequired(Broker365Error):
    """No usable token for the account; the user must sign in again."""


class ConsentRequired(Broker365Error):
    """The account hasn't granted the scope bundle this operation needs."""

    def __init__(self, bundle: str, provider: str | None = None):
        self.bundle, self.provider = bundle, provider
        flag = f"--provider {provider} " if provider and provider != DEFAULT_PROVIDER else ""
        super().__init__(
            f"Permission '{bundle}' not granted yet. Run: broker365 login {flag}--scopes {bundle}"
        )


class GraphError(Broker365Error):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"Graph {status} {code}: {message}")

    @classmethod
    def from_response(cls, resp: httpx.Response) -> GraphError:
        code, message = "unknown", resp.reason_phrase
        try:
            err = resp.json().get("error", {})
            code = err.get("code", code)
            message = err.get("message", message)
        except ValueError:
            pass
        if resp.status_code == 423:
            message = (
                "the file is locked (open in Excel, or edited moments ago); try again in a "
                f"minute or save under another name. ({message})"
            )
        kind = ConflictError if resp.status_code == 412 else cls
        return kind(resp.status_code, code, message)


class ConflictError(GraphError):
    """The file changed since it was read (eTag mismatch); nothing was overwritten."""


class NetworkError(Broker365Error):
    """Could not reach Microsoft (DNS, TLS, timeout, connection reset)."""


class ReadOnlyMode(Broker365Error):
    def __init__(self, tool: str):
        super().__init__(f"'{tool}' modifies data but broker365 is in read-only mode")


class PathNotAllowed(Broker365Error):
    """A local path lies outside the allowed directories."""
