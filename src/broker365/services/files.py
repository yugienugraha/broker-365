"""OneDrive files (works for personal and work/school accounts)."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from broker365 import config, graph
from broker365.errors import Broker365Error, GraphError
from broker365.graph import item_ref
from broker365.services import tool

TEXT_READ_MAX = 1_000_000


def summarize(item: dict[str, Any]) -> dict[str, Any]:
    parent = (item.get("parentReference") or {}).get("path", "")
    folder = parent.split("root:", 1)[1] if "root:" in parent else ""
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "path": f"{folder}/{item.get('name', '')}" if parent else "/",
        "type": "folder" if "folder" in item else "file",
        "size": item.get("size"),
        "modified": item.get("lastModifiedDateTime"),
        "web_url": item.get("webUrl"),
        "etag": item.get("eTag"),
    }


def _g() -> graph.GraphClient:
    return graph.client("files")


def get_item(item: str) -> dict[str, Any]:
    return _g().get(item_ref(item))


def download_bytes(item: str) -> tuple[bytes, dict[str, Any]]:
    """(content, raw driveItem). The metadata already carries a pre-authenticated download
    URL, so content costs one unauthenticated GET instead of another Graph hop."""
    return _content(_file_meta(item))


def _file_meta(item: str, g: graph.GraphClient | None = None, drive: str = "/me/drive") -> dict:
    meta = (g or _g()).get(item_ref(item, drive))
    if "folder" in meta:
        raise Broker365Error(f"'{item}' is a folder, not a file")
    return meta


def _content(
    meta: dict[str, Any], g: graph.GraphClient | None = None, drive: str = "/me/drive"
) -> tuple[bytes, dict[str, Any]]:
    if url := meta.get("@microsoft.graph.downloadUrl"):
        return graph.fetch(url), meta
    return (g or _g()).download(meta["id"], drive=drive), meta


def download_from(g: graph.GraphClient, drive: str, item: str) -> tuple[bytes, dict[str, Any]]:
    """download_bytes for any drive (SharePoint libraries) with that service's client."""
    return _content(_file_meta(item, g, drive), g, drive)


def download_office(item: str, exts: tuple[str, ...], label: str) -> tuple[bytes, dict]:
    """download_bytes for an Office round-trip, rejecting the wrong file type up front."""
    meta = _file_meta(item)
    if not meta.get("name", "").lower().endswith(exts):
        raise Broker365Error(
            f"'{meta.get('name')}' is not a supported {label} file ({', '.join(exts)})"
        )
    return _content(meta)


def parse_office(data: bytes, parser: Callable[[io.BytesIO], Any], label: str) -> Any:
    """Parse downloaded Office bytes; damaged files become a readable Broker365Error."""
    try:
        return parser(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - zip/xml/content-type errors from the libraries
        raise Broker365Error(f"Could not open the {label} file: {type(exc).__name__}") from exc


def to_bytes(doc: Any) -> bytes:
    """Serialize anything with .save(file) (python-docx, python-pptx, openpyxl)."""
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def save_if_changed(
    meta: dict[str, Any], changed: bool, make_bytes: Callable[[], bytes], **extra: Any
) -> dict[str, Any]:
    """Finish a round-trip edit: eTag-guarded upload when something changed, else no-op."""
    if not changed:
        return {**summarize(meta), **extra, "uploaded": False}
    return {**replace_bytes(meta, make_bytes()), **extra, "uploaded": True}


def replace_bytes(meta: dict[str, Any], data: bytes) -> dict[str, Any]:
    """Overwrite an existing file only if it hasn't changed since `meta` was read."""
    return summarize(_g().upload(data, item_id=meta["id"], if_match=meta.get("eTag")))


def create_bytes(
    remote_path: str,
    data: bytes,
    conflict: str = "fail",
    g: graph.GraphClient | None = None,
    drive: str = "/me/drive",
) -> dict[str, Any]:
    if not remote_path.startswith("/") or not remote_path.strip("/"):
        raise Broker365Error("Remote paths must be absolute, e.g. /Documents/report.docx")
    g = g or _g()
    return summarize(g.upload(data, remote_path=remote_path, conflict=conflict, drive=drive))


def read_local(path: str) -> Path:
    """A local file inside the sandbox, or a clear error."""
    src = config.resolve_local(path)
    if not src.is_file():
        raise Broker365Error(f"{src} is not a file")
    return src


def _folder_id(folder: str) -> str:
    """Folder id for a path; ids are passed through without a lookup."""
    return get_item(folder)["id"] if folder.startswith("/") else folder


def write_local(path: str | None, default_name: str, data: bytes, overwrite: bool) -> str:
    dest = config.resolve_local(path, default_name)
    if dest.exists() and not overwrite:
        raise Broker365Error(f"{dest} already exists (pass overwrite=true to replace it)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return str(dest)


# ---- tools ----------------------------------------------------------------------------


@tool("files")
def files_list(path: str = "/", limit: int = 100) -> list[dict[str, Any]]:
    """List a OneDrive folder. `path` is absolute ('/' = root, '/Documents') or an item id."""
    return [summarize(i) for i in _g().paged(f"{item_ref(path)}/children", limit=limit)]


@tool("files")
def files_search(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search file names and content across the user's OneDrive."""
    q = graph.odata_str(query)
    return [summarize(i) for i in _g().paged(f"/me/drive/root/search(q='{q}')", limit=limit)]


@tool("files")
def files_get(item: str) -> dict[str, Any]:
    """Metadata for a file or folder, by absolute path or item id."""
    return summarize(get_item(item))


@tool("files")
def files_read_text(item: str, max_bytes: int = 200_000) -> dict[str, Any]:
    """Read a text file (txt, md, csv, json, …) and return its content as UTF-8."""
    data, meta = download_bytes(item)
    limit = min(max_bytes, TEXT_READ_MAX)
    return {
        **summarize(meta),
        "truncated": len(data) > limit,
        "content": data[:limit].decode("utf-8", errors="replace"),
    }


@tool("files")
def files_download(item: str, dest: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Download a file to the local workspace (or `dest` inside an allowed directory)."""
    data, meta = download_bytes(item)
    return {**summarize(meta), "local_path": write_local(dest, meta["name"], data, overwrite)}


@tool("files", writes=True)
def files_upload(
    local_path: str,
    remote_path: str,
    conflict: Literal["fail", "replace", "rename"] = "fail",
) -> dict[str, Any]:
    """Upload a local file (inside an allowed directory) to an absolute OneDrive path."""
    return create_bytes(remote_path, read_local(local_path).read_bytes(), conflict)


@tool("files", writes=True)
def files_write_text(
    remote_path: str, content: str, conflict: Literal["fail", "replace", "rename"] = "fail"
) -> dict[str, Any]:
    """Create a text file (txt, md, csv, json, …) in OneDrive from a string."""
    return create_bytes(remote_path, content.encode("utf-8"), conflict)


@tool("files", writes=True)
def files_mkdir(path: str) -> dict[str, Any]:
    """Create a folder at an absolute path (parents must exist). Idempotent: if it already
    exists, the existing folder is returned with existed=true."""
    parent, _, name = path.rstrip("/").rpartition("/")
    if not path.startswith("/") or not name:
        raise Broker365Error("Folder path must be absolute, e.g. /Projects/2026")
    body = {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
    try:
        # conflictBehavior=fail makes a retried create harmless (seen live: transient 504s)
        created = _g().post(f"{item_ref(parent or '/')}/children", json=body, idempotent=True)
    except GraphError as e:
        if e.status != 409:
            raise
        existing = get_item(path)  # e.g. a retry after a 504 that had in fact succeeded
        if "folder" not in existing:
            raise
        return {**summarize(existing), "existed": True}
    return summarize(created)


@tool("files", writes=True)
def files_move(
    item: str, new_parent: str | None = None, new_name: str | None = None
) -> dict[str, Any]:
    """Move and/or rename a file or folder. `new_parent` is an absolute folder path or id."""
    if not new_parent and not new_name:
        raise Broker365Error("Give new_parent and/or new_name")
    body: dict[str, Any] = {}
    if new_name:
        body["name"] = new_name
    if new_parent:
        body["parentReference"] = {"id": _folder_id(new_parent)}
    return summarize(_g().patch(item_ref(item), json=body))


@tool("files", writes=True)
def files_copy(item: str, new_parent: str, new_name: str | None = None) -> dict[str, Any]:
    """Copy a file or folder into another folder (runs asynchronously on the server)."""
    body: dict[str, Any] = {"parentReference": {"id": _folder_id(new_parent)}}
    if new_name:
        body["name"] = new_name
    resp = _g().request("POST", f"{item_ref(item)}/copy", json=body)
    return {"status": "accepted", "monitor_url": resp.headers.get("Location")}


@tool("files", destructive=True)
def files_delete(item: str) -> dict[str, Any]:
    """Move a file or folder to the OneDrive recycle bin."""
    meta = get_item(item)
    _g().delete(item_ref(meta["id"]))
    return {"deleted": True, "id": meta["id"], "name": meta.get("name")}


@tool("files", destructive=True)
def files_share(
    item: str,
    link_type: Literal["view", "edit"] = "view",
    scope: Literal["anonymous", "organization"] = "organization",
) -> dict[str, Any]:
    """Create a sharing link. 'anonymous' links work for anyone who has the URL."""
    body = {"type": link_type, "scope": scope}
    link = _g().post(f"{item_ref(item)}/createLink", json=body).get("link", {})
    return {"url": link.get("webUrl"), "type": link.get("type"), "scope": link.get("scope")}


@tool("files")
def files_convert(
    item: str,
    format: Literal["pdf", "html"] = "pdf",
    dest: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Convert an Office file (docx, xlsx, pptx, …) to PDF/HTML and save it locally."""
    meta = get_item(item)
    data = _g().download(meta["id"], params={"format": format})
    name = meta["name"].rsplit(".", 1)[0] + f".{format}"
    return {"source": summarize(meta), "local_path": write_local(dest, name, data, overwrite)}
