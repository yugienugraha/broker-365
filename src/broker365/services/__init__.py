"""One module per Microsoft 365 / Google product. Plain sync functions returning JSON-able values,
registered with `@tool` so the CLI (`broker365 call`) and the MCP server expose the same set.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from broker365 import audit, config, providers
from broker365.errors import Broker365Error, ConsentRequired, ReadOnlyMode

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
    "gdrive",
    "gsheets",
    "gdocs",
    "gslides",
    "gmail",
    "gcalendar",
    "gtasks",
    "gcontacts",
)


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., Any]
    bundle: str
    writes: bool
    destructive: bool
    provider: str = config.MICROSOFT
    audit: tuple[str, ...] = ()  # argument names logged besides audit.TARGET_KEYS
    requires: tuple[str, ...] = ()  # other bundles of `provider` the tool needs

    @property
    def description(self) -> str:
        return inspect.getdoc(self.fn) or self.name


TOOLS: dict[str, Tool] = {}


def tool(
    bundle: str,
    provider: str = config.MICROSOFT,
    *,
    writes: bool = False,
    destructive: bool = False,
    audit: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
):
    """Register a service function. `bundle` is a scope bundle of `provider`. `writes` tools
    are hidden in read-only mode; `destructive` tools (delete/send/share/overwrite) are
    flagged to MCP clients. `audit` names extra arguments that identify a write's target
    (logged in addition to audit.TARGET_KEYS; never content). `requires` lists other bundles
    the tool always needs; consent for all of them is checked before it runs, so it can't
    stop halfway (e.g. after creating a file)."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS[fn.__name__] = Tool(
            fn.__name__,
            fn,
            bundle,
            writes or destructive,
            destructive,
            provider,
            audit,
            requires,
        )
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


def tool_catalog() -> list[dict[str, Any]]:
    """Every registered tool with its flags; `hidden` marks tools agents can't see right now
    (write tools in read-only mode)."""
    visible = {t.name for t in available_tools()}
    return [
        {
            "name": t.name,
            "provider": t.provider,
            "bundle": t.bundle,
            "requires": list(t.requires),
            "writes": t.writes,
            "destructive": t.destructive,
            "description": t.description.splitlines()[0],
            "hidden": t.name not in visible,
        }
        for t in load_all().values()
    ]


def require_consent(provider: str, bundles: Iterable[str]) -> None:
    """ConsentRequired for the first of `bundles` the provider's account hasn't granted.
    Used for a tool's static `requires` and by tools whose needs depend on their arguments,
    so a multi-step tool fails before its first step instead of halfway."""
    p = providers.get(provider)
    for bundle in bundles:
        if not p.has_consent(bundle):
            raise ConsentRequired(bundle, provider)


def call(name: str, /, **kwargs: Any) -> Any:
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
    if t.requires:
        require_consent(t.provider, (t.bundle, *t.requires))
    try:
        result = t.fn(**kwargs)
    except Exception as exc:
        if t.writes:
            audit.record(name, kwargs, error=exc, extra_keys=t.audit)
        raise
    if t.writes:
        audit.record(name, kwargs, result=result, extra_keys=t.audit)
    return result
