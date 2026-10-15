"""Append-only audit trail of write operations ($BROKER365_HOME/audit.log, JSON lines).

Records which tool ran (or which admin action, as `admin:<action>`, e.g. a settings change or
logout from the CLI or dashboard), when, whether it succeeded, *what it targeted* (ids, paths,
sheet/range, attachment paths, recipient count) and the ids/links it created — never message
bodies, file contents or cell values.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from broker365 import config

# Arguments that identify a target. Anything else (bodies, values, markdown, …) is omitted.
# A tool can name more with @tool(..., audit=(...)).
TARGET_KEYS = (
    "item", "remote_path", "local_path", "dest", "new_parent", "new_name", "path",
    "sheet", "address", "table", "name", "draft_id", "message_id", "destination_folder",
    "event_id", "task_id", "list_id", "contact_id", "site_id", "drive_id",
    "notebook_id", "section_id", "page_id", "save_as", "attachments",
    "link_type", "scope", "confirm",
)  # fmt: skip
RECIPIENT_KEYS = ("to", "cc", "bcc", "attendees", "emails")
# Ids/links of what a write created or touched, taken from the tool's result.
RESULT_KEYS = ("id", "draft_id", "path", "web_url", "url", "monitor_url", "local_path")
MAX_VALUE = 200
_BLOCK = 64 * 1024  # bytes read per step when scanning the log backwards

_lock = threading.Lock()


def record(
    tool: str,
    kwargs: dict[str, Any],
    *,
    result: Any = None,
    error: BaseException | None = None,
    extra_keys: tuple[str, ...] = (),
) -> None:
    """Best effort: this runs after the operation already happened, so a logging problem must
    never surface as a failure (the agent would retry and, e.g., send a mail twice)."""
    try:
        _append(_entry(tool, kwargs, result, error, extra_keys))
    except Exception:  # noqa: BLE001, S110 - deliberately swallowed, see docstring
        pass


def record_admin(action: str, target: dict[str, Any]) -> None:
    """Record an administrative action (settings, sign-in/out, Hermes install). `target`
    holds identifiers only (usernames, paths, flags), never tokens. Best effort, like record."""
    try:
        _append({"ts": _now(), "tool": f"admin:{action}", "ok": True, "target": target})
    except Exception:  # noqa: BLE001, S110 - the action already happened
        pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _clip(value: Any) -> Any:
    return value if isinstance(value, bool) else str(value)[:MAX_VALUE]


def _entry(
    tool: str,
    kwargs: dict[str, Any],
    result: Any,
    error: BaseException | None,
    extra_keys: tuple[str, ...] = (),
) -> dict:
    keys = TARGET_KEYS + tuple(k for k in extra_keys if k not in TARGET_KEYS)
    target = {k: _clip(v) for k in keys if (v := kwargs.get(k)) is not None}
    for key in RECIPIENT_KEYS:
        if isinstance(value := kwargs.get(key), list | tuple) and value:
            target[f"{key}_count"] = len(value)
    entry: dict[str, Any] = {
        "ts": _now(),
        "tool": tool,
        "ok": error is None,
        **({"error": type(error).__name__} if error else {}),
        "target": target,
    }
    if isinstance(result, dict) and (ids := {k: result[k] for k in RESULT_KEYS if result.get(k)}):
        entry["result"] = {k: _clip(v) for k, v in ids.items()}
    return entry


def _append(entry: dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    path = config.broker_home() / "audit.log"
    with _lock:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)


def read(limit: int = 100) -> list[dict[str, Any]]:
    """The newest `limit` entries, newest first; malformed lines are skipped. Reads the file
    backwards in blocks, so the cost depends on `limit`, not on the size of the log."""
    path = config.broker_home() / "audit.log"
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in _lines_from_end(path):
        if len(entries) >= limit:
            break
        try:
            entry = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _lines_from_end(path: Path) -> Iterator[bytes]:
    """Yield the file's lines last to first (without newlines)."""
    with path.open("rb") as f:
        pos = f.seek(0, os.SEEK_END)
        rest = b""  # head of the last block read: possibly an incomplete line
        while pos > 0:
            step = min(_BLOCK, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + rest).split(b"\n")
            rest = lines.pop(0)
            yield from reversed(lines)
        yield rest
