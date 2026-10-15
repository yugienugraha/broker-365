"""OneNote notebooks, sections and pages."""

from __future__ import annotations

import html
import re
from typing import Any, Literal

from broker365 import graph
from broker365.errors import Broker365Error, GraphError
from broker365.graph import seg
from broker365.services import tool
from broker365.services._shapes import html_to_text


def _g() -> graph.GraphClient:
    return graph.client("onenote")


def _web_url(obj: dict[str, Any]) -> str | None:
    return ((obj.get("links") or {}).get("oneNoteWebUrl") or {}).get("href")


def _page(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p.get("id"),
        "title": p.get("title"),
        "created": p.get("createdDateTime"),
        "modified": p.get("lastModifiedDateTime"),
        "web_url": _web_url(p),
    }


# Formatting tags allowed in body_type='html', without attributes: OneNote's servers fetch
# remote resources (img src, data-render-src, object data), which a prompt-injected agent
# could use to send data to an attacker, so everything else is shown as escaped text.
_ALLOWED_TAGS = "p|br|b|i|u|strong|em|h[1-6]|ul|ol|li|table|tr|td|th|thead|tbody|pre|code"
_SAFE_TAG = re.compile(rf"&lt;(/?)({_ALLOWED_TAGS})\s*/?&gt;", re.I)


def _safe_html(markup: str) -> str:
    return _SAFE_TAG.sub(lambda m: f"<{m[1]}{m[2].lower()}>", html.escape(markup, quote=False))


def _text_to_html(text: str) -> str:
    """Plain text as escaped paragraphs (split on blank lines); single newlines become <br/>."""
    paras = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in paras if p)


@tool("onenote")
def onenote_notebooks(limit: int = 50) -> list[dict[str, Any]]:
    """List the user's OneNote notebooks."""
    return [
        {
            "id": n.get("id"),
            "name": n.get("displayName"),
            "modified": n.get("lastModifiedDateTime"),
            "web_url": _web_url(n),
        }
        for n in _g().paged("/me/onenote/notebooks", limit=limit)
    ]


@tool("onenote")
def onenote_sections(notebook_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """List OneNote sections, in one notebook or across all notebooks."""
    path = _scoped("sections", "notebooks", notebook_id)
    return [
        {
            "id": s.get("id"),
            "name": s.get("displayName"),
            "notebook": (s.get("parentNotebook") or {}).get("displayName"),
        }
        for s in _g().paged(path, limit=limit)
    ]


@tool("onenote")
def onenote_pages(section_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """List OneNote pages in one section (preferred), or across all notebooks. Large
    notebooks can't be listed all at once; then pass a section_id from onenote_sections."""
    path = _scoped("pages", "sections", section_id)
    params = {"$select": "id,title,createdDateTime,lastModifiedDateTime,links"}
    try:
        return [_page(p) for p in _g().paged(path, params=params, limit=limit)]
    except GraphError as e:
        if section_id or e.status not in (400, 403):
            raise
        raise Broker365Error(
            f"Listing all pages failed ({e.code}: {e.message}). Too many sections to list at "
            "once; call onenote_sections and pass a section_id."
        ) from e


def _scoped(child: str, parent_kind: str, parent_id: str | None) -> str:
    """/me/onenote/{child} across everything, or under one parent when an id is given."""
    if not parent_id:
        return f"/me/onenote/{child}"
    return f"/me/onenote/{parent_kind}/{seg(parent_id, parent_kind[:-1] + ' id')}/{child}"


@tool("onenote")
def onenote_read_page(page_id: str, max_chars: int = 50_000) -> dict[str, Any]:
    """Read a OneNote page as plain text."""
    content = _g().request("GET", f"/me/onenote/pages/{seg(page_id, 'page id')}/content").text
    text = html_to_text(content)
    return {"id": page_id, "text": text[:max_chars], "truncated": len(text) > max_chars}


@tool("onenote", writes=True)
def onenote_create_page(
    section_id: str, title: str, body: str, body_type: Literal["text", "html"] = "text"
) -> dict[str, Any]:
    """Create a OneNote page in a section. `body` is plain text, or simple HTML when
    body_type='html' (p, br, b, i, u, h1-h6, ul/ol/li, table, pre; attributes, links and
    images are not allowed and appear as text)."""
    if not title.strip():
        raise Broker365Error("A page title is required")
    inner = _safe_html(body) if body_type == "html" else _text_to_html(body)
    doc = (
        "<!DOCTYPE html>\n<html><head>"
        f"<title>{html.escape(title)}</title>"
        f"</head><body>{inner}</body></html>"
    )
    resp = _g().request(
        "POST",
        f"/me/onenote/sections/{seg(section_id, 'section id')}/pages",
        content=doc.encode("utf-8"),
        headers={"Content-Type": "text/html"},
    )
    page = resp.json()
    return {"id": page.get("id"), "title": page.get("title"), "web_url": _web_url(page)}
