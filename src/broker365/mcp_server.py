"""MCP stdio server exposing broker365.services as tools (`broker365 mcp`).

Registered in Hermes via `mcp_servers.m365` in $HERMES_HOME/config.yaml, so tools surface as
mcp_m365_<tool>. Write tools are hidden in read-only mode; destructive ones are annotated.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from broker365 import __version__, services
from broker365.errors import Broker365Error

INSTRUCTIONS = """\
Microsoft 365 (OneDrive, Excel, Word, PowerPoint, Outlook, To Do) and Google (tools prefixed
g*, e.g. gdrive_*) for the user's signed-in accounts. Each tool's description has the details.
- Files are addressed by absolute path ('/Documents/report.docx'; '/' = root / My Drive) or
  by id. If a path is ambiguous (Google Drive allows duplicate names), use the id it lists.
- Not signed in or a permission missing: call account_login_start with the needed scopes (and
  provider). For kind 'device_code' (Microsoft) show the user the URL and code, then poll
  account_login_complete until status is 'signed_in'. For kind 'authorize_url'
  (provider="google") send the user authorize_url and the instructions; they approve, land on
  a "site can't be reached" page and paste its full address-bar URL back. Call
  account_login_complete(flow_id, redirect_url=<that URL>) and never repeat that URL: it holds
  a one-time code.
- Tools marked destructive (delete, trash, send, share, invite) need the user's explicit
  go-ahead first. Mail send/reply/forward create a draft by default: show it, and after
  approval send exactly that draft with mail_send_draft(draft_id).
- A conflict error means the file changed meanwhile: re-read it and retry; nothing was
  overwritten.
"""


def _entry(t: services.Tool) -> Callable[..., Any]:
    """Run the tool through services.call (read-only and argument checks happen there) with
    t.fn's signature for the schema. Broker errors become ToolError; the MCP SDK hides the
    text of any other exception, which is what we want for genuine crashes."""

    @functools.wraps(t.fn)
    def entry(**kwargs: Any) -> Any:
        try:
            return services.call(t.name, **kwargs)
        except Broker365Error as exc:
            raise ToolError(str(exc)) from exc

    return entry


def build_server() -> MCPServer:
    server = MCPServer("broker365", instructions=INSTRUCTIONS, version=__version__)
    for t in services.available_tools():
        server.add_tool(
            _entry(t),
            name=t.name,
            description=t.description,
            annotations=ToolAnnotations(
                read_only_hint=not t.writes,
                destructive_hint=t.destructive,
                open_world_hint=True,
            ),
        )
    return server


def run() -> None:
    build_server().run("stdio")
