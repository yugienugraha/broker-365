"""Append-only audit trail of write operations ($BROKER365_HOME/audit.log, JSON lines).

Records which tool ran, when, whether it succeeded, *what it targeted* (ids, paths,
sheet/range, attachment paths, recipient count) and the ids/links it created — never message
bodies, file contents or cell values.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from broker365 import config

# Arguments that identify a target. Anything else (bodies, values, markdown, …) is omitted.
TARGET_KEYS = (
    "item", "remote_path", "local_path", "dest", "new_parent", "new_name", "path",
    "sheet", "address", "table", "name", "draft_id", "message_id", "destination_folder",
    "event_id", "task_id", "list_id", "contact_id", "site_id", "drive_id",
    "notebook_id", "section_id", "page_id", "save_as", "attachments",
    "link_type", "scope", "confirm",
)  # fmt: skip
RECIPIENT_KEYS = ("to", "cc", "attendees")
# Ids/links of what a write created or touched, taken from the tool's result.
RESULT_KEYS = ("id", "draft_id", "path", "web_url", "url", "monitor_url", "local_path")
MAX_VALUE = 200

_lock = threading.Lock()


def record(
    tool: str, kwargs: dict[str, Any], *, result: Any = None, error: BaseException | None = None
) -> None:
    """Best effort: this runs after the operation already happened, so a logging problem must
    never surface as a failure (the agent would retry and, e.g., send a mail twice)."""
    try:
        _append(_entry(tool, kwargs, result, error))
    except Exception:  # noqa: BLE001, S110 - deliberately swallowed, see docstring
        pass


def _clip(value: Any) -> Any:
    return value if isinstance(value, bool) else str(value)[:MAX_VALUE]


def _entry(tool: str, kwargs: dict[str, Any], result: Any, error: BaseException | None) -> dict:
    target = {k: _clip(v) for k in TARGET_KEYS if (v := kwargs.get(k)) is not None}
    for key in RECIPIENT_KEYS:
        if isinstance(value := kwargs.get(key), list | tuple) and value:
            target[f"{key}_count"] = len(value)
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
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
