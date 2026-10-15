"""Google Tasks (Tasks v1) for a connected Google account: the gtasks_* tools. `list_id`
defaults to the user's default list ('@default')."""

from __future__ import annotations

from typing import Any

from broker365 import config, gapi
from broker365.errors import Broker365Error
from broker365.services import tool
from broker365.services._shapes import compact, iso_date
from broker365.transport import seg

BUNDLE = "gtasks"
GOOGLE = config.GOOGLE
TASKS = "https://tasks.googleapis.com/tasks/v1"
DEFAULT_LIST = "@default"


def _g() -> gapi.GoogleClient:
    return gapi.client(BUNDLE)


def _list_url(list_id: str) -> str:
    # Google's alias for the default list is used as is; real ids are quoted.
    ref = DEFAULT_LIST if list_id == DEFAULT_LIST else seg(list_id, "list id")
    return f"{TASKS}/lists/{ref}/tasks"


def _summarize_list(t: dict[str, Any]) -> dict[str, Any]:
    return {"id": t.get("id"), "name": t.get("title"), "updated": t.get("updated")}


def _summarize(t: dict[str, Any]) -> dict[str, Any]:
    due, done = t.get("due"), t.get("completed")
    return {
        "id": t.get("id"),
        "title": t.get("title"),
        "status": "completed" if t.get("status") == "completed" else "notStarted",
        "due": due[:10] if due else None,
        "body": (t.get("notes") or "")[:2000] or None,
        "updated": t.get("updated"),
        "completed": done,
        "web_link": t.get("webViewLink"),
    }


@tool(BUNDLE, GOOGLE)
def gtasks_lists() -> list[dict[str, Any]]:
    """List the user's Google Tasks lists."""
    lists = _g().paged(f"{TASKS}/users/@me/lists", None, "items", 500)
    return [_summarize_list(t) for t in lists]


@tool(BUNDLE, GOOGLE)
def gtasks_list(
    list_id: str = DEFAULT_LIST, include_completed: bool = False, limit: int = 100
) -> list[dict[str, Any]]:
    """List tasks in a Google Tasks list (the default list unless list_id is given); open
    tasks only unless include_completed=true."""
    params = (
        {"showCompleted": "true", "showHidden": "true"}
        if include_completed
        else {"showCompleted": "false"}
    )
    tasks = _g().paged(_list_url(list_id), params, "items", limit)
    return [_summarize(t) for t in tasks]


@tool(BUNDLE, GOOGLE, writes=True)
def gtasks_create(
    title: str, notes: str | None = None, due: str | None = None, list_id: str = DEFAULT_LIST
) -> dict[str, Any]:
    """Create a Google task. `due` is a date YYYY-MM-DD (Google keeps only the date)."""
    if not title.strip():
        raise Broker365Error("A task title is required")
    if due:
        iso_date(due, "due must be a date in YYYY-MM-DD format")
    task = compact(
        {"title": title, "due": f"{due}T00:00:00.000Z" if due else None, "notes": notes or None}
    )
    return _summarize(_g().post(_list_url(list_id), json=task))


@tool(BUNDLE, GOOGLE, writes=True)
def gtasks_complete(task_id: str, list_id: str = DEFAULT_LIST) -> dict[str, Any]:
    """Mark a Google task as completed."""
    url = f"{_list_url(list_id)}/{seg(task_id, 'task id')}"
    return _summarize(_g().patch(url, json={"status": "completed"}, idempotent=True))
