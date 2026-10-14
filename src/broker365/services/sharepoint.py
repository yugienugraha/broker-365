"""SharePoint sites and document libraries (work/school accounts only)."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from broker365 import graph
from broker365.graph import item_ref, seg
from broker365.services import tool
from broker365.services.files import (
    create_bytes,
    download_from,
    read_local,
    summarize,
    write_local,
)


def _g() -> graph.GraphClient:
    return graph.client("sharepoint")


def _site(site_id: str) -> str:
    """Site id as 'host,guid,guid' or 'host:/sites/name:' (both used literally by Graph)."""
    seg(site_id, "site id")  # validates non-empty
    return quote(site_id, safe=",:/")


def _drive(site_id: str, drive_id: str | None) -> str:
    """A site's default document library, or a specific one by `drive_id` (which then takes
    precedence: site_id is not used to address the drive)."""
    return f"/drives/{seg(drive_id, 'drive id')}" if drive_id else f"/sites/{_site(site_id)}/drive"


@tool("sharepoint")
def sharepoint_sites(query: str = "*", limit: int = 25) -> list[dict[str, Any]]:
    """Search SharePoint sites the user can access ('*' = all). Work/school accounts only."""
    return [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "display_name": s.get("displayName"),
            "web_url": s.get("webUrl"),
        }
        for s in _g().paged("/sites", params={"search": query}, limit=limit)
    ]


@tool("sharepoint")
def sharepoint_drives(site_id: str) -> list[dict[str, Any]]:
    """List the document libraries (drives) of a SharePoint site. Work/school accounts only."""
    return [
        {"id": d.get("id"), "name": d.get("name"), "web_url": d.get("webUrl")}
        for d in _g().paged(f"/sites/{_site(site_id)}/drives")
    ]


@tool("sharepoint")
def sharepoint_list(
    site_id: str, path: str = "/", drive_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """List a folder in a SharePoint site's default library (or `drive_id`). `path` is
    absolute ('/' = root) or an item id. `site_id` is ignored when `drive_id` is given.
    Work/school accounts only."""
    ref = item_ref(path, _drive(site_id, drive_id))
    return [summarize(i) for i in _g().paged(f"{ref}/children", limit=limit)]


@tool("sharepoint")
def sharepoint_download(
    site_id: str,
    item: str,
    drive_id: str | None = None,
    dest: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Download a SharePoint file (absolute path or item id) to the local workspace (or `dest`
    inside an allowed directory). `site_id` is ignored when `drive_id` is given.
    Work/school accounts only."""
    data, meta = download_from(_g(), _drive(site_id, drive_id), item)
    return {**summarize(meta), "local_path": write_local(dest, meta["name"], data, overwrite)}


@tool("sharepoint", writes=True)
def sharepoint_upload(
    local_path: str,
    site_id: str,
    remote_path: str,
    drive_id: str | None = None,
    conflict: Literal["fail", "replace", "rename"] = "fail",
) -> dict[str, Any]:
    """Upload a local file (inside an allowed directory) to an absolute path in a SharePoint
    library. `site_id` is ignored when `drive_id` is given. Work/school accounts only."""
    data = read_local(local_path).read_bytes()
    return create_bytes(remote_path, data, conflict, _g(), _drive(site_id, drive_id))
