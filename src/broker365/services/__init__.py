"""One module per Microsoft 365 product. Plain sync functions returning JSON-able values,
registered with `@tool` so the CLI (`broker365 call`) and the MCP server expose the same set.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from broker365 import audit, config
from broker365.errors import Broker365Error, ReadOnlyMode

SERVICE_MODULES = (
    "account",
    "files",
    "excel",
    "word",
    "powerpoint",
    "mail",
    "calendar",
    "contacts",
    "tasks",
    "sharepoint",
    "onenote",
)


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., Any]
    bundle: str
    writes: bool
    destructive: bool

    @property
    def description(self) -> str:
        return inspect.getdoc(self.fn) or self.name


TOOLS: dict[str, Tool] = {}


def tool(bundle: str, *, writes: bool = False, destructive: bool = False):
    """Register a service function. `writes` tools are hidden in read-only mode;
    `destructive` tools (delete/send/share/overwrite) are flagged to MCP clients."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS[fn.__name__] = Tool(fn.__name__, fn, bundle, writes or destructive, destructive)
        return fn

    return deco


def load_all() -> dict[str, Tool]:
    for mod in SERVICE_MODULES:
        importlib.import_module(f"broker365.services.{mod}")
    return TOOLS


def available_tools() -> list[Tool]:
    tools = load_all().values()
    if config.read_only():
        return [t for t in tools if not t.writes]
    return list(tools)


def call(name: str, **kwargs: Any) -> Any:
    tools = load_all()
    if name not in tools:
        raise Broker365Error(f"Unknown tool '{name}'. List them with: broker365 tools")
    t = tools[name]
    if t.writes and config.read_only():
        raise ReadOnlyMode(name)
    try:
        inspect.signature(t.fn).bind(**kwargs)
    except TypeError as exc:
        raise Broker365Error(f"Bad arguments for {name}: {exc}") from None
    try:
        result = t.fn(**kwargs)
    except Exception as exc:
        if t.writes:
            audit.record(name, kwargs, error=exc)
        raise
    if t.writes:
        audit.record(name, kwargs, result=result)
    return result
