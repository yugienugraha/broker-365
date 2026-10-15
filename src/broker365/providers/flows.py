"""Pending sign-in flows, shared by every provider.

A flow started in one process (a chat tool call, the dashboard) may be finished in another, so
its state lives in `flows/<id>.json` under the broker home: a random id, a mode-600 file, and
cleanup after an hour. The data is opaque here (MSAL's device flow, a PKCE verifier...); the file
also records which provider started it so completion can be dispatched.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from broker365 import config
from broker365.errors import Broker365Error

MAX_AGE = 3600  # seconds a flow file is kept
_FLOW_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _dir() -> Path:
    flows = config.broker_home() / "flows"
    flows.mkdir(exist_ok=True, mode=0o700)
    return flows


def _path(flow_id: str) -> Path:
    if not isinstance(flow_id, str) or not _FLOW_ID.fullmatch(flow_id):
        raise Broker365Error("Invalid flow id")
    return _dir() / f"{flow_id}.json"


def cleanup(max_age: float = MAX_AGE) -> None:
    """Delete flow files older than `max_age` seconds."""
    now = time.time()
    for f in [*_dir().glob("*.json"), *_dir().glob(".*.tmp")]:
        try:
            if now - f.stat().st_mtime > max_age:
                f.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - raced with another process
            pass


def new_id() -> str:
    """A fresh random flow id, for providers that must know the id before saving (e.g. to
    embed it in an OAuth `state`)."""
    return secrets.token_urlsafe(12)


def _write(flow_id: str, provider: str, data: dict[str, Any]) -> None:
    """Write a flow file atomically: a private temp file renamed into place, so a crash (or a
    concurrent reader) never sees a partial flow."""
    path = _path(flow_id)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{flow_id}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            tmp.chmod(0o600)  # mkstemp already uses 600 on POSIX
        except OSError:  # pragma: no cover - best effort on Windows
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"provider": provider, "data": data}, f)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save(provider: str, data: dict[str, Any], flow_id: str | None = None) -> str:
    """Store a new flow for `provider` and return its id (`flow_id` from `new_id()`, or a
    fresh one)."""
    cleanup()
    flow_id = flow_id or new_id()
    _write(flow_id, provider, data)
    return flow_id


def update(flow_id: str, provider: str, data: dict[str, Any]) -> None:
    """Replace the data of a pending flow (atomically). A flow that was already consumed is
    not brought back: that raises like `load`."""
    if not _path(flow_id).exists():
        raise Broker365Error("Unknown or already-used flow id. Start again: broker365 login")
    _write(flow_id, provider, data)


def load(flow_id: str) -> tuple[str, dict[str, Any]]:
    """(provider, data) of a pending flow. Files from before providers existed hold MSAL's flow
    dict directly and belong to Microsoft."""
    path = _path(flow_id)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Broker365Error(
            "Unknown or already-used flow id. Start again: broker365 login"
        ) from None
    except (OSError, ValueError):
        raise Broker365Error("Unreadable sign-in flow. Start again: broker365 login") from None
    if not isinstance(stored, dict):
        raise Broker365Error("Unreadable sign-in flow. Start again: broker365 login")
    if "provider" in stored and isinstance(stored.get("data"), dict):
        return str(stored["provider"]), stored["data"]
    legacy = {k: v for k, v in stored.items() if k != "provider"}
    return str(stored.get("provider") or config.MICROSOFT), legacy


def consume(flow_id: str) -> None:
    """Delete a finished (or abandoned) flow."""
    _path(flow_id).unlink(missing_ok=True)
