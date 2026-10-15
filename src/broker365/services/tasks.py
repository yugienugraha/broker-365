"""Microsoft To Do tasks. `list_id` defaults to the user's default 'Tasks' list."""

from __future__ import annotations

from typing import Any, Literal

from broker365 import graph
from broker365.errors import Broker365Error
from broker365.services import tool
from broker365.services._shapes import date_time, date_time_tz, iso_date


def _g() -> graph.GraphClient:
    return graph.client("tasks")


def _summarize_list(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": t.get("id"),
        "name": t.get("displayName"),
        "default": t.get("wellknownListName") == "defaultList",
        "shared": t.get("isShared"),
    }


def _summarize(t: dict[str, Any]) -> dict[str, Any]:
    due = date_time(t, "dueDateTime")
    return {
        "id": t.get("id"),
        "title": t.get("title"),
        "status": t.get("status"),
        "importance": t.get("importance"),
        "due": due[:10] if due else None,
        "body": ((t.get("body") or {}).get("content") or "")[:2000] or None,
        "created": t.get("createdDateTime"),
        "completed": date_time(t, "completedDateTime"),
    }


def _list_path(list_id: str | None) -> str:
    if not list_id:
        lists = _g().iter_pages("/me/todo/lists", limit=500)
        default = next((t for t in lists if t.get("wellknownListName") == "defaultList"), None)
        if default is None:
            raise Broker365Error("No default To Do list found; pass list_id (see tasks_lists)")
        list_id = default["id"]
    return f"/me/todo/lists/{graph.seg(list_id, 'list id')}/tasks"


def _task_path(task_id: str, list_id: str | None) -> str:
    task = graph.seg(task_id, "task id")  # validate before resolving the default list
    return f"{_list_path(list_id)}/{task}"


@tool("tasks")
def tasks_lists() -> list[dict[str, Any]]:
    """List the user's Microsoft To Do task lists."""
    return [_summarize_list(t) for t in _g().paged("/me/todo/lists", limit=500)]


@tool("tasks")
def tasks_list(
    list_id: str | None = None, include_completed: bool = False, limit: int = 50
) -> list[dict[str, Any]]:
    """List tasks in a To Do list (default list when list_id is omitted); open tasks only
    unless include_completed=true."""
    params = {} if include_completed else {"$filter": "status ne 'completed'"}
    return [_summarize(t) for t in _g().paged(_list_path(list_id), params=params, limit=limit)]


@tool("tasks", writes=True)
def tasks_create(
    title: str,
    list_id: str | None = None,
    due: str | None = None,
    body: str | None = None,
    importance: Literal["low", "normal", "high"] = "normal",
) -> dict[str, Any]:
    """Create a To Do task. `due` is a date YYYY-MM-DD; list_id defaults to the default list."""
    task: dict[str, Any] = {"title": title, "importance": importance}
    if due:
        iso_date(due, "due must be a date in YYYY-MM-DD format")
        task["dueDateTime"] = date_time_tz(f"{due}T00:00:00", "UTC")
    if body:
        task["body"] = {"contentType": "text", "content": body}
    return _summarize(_g().post(_list_path(list_id), json=task))


@tool("tasks", writes=True)
def tasks_complete(task_id: str, list_id: str | None = None) -> dict[str, Any]:
    """Mark a To Do task as completed."""
    return _summarize(_g().patch(_task_path(task_id, list_id), json={"status": "completed"}))


@tool("tasks", destructive=True)
def tasks_delete(task_id: str, list_id: str | None = None) -> dict[str, Any]:
    """Delete a To Do task."""
    _g().delete(_task_path(task_id, list_id))
    return {"deleted": True, "id": task_id}
