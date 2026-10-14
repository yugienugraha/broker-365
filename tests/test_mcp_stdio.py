"""End-to-end: spawn `python -m broker365 mcp` exactly as Hermes would and talk MCP over stdio."""

import asyncio
import json
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def _session(env: dict[str, str], fn):
    params = StdioServerParameters(command=sys.executable, args=["-m", "broker365", "mcp"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return await fn(session)


def _env(tmp_path, **extra):
    return {
        "BROKER365_HOME": str(tmp_path / "mcp-home"),
        "BROKER365_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
        "BROKER365_PLAINTEXT_CACHE": "1",
        "PYTHONPATH": os.pathsep.join(sys.path),
        **extra,
    }


def test_stdio_lists_tools_and_reports_not_signed_in(tmp_path):
    async def run(session):
        tools = (await session.list_tools()).tools
        status = await session.call_tool("account_status", {})
        files = await session.call_tool("files_list", {"path": "/"})
        return tools, status, files

    tools, status, files = asyncio.run(_session(_env(tmp_path), run))
    names = {t.name for t in tools}
    assert {"files_list", "excel_write_range", "word_create", "mail_send"} <= names
    payload = json.loads(status.content[0].text)
    assert payload["signed_in"] is False
    assert files.is_error and "broker365 login" in files.content[0].text


def test_stdio_read_only_hides_write_tools(tmp_path):
    async def run(session):
        return {t.name for t in (await session.list_tools()).tools}

    names = asyncio.run(_session(_env(tmp_path, BROKER365_READ_ONLY="1"), run))
    assert "files_list" in names and "files_delete" not in names and "mail_send" not in names
