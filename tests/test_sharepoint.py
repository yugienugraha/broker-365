import re

import pytest

from broker365 import config, graph
from broker365.errors import Broker365Error, PathNotAllowed
from broker365.services import sharepoint
from tests.conftest import G

SITE = "contoso.sharepoint.com,aaa,bbb"
SITE_SEG = "contoso.sharepoint.com,aaa,bbb"
ITEM = {
    "id": "01X!2",
    "name": "a.txt",
    "size": 5,
    "file": {},
    "parentReference": {"path": "/drives/d1/root:/Shared"},
}


def test_sites_search_params(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{G}/sites?") + ".*"),
        json={"value": [{"id": SITE, "name": "team", "displayName": "Team", "webUrl": "u"}]},
    )
    out = sharepoint.sharepoint_sites("team", limit=5)
    assert out == [{"id": SITE, "name": "team", "display_name": "Team", "web_url": "u"}]
    params = httpx_mock.get_requests()[0].url.params
    assert params["search"] == "team" and params["$top"] == "5"


def test_drive_ref_variants():
    def ref(site, drive_id, item):
        return graph.item_ref(item, sharepoint._drive(site, drive_id))

    assert ref(SITE, None, "/") == f"/sites/{SITE_SEG}/drive/root"
    assert ref(SITE, None, "/Shared/a b.txt") == f"/sites/{SITE_SEG}/drive/root:/Shared/a%20b.txt:"
    assert ref(SITE, None, "01X!2") == f"/sites/{SITE_SEG}/drive/items/01X!2"
    assert ref(SITE, "b!d/1", "/Docs/") == "/drives/b%21d%2F1/root:/Docs:"
    assert ref(SITE, "d1", "01X!2") == "/drives/d1/items/01X!2"
    with pytest.raises(Broker365Error):
        ref(SITE, None, "")


def test_drives_and_list(httpx_mock):
    httpx_mock.add_response(
        url=f"{G}/sites/{SITE_SEG}/drives?%24top=100",
        json={"value": [{"id": "d1", "name": "Documents", "webUrl": "u"}]},
    )
    httpx_mock.add_response(
        url=f"{G}/drives/d1/root:/Shared:/children?%24top=100", json={"value": [ITEM]}
    )
    assert sharepoint.sharepoint_drives(SITE)[0]["name"] == "Documents"
    out = sharepoint.sharepoint_list(SITE, "/Shared", drive_id="d1")
    assert out[0]["path"] == "/Shared/a.txt" and out[0]["type"] == "file"


def test_download_via_download_url_without_token(httpx_mock):
    meta = {**ITEM, "@microsoft.graph.downloadUrl": "https://dl.example.com/x"}
    httpx_mock.add_response(url=f"{G}/sites/{SITE_SEG}/drive/root:/Shared/a.txt:", json=meta)
    httpx_mock.add_response(url="https://dl.example.com/x", content=b"hello")
    out = sharepoint.sharepoint_download(SITE, "/Shared/a.txt")
    assert open(out["local_path"], "rb").read() == b"hello"
    assert "Authorization" not in httpx_mock.get_requests()[-1].headers


def test_download_redirect_fallback(httpx_mock):
    httpx_mock.add_response(url=f"{G}/drives/d1/items/01X!2", json=ITEM)
    httpx_mock.add_response(
        url=f"{G}/drives/d1/items/01X!2/content",
        status_code=302,
        headers={"Location": "https://dl.example.com/y"},
    )
    httpx_mock.add_response(url="https://dl.example.com/y", content=b"yo")
    out = sharepoint.sharepoint_download(SITE, "01X!2", drive_id="d1")
    assert open(out["local_path"], "rb").read() == b"yo"
    assert "Authorization" not in httpx_mock.get_requests()[-1].headers


def test_download_rejects_folder_and_outside_sandbox(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url=f"{G}/sites/{SITE_SEG}/drive/root:/Shared:", json={**ITEM, "folder": {}}
    )
    with pytest.raises(Broker365Error, match="folder"):
        sharepoint.sharepoint_download(SITE, "/Shared")
    meta = {**ITEM, "@microsoft.graph.downloadUrl": "https://dl.example.com/x"}
    httpx_mock.add_response(url=f"{G}/sites/{SITE_SEG}/drive/items/01X!2", json=meta)
    httpx_mock.add_response(url="https://dl.example.com/x", content=b"x")
    with pytest.raises(PathNotAllowed):
        sharepoint.sharepoint_download(SITE, "01X!2", dest=str(tmp_path / "elsewhere" / "a"))


def test_upload_conflict_param(httpx_mock):
    (config.workspace() / "up.txt").write_text("hi")
    httpx_mock.add_response(
        method="PUT",
        url=f"{G}/drives/d1/root:/Shared/up.txt:/content"
        "?%40microsoft.graph.conflictBehavior=rename",
        json={**ITEM, "name": "up.txt"},
    )
    out = sharepoint.sharepoint_upload("up.txt", SITE, "/Shared/up.txt", "d1", "rename")
    assert out["name"] == "up.txt"
    assert httpx_mock.get_requests()[0].content == b"hi"


def test_large_upload_uses_session(httpx_mock, monkeypatch):
    monkeypatch.setattr(graph, "SIMPLE_UPLOAD_MAX", 4)
    (config.workspace() / "big.bin").write_bytes(b"12345")
    httpx_mock.add_response(
        method="POST",
        url=f"{G}/sites/{SITE_SEG}/drive/root:/Shared/big.bin:/createUploadSession",
        json={"uploadUrl": "https://up.example.com/s"},
    )
    httpx_mock.add_response(method="PUT", url="https://up.example.com/s", json=ITEM)
    assert sharepoint.sharepoint_upload("big.bin", SITE, "/Shared/big.bin")["id"] == "01X!2"
    assert "Authorization" not in httpx_mock.get_requests()[-1].headers
    with pytest.raises(Broker365Error, match="absolute"):
        sharepoint.sharepoint_upload("big.bin", SITE, "Shared/big.bin")


def test_site_path_form_kept_literal():
    assert sharepoint._drive("contoso.sharepoint.com:/sites/Team:", None) == (
        "/sites/contoso.sharepoint.com:/sites/Team:/drive"
    )
