"""Google Docs (Docs v1) for a connected Google account: the gdocs_* tools.

Documents are addressed like Drive files (id or absolute path). Writes can be guarded with the
`revisionId` from gdocs_read: if the document changed since, nothing is written and a conflict
error is returned.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any, Literal

from broker365 import config
from broker365.errors import Broker365Error, GoogleConflictError
from broker365.services import _gnative, gdrive, require_consent, tool

BUNDLE = "gdocs"
GOOGLE = config.GOOGLE
DOCS = "https://docs.googleapis.com/v1/documents"
DOC = _gnative.Native(
    BUNDLE, DOCS, gdrive.DOC, "Google Doc", "the word_* tools for .docx files in OneDrive"
)
READ_MAX = 1_000_000
_KEY = re.compile(r"[^{}]{1,200}")
_PARA = (
    "paragraph(elements(textRun(content)),paragraphStyle(namedStyleType),"
    "bullet(listId,nestingLevel))"
)
# Everything _Flattener reads: paragraphs (also inside tables and a TOC) and list glyphs.
READ_FIELDS = (
    f"documentId,title,revisionId,lists,body.content({_PARA},"
    f"table(tableRows(tableCells(content({_PARA})))),tableOfContents(content({_PARA})))"
)
UNORDERED_GLYPHS = {"", "GLYPH_TYPE_UNSPECIFIED", "NONE"}
APPEND_ATTEMPTS = 3  # gdocs_append re-reads and retries when the doc changed meanwhile


# ---- reading ----------------------------------------------------------------------------


class _Flattener:
    """Structural elements as text lines: headings as '#', list items as '- ' or '1.' (per
    list and nesting level, from the list's glyph), tables as tab-separated rows."""

    def __init__(self, lists: dict[str, Any] | None):
        self.lists = lists or {}
        self.counters: dict[str, list[int]] = {}  # listId -> count per nesting level

    def _ordered(self, list_id: str, level: int) -> bool:
        levels = ((self.lists.get(list_id) or {}).get("listProperties") or {}).get(
            "nestingLevels"
        ) or []
        nl = levels[level] if level < len(levels) else {}
        return nl.get("glyphType", "") not in UNORDERED_GLYPHS and not nl.get("glyphSymbol")

    def _marker(self, bullet: dict[str, Any]) -> str:
        list_id, level = str(bullet.get("listId") or ""), int(bullet.get("nestingLevel") or 0)
        counts = self.counters.setdefault(list_id, [])
        del counts[level + 1 :]  # deeper levels restart under a new item
        counts.extend([0] * (level + 1 - len(counts)))
        if not self._ordered(list_id, level):
            return "-"
        counts[level] += 1
        return f"{counts[level]}."

    def paragraph(self, p: dict[str, Any]) -> str:
        # vertical tab = soft line break
        text = _gnative.run_text(p.get("elements")).rstrip("\n").replace("\u000b", "\n")
        style = (p.get("paragraphStyle") or {}).get("namedStyleType") or ""
        if (m := re.fullmatch(r"HEADING_([1-6])", style)) and text:
            return "#" * int(m.group(1)) + " " + text
        if style == "TITLE" and text:
            return "# " + text
        if (bullet := p.get("bullet")) is not None:
            indent = "  " * int(bullet.get("nestingLevel") or 0)
            return f"{indent}{self._marker(bullet)} {text}"
        return text

    def lines(self, content: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        for el in content:
            if "paragraph" in el:
                out.append(self.paragraph(el["paragraph"]))
            elif "table" in el:
                for row in el["table"].get("tableRows") or []:
                    cells = [
                        " ".join(t for t in self.lines(c.get("content") or []) if t)
                        for c in row.get("tableCells") or []
                    ]
                    out.append("\t".join(cells))
            elif "tableOfContents" in el:
                out.extend(self.lines(el["tableOfContents"].get("content") or []))
        return out


def _end_index(doc: dict[str, Any]) -> int:
    content = (doc.get("body") or {}).get("content") or []
    return max((int(el.get("endIndex") or 1) for el in content), default=1)


# ---- tools ------------------------------------------------------------------------------


@tool(BUNDLE, GOOGLE, writes=True, audit=("title", "remote_folder"), requires=("gdrive",))
def gdocs_create(
    title: str,
    markdown: str = "",
    remote_folder: str = "/",
    conflict: Literal["fail", "rename"] = "fail",
) -> dict[str, Any]:
    """Create a Google Doc called `title` in a Drive folder (path or id; '/' = My Drive) from
    Markdown: headings, bold/italic, lists, links and tables are converted by Google.
    conflict: 'fail' if the title is taken in the folder, or 'rename'."""
    parent = gdrive.folder_ref(remote_folder)
    if not markdown.strip():
        return gdrive.create(parent, title, gdrive.DOC, conflict)
    data = markdown.encode("utf-8")
    return gdrive.put(
        parent, title, data, "text/markdown", conflict, gdrive.DOC, allowed=("fail", "rename")
    )


@tool(BUNDLE, GOOGLE)
def gdocs_read(item: str, max_chars: int = 100_000) -> dict[str, Any]:
    """Read a Google Doc (id or path) as plain text: headings as '#', list items as '- ' or
    '1.', tables as tab-separated rows. Returns revisionId for guarded writes."""
    meta = DOC.resolve(item)
    doc = DOC.client().get(DOC.url(meta["id"]), params={"fields": READ_FIELDS})
    flat = _Flattener(doc.get("lists"))
    text = "\n".join(flat.lines((doc.get("body") or {}).get("content") or []))
    limit = max(0, min(max_chars, READ_MAX))
    return {
        "id": doc.get("documentId") or meta["id"],
        "title": doc.get("title"),
        "revisionId": doc.get("revisionId"),
        "truncated": len(text) > limit,
        "content": text[:limit],
    }


def _utf16_len(text: str) -> int:
    """Docs indexes count UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _append_requests(index: int, text: str) -> list[dict[str, Any]]:
    """Insert `text` as new plain paragraph(s) before the body's final newline at `index`:
    a leading newline when the document has content, then NORMAL_TEXT and no bullets over
    the inserted paragraphs (they would inherit the last paragraph's heading/list style)."""
    lead = "\n" if index > 1 else ""
    start = index + len(lead)
    end = start + _utf16_len(text)
    rng = {"startIndex": start, "endIndex": end}
    return [
        {"insertText": {"location": {"index": index}, "text": lead + text}},
        {
            "updateParagraphStyle": {
                "range": rng,
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType",
            }
        },
        {"deleteParagraphBullets": {"range": rng}},
    ]


@tool(BUNDLE, GOOGLE, writes=True)
def gdocs_append(item: str, markdown_or_text: str) -> dict[str, Any]:
    """Append text as new plain paragraph(s) at the end of a Google Doc. The text is inserted
    as-is: Markdown markers are not turned into formatting (use gdocs_create for that)."""
    text = (markdown_or_text or "").rstrip("\n")
    if not text.strip():
        raise Broker365Error("Nothing to append")
    meta = DOC.resolve(item)
    for _ in range(APPEND_ATTEMPTS):
        doc = DOC.client().get(
            DOC.url(meta["id"]), params={"fields": "revisionId,body.content(endIndex)"}
        )
        index = _end_index(doc) - 1  # before the body's final newline
        try:
            # Guarded by the revision just read, so the index is still right when it lands.
            _, revision = DOC.update(
                meta["id"], _append_requests(index, text), doc.get("revisionId")
            )
        except GoogleConflictError as e:
            conflict = e
            continue  # someone edited in between: read the new end and try again
        return {"id": meta["id"], "inserted_at": index, "revisionId": revision}
    raise GoogleConflictError(
        conflict.status,
        conflict.code,
        "the Google Doc is being edited by someone else right now, so nothing was appended. "
        "Try again shortly.",
    )


@tool(BUNDLE, GOOGLE, writes=True)
def gdocs_replace_text(
    item: str,
    find: str,
    replace: str,
    match_case: bool = True,
    revision_id: str | None = None,
) -> dict[str, Any]:
    """Replace every occurrence of `find` in a Google Doc. Pass `revision_id` (from
    gdocs_read) to fail with a conflict instead of editing a document changed since."""
    if not find:
        raise Broker365Error("`find` must not be empty")
    meta = DOC.resolve(item)
    counts, revision = DOC.replace_all(meta["id"], [(find, replace)], match_case, revision_id)
    return {"id": meta["id"], "occurrencesChanged": counts[0], "revisionId": revision}


@tool(BUNDLE, GOOGLE, writes=True, audit=("copy_to", "remote_folder", "conflict"))
def gdocs_fill_template(
    item: str,
    values: dict[str, Any],
    copy_to: str | None = None,
    remote_folder: str | None = None,
    conflict: Literal["fail", "rename"] = "fail",
) -> dict[str, Any]:
    """Replace {{key}} placeholders in a Google Doc with `values`. With `copy_to` (a title)
    the template is copied first (into `remote_folder`, default: next to it; conflict 'fail'
    if that title is taken, or 'rename') and the copy is filled; if filling fails the copy
    goes to the trash. Reports how often each key was replaced and which keys matched
    nothing."""
    if not isinstance(values, dict) or not values:
        raise Broker365Error("values must be a non-empty object of {key: text}")
    for key in values:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise Broker365Error(f"Invalid placeholder key {key!r} (no braces, 1-200 chars)")
    if remote_folder and not copy_to:
        raise Broker365Error("remote_folder is only used together with copy_to")
    if copy_to or item.startswith("/"):  # Drive is needed to copy or to find a path
        require_consent(GOOGLE, (BUNDLE, gdrive.BUNDLE))
    # Before copying, make sure the template really is a Doc (no stray copies).
    meta = DOC.resolve(item, verify=bool(copy_to), fields=f"{gdrive.PROBE_FIELDS},parents")
    copy = None
    if copy_to:
        if remote_folder:
            parent = gdrive.folder(remote_folder)
        elif meta.get("parents"):
            parent = {"id": meta["parents"][0], "driveId": meta.get("driveId")}
        else:
            raise Broker365Error("The template has no folder; pass remote_folder for the copy")
        _, name = gdrive.claim_name(parent, copy_to, conflict, ("fail", "rename"))
        copy = gdrive.copy(meta["id"], parent, name)
    target = (copy or meta)["id"]
    keys = list(values)
    pairs = [("{{" + k + "}}", "" if values[k] is None else str(values[k])) for k in keys]
    try:
        counts, revision = DOC.replace_all(target, pairs)
    except Exception:
        if copy:  # don't leave a half-made copy behind; trash only, never delete
            with contextlib.suppress(Broker365Error):
                gdrive.trash(copy["id"])
        raise
    replacements = dict(zip(keys, counts, strict=True))
    result: dict[str, Any] = {
        "id": target,
        "replacements": replacements,
        "unmatched_keys": [k for k, n in replacements.items() if n == 0],
        "revisionId": revision,
    }
    if copy:
        result["copy"] = copy
    return result
