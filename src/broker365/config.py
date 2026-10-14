"""Paths, app identity, scope bundles, persisted settings. See docs/PLAN.md §3 and §5a."""

from __future__ import annotations

import os
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from broker365.errors import Broker365Error, PathNotAllowed

# Public client ID of the publisher's multi-tenant app registration (docs/PLAN.md §3.1).
# Not a secret. Fill in after creating the Entra app; users may override with BROKER365_CLIENT_ID.
PLACEHOLDER_CLIENT_ID = "00000000-0000-0000-0000-000000000000"
DEFAULT_CLIENT_ID = PLACEHOLDER_CLIENT_ID

AUTHORITY = "https://login.microsoftonline.com/common"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Tenant ID that identifies personal Microsoft accounts (Outlook.com, Hotmail).
MSA_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"

# Incremental-consent bundles. MSAL adds openid/profile/offline_access automatically.
SCOPE_BUNDLES: dict[str, list[str]] = {
    "core": ["User.Read"],
    "files": ["Files.ReadWrite"],
    "files-shared": ["Files.ReadWrite.All"],
    "sharepoint": ["Sites.ReadWrite.All"],
    "mail": ["Mail.ReadWrite", "Mail.Send"],
    "calendar": ["Calendars.ReadWrite"],
    "contacts": ["Contacts.ReadWrite"],
    "tasks": ["Tasks.ReadWrite"],
    "onenote": ["Notes.ReadWrite"],
}


def env_flag(name: str) -> bool | None:
    """True/False for a set boolean-ish env var, None when unset."""
    val = os.environ.get(name, "").strip().lower()
    return val in ("1", "true", "yes") if val else None


def client_id() -> str:
    return os.environ.get("BROKER365_CLIENT_ID", "").strip() or DEFAULT_CLIENT_ID


def broker_home() -> Path:
    """BROKER365_HOME → $HERMES_HOME/broker365 → ~/.broker365 (created with mode 700)."""
    if val := os.environ.get("BROKER365_HOME", "").strip():
        home = Path(val)
    elif val := os.environ.get("HERMES_HOME", "").strip():
        home = Path(val) / "broker365"
    else:
        home = Path.home() / ".broker365"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return home


def scopes_for(bundles: list[str]) -> list[str]:
    unknown = [b for b in bundles if b not in SCOPE_BUNDLES]
    if unknown:
        known = ", ".join(b for b in SCOPE_BUNDLES if b != "core")
        raise Broker365Error(f"Unknown scope bundle(s): {', '.join(unknown)}. Known: {known}")
    scopes: list[str] = list(SCOPE_BUNDLES["core"])
    for b in bundles:
        scopes += [s for s in SCOPE_BUNDLES[b] if s not in scopes]
    return scopes


# ---- persisted settings (config.toml) -------------------------------------------------


def _settings_path() -> Path:
    return broker_home() / "config.toml"


def load_settings() -> dict[str, Any]:
    path = _settings_path()
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


_settings_lock = threading.Lock()  # MCP runs sync tools in worker threads


def save_settings(settings: dict[str, Any]) -> None:
    path = _settings_path()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="config.", suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(tomli_w.dumps(settings).encode())
    os.replace(tmp, path)


def update_settings(**changes: Any) -> dict[str, Any]:
    """Merge top-level keys into config.toml; a value of None removes the key."""
    with _settings_lock:
        settings = load_settings()
        for key, value in changes.items():
            if value is None:
                settings.pop(key, None)
            else:
                settings[key] = value
        save_settings(settings)
        return settings


def read_only() -> bool:
    env = env_flag("BROKER365_READ_ONLY")
    return env if env is not None else bool(load_settings().get("read_only", False))


# ---- local filesystem sandbox ---------------------------------------------------------


def workspace() -> Path:
    """Default local directory for downloads/uploads."""
    if val := os.environ.get("BROKER365_WORKSPACE", "").strip():
        ws = Path(val).expanduser()
    else:
        ws = broker_home() / "files"
    ws.mkdir(parents=True, exist_ok=True)
    return ws.resolve()


def allowed_dirs() -> list[Path]:
    """Workspace plus any `allowed_dirs` from config.toml / BROKER365_ALLOWED_DIRS."""
    extra: list[str] = list(load_settings().get("allowed_dirs", []))
    if env := os.environ.get("BROKER365_ALLOWED_DIRS", "").strip():
        extra += env.split(os.pathsep)
    return [workspace()] + [normalize_dir(d) for d in extra if d]


def normalize_dir(path: str) -> Path:
    return Path(path).expanduser().resolve()


def resolve_local(path: str | None, default_name: str | None = None) -> Path:
    """Resolve a local path (relative paths are relative to the workspace) and ensure it
    lies inside an allowed directory, following symlinks. Guards against an agent being
    steered into reading ~/.ssh or overwriting dotfiles."""
    if not path:
        if not default_name:
            raise PathNotAllowed("A local path is required")
        path = default_name
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = workspace() / p
    p = p.resolve()
    roots = allowed_dirs()
    if any(p.is_relative_to(root) for root in roots):
        return p
    raise PathNotAllowed(
        f"Local path {p} is outside the allowed directories ({', '.join(map(str, roots))}). "
        "Add it to allowed_dirs in config.toml or BROKER365_ALLOWED_DIRS."
    )
