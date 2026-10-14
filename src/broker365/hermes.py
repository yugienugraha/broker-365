"""Hermes Agent integration: register the MCP server in $HERMES_HOME/config.yaml and install
the companion skill. Edits config.yaml textually (after a timestamped backup) so the user's
comments and formatting survive."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

from broker365 import audit, config
from broker365.errors import Broker365Error

SERVER_KEY = "m365"
SKILL_DIR = Path("skills") / "productivity" / "microsoft-365"
_TOP_KEY = re.compile(r"^mcp_servers:\s*(\{\s*\})?\s*(#.*)?$")


def hermes_home(explicit: str | None = None) -> Path:
    if explicit:
        if not (Path(explicit) / "config.yaml").exists():
            raise Broker365Error(f"No config.yaml in {explicit}")
        return Path(explicit)
    candidates = [os.environ.get("HERMES_HOME"), str(Path.home() / ".hermes")]
    if local := os.environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(local) / "hermes"))
    for c in candidates:
        if c and (Path(c) / "config.yaml").exists():
            return Path(c)
    raise Broker365Error("Hermes config.yaml not found. Pass --hermes-home or set HERMES_HOME.")


def server_block(indent: int = 2) -> str:
    """YAML for our mcp_servers entry. Values are JSON-quoted, which YAML parses as plain
    strings, so Windows paths need no escaping tricks."""
    env = {"BROKER365_HOME": str(config.broker_home())}
    if cid := os.environ.get("BROKER365_CLIENT_ID"):
        env["BROKER365_CLIENT_ID"] = cid
    a, b, c = (" " * indent * n for n in (1, 2, 3))
    lines = [
        f"{a}{SERVER_KEY}:",
        f"{b}command: {json.dumps(sys.executable)}",
        f'{b}args: ["-m", "broker365", "mcp"]',
        f"{b}timeout: 180",
        f"{b}env:",
        *[f"{c}{k}: {json.dumps(v)}" for k, v in env.items()],
    ]
    return "\n".join(lines) + "\n"


def _section(lines: list[str]) -> tuple[int, list[str]] | None:
    """(index of the `mcp_servers:` line, the lines belonging to it) or None."""
    for i, line in enumerate(lines):
        if _TOP_KEY.match(line.rstrip("\r\n")):
            body = []
            for nxt in lines[i + 1 :]:
                if nxt.strip() and not nxt[0].isspace() and not nxt.startswith("#"):
                    break
                body.append(nxt)
            return i, body
    return None


def _indent(body: list[str]) -> int:
    for line in body:
        if line.strip() and not line.lstrip().startswith("#"):
            return len(line) - len(line.lstrip(" "))
    return 2


def _has_server(text: str) -> bool:
    found = _section(text.splitlines())
    if not found:
        return False
    ind = _indent(found[1])
    return any(re.match(rf"^ {{{ind}}}{SERVER_KEY}:\s*$", line) for line in found[1])


def patched_config(text: str) -> str:
    lines = text.splitlines(keepends=True)
    found = _section(lines)
    if found:
        i, body = found
        lines[i] = "mcp_servers:\n"
        return "".join(lines[: i + 1]) + server_block(_indent(body)) + "".join(lines[i + 1 :])
    sep = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{sep}\nmcp_servers:\n{server_block()}"


def status(home: str | None = None) -> dict[str, Any]:
    """Whether Hermes is found and has our MCP server entry and skill installed."""
    try:
        hh = hermes_home(home)
        text = (hh / "config.yaml").read_text(encoding="utf-8", errors="replace")
    except (Broker365Error, OSError):
        return {"found": False, "home": None, "mcp_server": False, "skill": False}
    try:
        has_server = _has_server(text)
    except (UnicodeDecodeError, ValueError):  # unparsable config.yaml: report, don't crash
        has_server = False
    return {
        "found": True,
        "home": str(hh),
        "mcp_server": has_server,
        "skill": (hh / SKILL_DIR / "SKILL.md").exists(),
    }


def install(home: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    hh = hermes_home(home)
    cfg = hh / "config.yaml"
    text = cfg.read_text(encoding="utf-8")
    result: dict[str, Any] = {"hermes_home": str(hh), "config": str(cfg)}
    if _has_server(text):
        result["mcp_server"] = "already present (left unchanged)"
    elif dry_run:
        result["mcp_server"] = "would add:\n" + server_block()
    else:
        backup = cfg.with_name(f"config.yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(cfg, backup)
        cfg.write_text(patched_config(text), encoding="utf-8")
        result.update(mcp_server="added", backup=str(backup))
    skill_dest = hh / SKILL_DIR
    if not dry_run:
        skill_dest.mkdir(parents=True, exist_ok=True)
        src = resources.files("broker365") / "hermes_skill" / "SKILL.md"
        (skill_dest / "SKILL.md").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    result["skill"] = str(skill_dest / "SKILL.md")
    if not dry_run:
        audit.record_admin("hermes_install", {"hermes_home": str(hh)})
    result["next"] = "Restart Hermes so it connects to the m365 MCP server."
    return result
