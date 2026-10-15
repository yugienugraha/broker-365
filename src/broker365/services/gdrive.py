"""Google Drive (Drive v3) for a connected Google account: the gdrive_* tools.

Items are addressed by Drive file id or by absolute path ('/Folder/file.ext', '/' = My Drive),
resolved name by name. Drive allows several items with the same name in one folder, so an
ambiguous path is an error that lists the candidates' ids. Google Docs/Sheets/Slides have no
bytes of their own: they are read with gdrive_read_text or saved with gdrive_export.
"""

from __future__ import annotations

import contextlib
import mimetypes
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from broker365 import config, gapi
from broker365.errors import Broker365Error, GoogleApiError
from broker365.services import files, tool
from broker365.transport import seg

BUNDLE = "gdrive"
GOOGLE = config.GOOGLE
FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"
NATIVE_PREFIX = "application/vnd.google-apps."
KINDS = {FOLDER: "folder", DOC: "google-doc", SHEET: "google-sheet", SLIDES: "google-slides"}
FIELDS = (
    "id,name,mimeType,size,modifiedTime,webViewLink,parents,owners(emailAddress),shared,"
    "trashed,driveId"
)
# Enough for existence/ambiguity checks; driveId scopes the next query in a path walk.
PROBE_FIELDS = "id,name,mimeType,driveId"
ALL_DRIVES = {"supportsAllDrives": "true"}
ITEM_PARAMS = {**ALL_DRIVES, "fields": FIELDS}
MEDIA_PARAMS = {**ALL_DRIVES, "alt": "media"}
LISTING = {**ALL_DRIVES, "includeItemsFromAllDrives": "true"}
SCAN_MAX = 100_000  # names read when looking for a free "name (n).ext"
INCOMPLETE = (
    "Google Drive's search was incomplete, so this path can't be resolved reliably. "
    "Use the file id instead."
)
TEXT_READ_MAX = files.TEXT_READ_MAX
EXPORT_LIMIT = 10 * 1024 * 1024  # Google's limit on exported content

# Export formats per native type (Drive's export matrix) -> MIME type requested.
FORMAT_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/x-vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "rtf": "application/rtf",
    "txt": "text/plain",
    "md": "text/markdown",
    "html": "text/html",
    "epub": "application/epub+zip",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
}
EXPORTS = {
    DOC: ("pdf", "docx", "odt", "txt", "md", "html", "epub", "rtf"),
    SHEET: ("pdf", "xlsx", "ods", "csv", "tsv"),
    SLIDES: ("pdf", "pptx", "odp", "txt"),
}
# How gdrive_read_text turns a native file into text.
TEXT_EXPORT = {DOC: "text/plain", SHEET: "text/csv", SLIDES: "text/plain"}
TEXT_MIMES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/javascript",
    "application/x-sh",
    "application/toml",
}
TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log", ".toml"}
EXTRA_MIMES = {
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".log": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
}
# convert=True on upload: source extension -> native type.
CONVERT = {
    **dict.fromkeys((".docx", ".odt", ".rtf", ".md", ".txt", ".html", ".htm"), DOC),
    **dict.fromkeys((".xlsx", ".ods", ".csv", ".tsv"), SHEET),
    **dict.fromkeys((".pptx", ".odp"), SLIDES),
}
Conflict = Literal["fail", "replace", "rename"]
ExportFormat = Literal[
    "pdf", "docx", "xlsx", "pptx", "odt", "ods", "odp", "rtf", "txt", "md", "html", "epub",
    "csv", "tsv",
]  # fmt: skip


def _g() -> gapi.GoogleClient:
    return gapi.client(BUNDLE)


def _q(value: str) -> str:
    """Escape a value for a single-quoted string in Drive's `q` syntax."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _file_url(file_id: str) -> str:
    return f"{gapi.DRIVE}/files/{seg(file_id, 'file id')}"


def summarize(f: dict[str, Any]) -> dict[str, Any]:
    mime = f.get("mimeType") or ""
    kind = KINDS.get(mime)
    if kind is None:
        kind = f"google-{mime[len(NATIVE_PREFIX) :]}" if mime.startswith(NATIVE_PREFIX) else "file"
    size = f.get("size")
    return {
        "id": f.get("id"),
        "name": f.get("name"),
        "mimeType": mime,
        "kind": kind,
        "size": int(size) if isinstance(size, str) and size.isdigit() else size,
        "modifiedTime": f.get("modifiedTime"),
        "webViewLink": f.get("webViewLink"),
        "parents": f.get("parents") or [],
        "owners": [o.get("emailAddress") for o in f.get("owners") or [] if o.get("emailAddress")],
        "shared": f.get("shared"),
        "trashed": f.get("trashed"),
    }


# ---- item resolution --------------------------------------------------------------------


def _by_id(file_id: str) -> dict[str, Any]:
    return _g().get(_file_url(file_id), params=ITEM_PARAMS)


def _query(
    q: str, limit: int, fields: str, corpora: dict[str, str], **extra: Any
) -> tuple[list[dict[str, Any]], bool]:
    """(files.list results, incompleteSearch)."""
    params = {
        **LISTING,
        **corpora,
        "q": q,
        "fields": f"nextPageToken,incompleteSearch,files({fields})",
        **extra,
    }
    flags = {"incompleteSearch": False}
    found = _g().paged(f"{gapi.DRIVE}/files", params, "files", limit, flags)
    return found, flags["incompleteSearch"]


def _in_folder(
    folder: dict[str, Any], q: str, limit: int, fields: str = FIELDS, **extra: Any
) -> list[dict[str, Any]]:
    """files.list of `q` scoped to the folder's drive: its shared drive (driveId) or the
    user's files. Partial results are an error, never acted on."""
    drive_id = folder.get("driveId")
    corpora = {"corpora": "drive", "driveId": drive_id} if drive_id else {"corpora": "user"}
    found, incomplete = _query(q, limit, fields, corpora, **extra)
    if incomplete:
        raise Broker365Error(INCOMPLETE)
    return found


def _children(
    parent: dict[str, Any],
    name: str,
    *,
    folders_only: bool = False,
    fields: str = PROBE_FIELDS,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Untrashed items called `name` in folder `parent` (its metadata: id, driveId). The
    defaults suit existence/ambiguity probes; pass FIELDS (and a larger limit) where the
    result is returned."""
    q = f"name = '{_q(name)}' and '{_q(parent['id'])}' in parents and trashed = false"
    if folders_only:
        q += f" and mimeType = '{FOLDER}'"
    return _in_folder(parent, q, limit, fields)


def _one(matches: list[dict[str, Any]], what: str) -> dict[str, Any]:
    if not matches:
        raise Broker365Error(f"'{what}' not found in Google Drive (or no access)")
    if len(matches) > 1:
        options = "; ".join(f"{m.get('id')} ({m.get('mimeType')})" for m in matches)
        raise Broker365Error(
            f"Several items are named '{what}': {options}. Use the file id instead of the path."
        )
    return matches[0]


def _split(path: str) -> list[str]:
    if not path.startswith("/"):
        raise Broker365Error("Drive paths must be absolute, e.g. /Reports/q3.pdf ('/' = My Drive)")
    return [p for p in path.split("/") if p]


def resolve(item: str) -> dict[str, Any]:
    """Metadata of a file or folder given a Drive file id or an absolute path."""
    if not item:
        raise Broker365Error("A Drive file id or absolute path is required")
    if not item.startswith("/"):
        return _by_id(item)
    parts = _split(item)
    if not parts:
        return _by_id("root")
    parent: dict[str, Any] = {"id": "root"}  # My Drive's alias: no lookup to start the walk
    for i, name in enumerate(parts[:-1]):
        parent = _one(_children(parent, name, folders_only=True), "/" + "/".join(parts[: i + 1]))
    return _one(_children(parent, parts[-1], fields=FIELDS, limit=10), "/" + "/".join(parts))


def _folder(item: str) -> dict[str, Any]:
    meta = resolve(item)
    if meta.get("mimeType") != FOLDER:
        raise Broker365Error(f"'{item}' is not a folder")
    return meta


def _file(item: str) -> dict[str, Any]:
    meta = resolve(item)
    if meta.get("mimeType") == FOLDER:
        raise Broker365Error(f"'{item}' is a folder, not a file")
    return meta


def _parent_and_leaf(path: str, example: str) -> tuple[dict[str, Any], str]:
    """(metadata of the existing parent folder, last name) of an absolute path."""
    parts = _split(path)
    if not parts:
        raise Broker365Error(f"Give a path below '/', e.g. {example}")
    return _folder("/" + "/".join(parts[:-1])), parts[-1]


def _local_name(name: str | None, fallback: str = "download") -> str:
    """A Drive name as a single local file name (Drive names may contain '/')."""
    cleaned = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", name or "").strip().strip(".")
    return cleaned or fallback


# ---- content ----------------------------------------------------------------------------


@contextlib.contextmanager
def _export_limit(meta: dict[str, Any]) -> Iterator[None]:
    """Turn Google's 10 MB export refusal into a clear error."""
    try:
        yield
    except GoogleApiError as e:
        if e.code == "exportSizeLimitExceeded":
            raise Broker365Error(
                f"'{meta.get('name')}' is too large to export (Google's limit is 10 MB). "
                f"Open it in the browser instead: {meta.get('webViewLink')}"
            ) from None
        raise


def _export_url(meta: dict[str, Any]) -> str:
    return f"{_file_url(meta['id'])}/export"


def _is_text(meta: dict[str, Any]) -> bool:
    mime = (meta.get("mimeType") or "").split(";")[0].strip().lower()
    return (
        mime.startswith("text/")
        or mime in TEXT_MIMES
        or mime.endswith(("+json", "+xml"))
        or PurePosixPath(meta.get("name") or "").suffix.lower() in TEXT_EXTS
    )


def _upload_mime(name: str) -> str:
    """MIME type for an upload; common types are fixed here because mimetypes depends on
    the platform (on Windows, the registry)."""
    ext = PurePosixPath(name).suffix.lower()
    return (
        EXTRA_MIMES.get(ext)
        or FORMAT_MIME.get(ext[1:])
        or mimetypes.guess_type(name)[0]
        or "application/octet-stream"
    )


def _free_name(parent: dict[str, Any], name: str) -> str:
    """'name (1).ext', 'name (2).ext', …: the first that isn't taken in the folder. Reads the
    folder's names once (Drive's `name contains` matches prefixes/tokens, so it can miss)."""
    p = PurePosixPath(name)
    stem, ext = (p.stem, p.suffix) if p.stem else (name, "")
    q = f"'{_q(parent['id'])}' in parents and trashed = false"
    taken = {f.get("name") for f in _in_folder(parent, q, SCAN_MAX, "name")}
    n = 1
    while f"{stem} ({n}){ext}" in taken:
        n += 1
    return f"{stem} ({n}){ext}"


def _put(
    parent: dict[str, Any],
    name: str,
    data: bytes | Path,
    mime: str,
    conflict: str,
    target_mime: str | None = None,
) -> dict[str, Any]:
    """Create `name` in folder `parent`, or apply `conflict` if that name is taken."""
    if conflict not in ("fail", "replace", "rename"):
        raise Broker365Error("conflict must be 'fail', 'replace' or 'rename'")
    if not name or "/" in name:
        raise Broker365Error("A file name without '/' is required")
    existing = _children(parent, name)
    if existing and conflict == "fail":
        ids = ", ".join(e["id"] for e in existing)
        raise Broker365Error(
            f"'{name}' already exists in that folder (id {ids}). "
            "Pass conflict='replace' to overwrite it or 'rename' to keep both."
        )
    if existing and conflict == "replace" and target_mime:
        raise Broker365Error(
            f"'{name}' exists, and a file can't be replaced while converting it. Use "
            "conflict='rename', or move the old one to the trash first (gdrive_trash)."
        )
    if existing and conflict == "replace":
        target = _one(existing, name)
        if target.get("mimeType") == FOLDER:
            raise Broker365Error(f"'{name}' is a folder; it can't be replaced by a file")
        out = _g().upload(data, {}, mime, file_id=target["id"], params=ITEM_PARAMS)
        return {**summarize(out), "replaced": True}
    if existing:  # rename
        name = _free_name(parent, name)
    metadata: dict[str, Any] = {"name": name, "parents": [parent["id"]]}
    if target_mime:
        metadata["mimeType"] = target_mime
    return summarize(_g().upload(data, metadata, mime, params=ITEM_PARAMS))


# ---- tools ------------------------------------------------------------------------------


@tool(BUNDLE, GOOGLE)
def gdrive_list(
    path: str = "/", limit: int = 100, include_trashed: bool = False
) -> list[dict[str, Any]]:
    """List a Google Drive folder. `path`: absolute path ('/' = My Drive, '/Reports') or a
    folder id. Folders first, then by name."""
    folder = _folder(path)
    q = f"'{_q(folder['id'])}' in parents"
    if not include_trashed:
        q += " and trashed = false"
    return [summarize(f) for f in _in_folder(folder, q, limit, orderBy="folder,name")]


@tool(BUNDLE, GOOGLE)
def gdrive_search(
    query: str, limit: int = 25, raw: bool = False
) -> list[dict[str, Any]] | dict[str, Any]:
    """Search all of Google Drive (My Drive, shared with me, shared drives) by file name and
    content; trashed items excluded. With raw=true, `query` is Drive `q` syntax as-is, e.g.
    "mimeType = 'application/pdf' and modifiedTime > '2026-01-01T00:00:00'". Returns a list;
    if Google searched only part of the drives, {"files": [...], "incompleteSearch": true}."""
    if not query:
        raise Broker365Error("A search query is required")
    if raw:
        q = query
    else:
        e = _q(query)
        q = f"(fullText contains '{e}' or name contains '{e}') and trashed = false"
    found, incomplete = _query(q, limit, FIELDS, {"corpora": "allDrives"})
    files_ = [summarize(f) for f in found]
    if not incomplete:
        return files_
    return {
        "files": files_,
        "incompleteSearch": True,
        "note": "Google didn't search every drive; results may be missing. Narrow the query.",
    }


@tool(BUNDLE, GOOGLE)
def gdrive_get(item: str) -> dict[str, Any]:
    """Metadata of a Google Drive file or folder, by absolute path or file id."""
    return summarize(resolve(item))


@tool(BUNDLE, GOOGLE)
def gdrive_read_text(item: str, max_bytes: int = 200_000) -> dict[str, Any]:
    """Read a Drive file as text: text files (txt, md, csv, json, …) as they are, Google Docs
    and Slides as plain text, Google Sheets as CSV of the first sheet only. Other binary files:
    use gdrive_download or gdrive_export."""
    meta = _file(item)
    limit = max(0, min(max_bytes, TEXT_READ_MAX))
    mime = meta.get("mimeType") or ""
    note = None
    if mime in TEXT_EXPORT:
        with _export_limit(meta):
            params = {"mimeType": TEXT_EXPORT[mime]}
            data, truncated = _g().download(_export_url(meta), params, limit, truncate=True)
        if mime == SHEET:
            note = "CSV of the first sheet only"
    elif mime.startswith(NATIVE_PREFIX):
        raise Broker365Error(f"'{meta.get('name')}' ({mime}) can't be read as text")
    elif _is_text(meta):
        url = _file_url(meta["id"])
        data, truncated = _g().download(url, MEDIA_PARAMS, limit, truncate=True)
    else:
        raise Broker365Error(
            f"'{meta.get('name')}' is not a text file ({mime}). Use gdrive_download to save it "
            "locally (or gdrive_export for Google Docs/Sheets/Slides)."
        )
    out = {
        **summarize(meta),
        "truncated": truncated,
        # Google's text/plain export starts with a UTF-8 BOM (seen live).
        "content": data.decode("utf-8", errors="replace").removeprefix("﻿"),
    }
    if note:
        out["note"] = note
    return out


@tool(BUNDLE, GOOGLE)
def gdrive_download(item: str, dest: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Download a (non-Google-native) Drive file to the local workspace, or to `dest` inside
    an allowed directory. Google Docs/Sheets/Slides: use gdrive_export."""
    meta = _file(item)
    if (meta.get("mimeType") or "").startswith(NATIVE_PREFIX):
        raise Broker365Error(
            f"'{meta.get('name')}' is a Google-native file with no downloadable content. "
            "Use gdrive_export (e.g. format='pdf' or 'docx')."
        )
    url = _file_url(meta["id"])
    local = files.stream_local(
        dest,
        _local_name(meta.get("name")),
        overwrite,
        lambda out: _g().download_to(url, out, MEDIA_PARAMS),
    )
    return {**summarize(meta), "local_path": local}


@tool(BUNDLE, GOOGLE)
def gdrive_export(
    item: str, format: ExportFormat, dest: str | None = None, overwrite: bool = False
) -> dict[str, Any]:
    """Export a Google Doc (pdf docx odt txt md html epub rtf), Sheet (pdf xlsx ods csv tsv;
    csv/tsv = first sheet) or Slides deck (pdf pptx odp txt) and save it locally (workspace,
    or `dest` inside an allowed directory). Google limits exports to 10 MB."""
    meta = _file(item)
    mime = meta.get("mimeType") or ""
    if mime not in EXPORTS:
        raise Broker365Error(
            f"'{meta.get('name')}' is not a Google Doc, Sheet or Slides file; "
            "use gdrive_download instead."
        )
    if format not in EXPORTS[mime]:
        raise Broker365Error(
            f"A {KINDS[mime]} can be exported as: {', '.join(EXPORTS[mime])} (not '{format}')"
        )

    def fill(out: Any) -> None:
        with _export_limit(meta):
            params = {"mimeType": FORMAT_MIME[format]}
            _g().download_to(_export_url(meta), out, params, EXPORT_LIMIT)

    name = f"{_local_name(meta.get('name'), 'export')}.{format}"
    return {
        "source": summarize(meta),
        "format": format,
        "local_path": files.stream_local(dest, name, overwrite, fill),
    }


@tool(BUNDLE, GOOGLE, writes=True, audit=("remote_folder", "conflict", "convert"))
def gdrive_upload(
    local_path: str,
    remote_folder: str = "/",
    name: str | None = None,
    conflict: Conflict = "fail",
    convert: bool = False,
) -> dict[str, Any]:
    """Upload a local file (inside an allowed directory) into a Drive folder (path or id).
    conflict: 'fail' if the name exists, 'replace' its content, or 'rename' ('a (1).txt').
    convert=true turns docx/odt/rtf/md/txt/html into a Google Doc, xlsx/ods/csv/tsv into a
    Sheet and pptx/odp into Slides (Markdown keeps headings, bold, lists)."""
    src = files.read_local(local_path)
    target_mime = None
    if convert:
        target_mime = CONVERT.get(src.suffix.lower())
        if not target_mime:
            raise Broker365Error(f"convert=true supports {', '.join(CONVERT)} files")
    if name is None:
        name = src.stem if convert else src.name
    folder = _folder(remote_folder)
    return _put(folder, name, src, _upload_mime(src.name), conflict, target_mime)


@tool(BUNDLE, GOOGLE, writes=True, audit=("conflict",))
def gdrive_write_text(remote_path: str, text: str, conflict: Conflict = "fail") -> dict[str, Any]:
    """Create a text file (txt, md, csv, json, …) at an absolute Drive path from a string.
    conflict: 'fail', 'replace' or 'rename', as for gdrive_upload."""
    folder, name = _parent_and_leaf(remote_path, "/Notes/todo.md")
    mime = _upload_mime(name)
    if not mime.startswith("text/") and mime not in TEXT_MIMES:
        mime = "text/plain"
    return _put(folder, name, text.encode("utf-8"), mime, conflict)


@tool(BUNDLE, GOOGLE, writes=True)
def gdrive_mkdir(path: str) -> dict[str, Any]:
    """Create a folder at an absolute Drive path (parents must exist). Idempotent: if a
    folder of that name already exists there, it is returned with existed=true."""
    parent, name = _parent_and_leaf(path, "/Projects/2026")
    if found := _children(parent, name, folders_only=True, fields=FIELDS):
        return {**summarize(_one(found, path)), "existed": True}
    body = {"name": name, "mimeType": FOLDER, "parents": [parent["id"]]}
    return summarize(_g().post(f"{gapi.DRIVE}/files", json=body, params=ITEM_PARAMS))


@tool(BUNDLE, GOOGLE, writes=True)
def gdrive_move(
    item: str, new_parent: str | None = None, new_name: str | None = None
) -> dict[str, Any]:
    """Move and/or rename a Drive file or folder. `new_parent` is a folder path or id."""
    if not new_parent and not new_name:
        raise Broker365Error("Give new_parent and/or new_name")
    meta = resolve(item)
    params: dict[str, Any] = dict(ITEM_PARAMS)
    if new_parent:
        dest = _folder(new_parent)
        params["addParents"] = dest["id"]
        if old := [p for p in meta.get("parents") or [] if p != dest["id"]]:
            params["removeParents"] = ",".join(old)
    body = {"name": new_name} if new_name else {}
    out = _g().patch(_file_url(meta["id"]), json=body, params=params, idempotent=True)
    return summarize(out)


@tool(BUNDLE, GOOGLE, writes=True)
def gdrive_copy(
    item: str, new_parent: str | None = None, new_name: str | None = None
) -> dict[str, Any]:
    """Copy a Drive file (not a folder) into `new_parent` (path or id; default: same folder),
    optionally under `new_name` (default: Google's 'Copy of …')."""
    meta = _file(item)
    body: dict[str, Any] = {}
    if new_name:
        body["name"] = new_name
    if new_parent:
        body["parents"] = [_folder(new_parent)["id"]]
    return summarize(_g().post(f"{_file_url(meta['id'])}/copy", json=body, params=ITEM_PARAMS))


@tool(BUNDLE, GOOGLE, destructive=True)
def gdrive_trash(item: str) -> dict[str, Any]:
    """Move a Drive file or folder to the trash (restorable for 30 days; never deleted
    permanently by broker365)."""
    url = _file_url(resolve(item)["id"])
    return summarize(_g().patch(url, json={"trashed": True}, params=ITEM_PARAMS, idempotent=True))


@tool(BUNDLE, GOOGLE, destructive=True, audit=("email", "role", "type", "domain"))
def gdrive_share(
    item: str,
    email: str | None = None,
    role: Literal["reader", "commenter", "writer"] = "reader",
    type: Literal["user", "group", "domain", "anyone"] = "user",
    notify: bool = False,
    message: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """Share a Drive item. type='user'/'group' needs `email`; 'domain' needs `domain`;
    'anyone' = anyone with the link. role: reader, commenter or writer (no ownership
    transfer). notify=true emails the person (with optional `message`)."""
    if role not in ("reader", "commenter", "writer"):
        raise Broker365Error("role must be 'reader', 'commenter' or 'writer'")
    if type not in ("user", "group", "domain", "anyone"):
        raise Broker365Error("type must be 'user', 'group', 'domain' or 'anyone'")
    body: dict[str, Any] = {"role": role, "type": type}
    params: dict[str, Any] = {**ALL_DRIVES, "fields": "id,role,type,emailAddress,domain"}
    if type in ("user", "group"):
        if not email or "@" not in email:
            raise Broker365Error(f"type='{type}' needs an email address")
        body["emailAddress"] = email
        params["sendNotificationEmail"] = "true" if notify else "false"
        if notify and message:
            params["emailMessage"] = message
    elif type == "domain":
        if not domain:
            raise Broker365Error("type='domain' needs a domain, e.g. example.com")
        body["domain"] = domain
    if type in ("domain", "anyone"):
        body["allowFileDiscovery"] = False  # reachable by link only, not listed in searches
    meta = resolve(item)
    try:
        perm = _g().post(f"{_file_url(meta['id'])}/permissions", json=body, params=params)
    except GoogleApiError as e:
        no_account = e.code == "invalidSharingRequest" or "notif" in e.message.lower()
        if e.status == 400 and type in ("user", "group") and not notify and no_account:
            raise Broker365Error(
                f"{email} has no Google account, and Google requires a notification email "
                "to share with it. Retry with notify=true."
            ) from None
        raise
    out = {
        "permission": perm,
        "id": meta["id"],
        "name": meta.get("name"),
        "webViewLink": meta.get("webViewLink"),
    }
    if type == "anyone":
        out["access"] = "anyone with the link"
    return out
