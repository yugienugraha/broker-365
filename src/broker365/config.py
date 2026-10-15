"""Paths, app identity, scope bundles, persisted settings. See docs/PLAN.md §3 and §5a."""

from __future__ import annotations

import copy
import os
import tempfile
import threading
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path, PureWindowsPath
from typing import Any

import tomli_w

from broker365.errors import Broker365Error, PathNotAllowed

# Public client ID of the publisher's multi-tenant app registration "broker365" (docs/PLAN.md
# §3.1; any Entra tenant + personal accounts, public client, no secret). Not a secret: it
# identifies the app, not a user. Users may override it with BROKER365_CLIENT_ID.
PLACEHOLDER_CLIENT_ID = "00000000-0000-0000-0000-000000000000"
DEFAULT_CLIENT_ID = "5b97899a-d268-4f28-b4e6-3ed7ee60e65f"

# Registry key of the Microsoft identity provider (default_accounts, flow files, audit, tools).
MICROSOFT = "microsoft"

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


def user_bundles() -> list[str]:
    """Bundles a user can choose; `core` is always included implicitly."""
    return [b for b in SCOPE_BUNDLES if b != "core"]


def scopes_for(bundles: list[str]) -> list[str]:
    unknown = [b for b in bundles if b not in SCOPE_BUNDLES]
    if unknown:
        known = ", ".join(user_bundles())
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


def _modify_settings(transform: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Read-modify-write config.toml under the lock; `transform` edits the dict in place.
    Nothing is written when `transform` leaves the settings unchanged."""
    with _settings_lock:
        settings = load_settings()
        before = copy.deepcopy(settings)
        transform(settings)
        if settings != before:
            save_settings(settings)
        return settings


def update_settings(**changes: Any) -> dict[str, Any]:
    """Merge top-level keys into config.toml; a value of None removes the key."""

    def apply(settings: dict[str, Any]) -> None:
        for key, value in changes.items():
            if value is None:
                settings.pop(key, None)
            else:
                settings[key] = value

    return _modify_settings(apply)


def _string_table(value: Any) -> dict[str, str]:
    """The str -> str entries of `value`, or {} when it is not a table."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def _default_accounts(settings: dict[str, Any]) -> dict[str, str]:
    """Per-provider default accounts. A legacy top-level `default_account` (before providers
    existed) counts as Microsoft's default unless `default_accounts.microsoft` is set. A
    hand-edited value of the wrong type is ignored (a non-table `default_accounts`, non-string
    entries, a non-string legacy key)."""
    accounts = {k: v for k, v in _string_table(settings.get("default_accounts")).items() if v}
    legacy = settings.get("default_account")
    if isinstance(legacy, str) and legacy and MICROSOFT not in accounts:
        accounts[MICROSOFT] = legacy
    return accounts


def default_account(provider: str) -> str | None:
    """The default username for `provider`, as stored (compare case-insensitively)."""
    return _default_accounts(load_settings()).get(provider)


def set_default_account(provider: str, username: str | None, *, if_unset: bool = False) -> None:
    """Set (or with None, clear) `provider`'s default account under the settings lock. With
    `if_unset`, only set it when the provider has no default yet. A change also migrates a
    legacy `default_account` key into `default_accounts`; no change writes nothing."""

    def apply(settings: dict[str, Any]) -> None:
        current = _default_accounts(settings)
        accounts = dict(current)
        if username is None:
            accounts.pop(provider, None)
        elif not (if_unset and accounts.get(provider)):
            accounts[provider] = username
        stored = settings.get("default_accounts")
        malformed = stored is not None and _string_table(stored) != stored
        if accounts == current and not malformed:
            return  # a malformed stored value is rewritten as a proper table
        settings.pop("default_account", None)
        if accounts:
            settings["default_accounts"] = accounts
        else:
            settings.pop("default_accounts", None)

    _modify_settings(apply)


def set_excel_backend(username: str, backend: str) -> None:
    """Cache the Excel backend chosen for a personal account (locked read-modify-write)."""

    def apply(settings: dict[str, Any]) -> None:
        settings["excel_backend"] = {**settings.get("excel_backend", {}), username: backend}

    _modify_settings(apply)


def _allowable_dir(path: str) -> str:
    """Validate a folder the user asks to allow and return its normalized form."""
    p = Path(path).expanduser()
    if _is_remote(str(p)):
        # Checked before anything touches the path: resolve() on \host\share leaks NTLM.
        raise Broker365Error("Network shares and device paths can't be allowed folders")
    if not p.is_absolute():
        raise Broker365Error(f"Allowed folders must be absolute paths: {path}")
    if not p.is_dir():
        raise Broker365Error(f"Not an existing folder: {path}")
    return str(normalize_dir(str(p)))


def _same_dir(stored: str, requested: str, requested_norm: str | None) -> bool:
    """Whether a stored allowed_dirs entry is the one the user asked to remove: equal as
    written, or equal once normalized (remote/unresolvable entries compare as written only)."""
    if stored == requested:
        return True
    if requested_norm is None or _is_remote(str(Path(stored).expanduser())):
        return False
    try:
        return str(normalize_dir(stored)) == requested_norm
    except (OSError, RuntimeError, Broker365Error):
        return False


def change_settings(
    read_only: bool | None = None,
    add_dirs: Iterable[str] = (),
    remove_dirs: Iterable[str] = (),
    reset_excel_probe: bool = False,
) -> dict[str, Any]:
    """Apply user-facing settings changes atomically. Returns what actually changed
    (`read_only`, `dirs_added`, `dirs_removed`, `excel_probe_reset`), e.g. for the audit log."""
    add = {_allowable_dir(d) for d in add_dirs if d}
    remove: list[tuple[str, str | None]] = []
    for d in remove_dirs:
        if not d:
            continue
        norm = None
        if not _is_remote(str(Path(d).expanduser())):
            try:
                norm = str(normalize_dir(d))
            except (OSError, RuntimeError):
                pass
        remove.append((d, norm))
    changed: dict[str, Any] = {}

    def apply(settings: dict[str, Any]) -> None:
        if read_only is not None:
            if bool(settings.get("read_only", False)) != read_only:
                changed["read_only"] = read_only
            settings["read_only"] = read_only
        old = set(settings.get("allowed_dirs", []))
        new = old | add
        new = {s for s in new if not any(_same_dir(s, raw, norm) for raw, norm in remove)}
        if new != old:
            if added := sorted(new - old):
                changed["dirs_added"] = added
            if removed := sorted(old - new):
                changed["dirs_removed"] = removed
        if new:
            settings["allowed_dirs"] = sorted(new)
        else:
            settings.pop("allowed_dirs", None)
        if reset_excel_probe and settings.pop("excel_backend", None) is not None:
            changed["excel_probe_reset"] = True

    _modify_settings(apply)
    return changed


def settings_view() -> dict[str, Any]:
    """Effective settings as shown by `broker365 settings` and the dashboard."""
    settings = load_settings()
    env = env_flag("BROKER365_READ_ONLY")
    return {
        "read_only": env if env is not None else bool(settings.get("read_only", False)),
        "read_only_env": env is not None,
        "allowed_dirs": list(settings.get("allowed_dirs", [])),
        "workspace": str(workspace()),
        "excel_backend": dict(settings.get("excel_backend", {})),
    }


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
    return [workspace()] + [_root(d) for d in extra if d]


def _root(path: str) -> Path:
    """An allowed_dirs entry as a root. A share listed by hand (config.toml / env) is kept
    lexical (never resolved, so never contacted here); everything else is normalized."""
    p = Path(path).expanduser()
    return Path(os.path.normpath(p)) if _is_remote(str(p)) else normalize_dir(path)


def normalize_dir(path: str) -> Path:
    """Absolute, symlink-free form of a local folder. Refuses UNC/device/drive-relative paths:
    resolving one would make Windows contact that host (sending NTLM credentials)."""
    p = Path(path).expanduser()
    if _is_remote(str(p)):
        raise PathNotAllowed(f"UNC, device and drive-relative paths are not allowed: {p}")
    return p.resolve()


def resolve_local(path: str | None, default_name: str | None = None) -> Path:
    """Resolve a local path (relative paths are relative to the workspace) and ensure it
    lies inside an allowed directory, following symlinks. Guards against an agent being
    steered into reading ~/.ssh or overwriting dotfiles."""
    if not path:
        if not default_name:
            raise PathNotAllowed("A local path is required")
        path = default_name
    roots = allowed_dirs()  # roots[0] is the workspace
    p = Path(path).expanduser()
    if _is_remote(str(p)):
        # resolve() on a UNC path makes Windows contact that host (sending NTLM credentials)
        # before any allow-list check could reject it, so only a lexical check is safe here.
        lexical = Path(os.path.normpath(p))
        if not any(_is_remote(str(r)) and lexical.is_relative_to(r) for r in roots):
            raise PathNotAllowed(f"UNC, device and drive-relative paths are not allowed: {p}")
    elif not p.is_absolute():
        p = roots[0] / p
    p = p.resolve()
    if any(p.is_relative_to(root) for root in roots):
        return p
    raise PathNotAllowed(
        f"Local path {p} is outside the allowed directories ({', '.join(map(str, roots))}). "
        "Add it to allowed_dirs in config.toml or BROKER365_ALLOWED_DIRS."
    )


_WINDOWS = os.name == "nt"


def _is_remote(path: str) -> bool:
    r"""Windows UNC (\\host\share, //host/share), device (\\?\, \\.\) or drive-relative
    ('C:foo') path. Always False elsewhere, where these are ordinary file names."""
    if not _WINDOWS:
        return False
    if path[:1] in ("\\", "/") and path[1:2] in ("\\", "/"):
        return True
    w = PureWindowsPath(path)
    return bool(w.drive) and not w.root
