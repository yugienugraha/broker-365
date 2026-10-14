import json

import pytest

from broker365 import config, services
from broker365.errors import ConflictError, PathNotAllowed, ReadOnlyMode
from broker365.services import files
from tests.conftest import G

ITEM = {
    "id": "ABC!1",
    "name": "a.txt",
    "size": 5,
    "eTag": '"etag-1"',
    "file": {},
    "parentReference": {"path": "/drive/root:/Docs"},
}


def test_list_follows_paging(httpx_mock):
    httpx_mock.add_response(
        url=f"{G}/me/drive/root:/Docs:/children?%24top=100",
        json={"value": [ITEM], "@odata.nextLink": f"{G}/next"},
    )
    httpx_mock.add_response(url=f"{G}/next", json={"value": [{**ITEM, "id": "2"}]})
    out = files.files_list("/Docs")
    assert [i["id"] for i in out] == ["ABC!1", "2"]
    assert out[0]["path"] == "/Docs/a.txt"
    assert httpx_mock.get_requests()[0].headers["Authorization"] == "Bearer test-token"


def test_retry_after_on_429(httpx_mock):
    httpx_mock.add_response(
        url=f"{G}/me/drive/items/ABC!1", status_code=429, headers={"Retry-After": "1"}
    )
    httpx_mock.add_response(url=f"{G}/me/drive/items/ABC!1", json=ITEM)
    assert files.files_get("ABC!1")["name"] == "a.txt"


def test_download_follows_redirect_without_token(httpx_mock):
    httpx_mock.add_response(url=f"{G}/me/drive/root:/Docs/a.txt:", json=ITEM)
    httpx_mock.add_response(
        url=f"{G}/me/drive/items/ABC!1/content",
        status_code=302,
        headers={"Location": "https://dl.example.com/x"},
    )
    httpx_mock.add_response(url="https://dl.example.com/x", content=b"hello")
    out = files.files_download("/Docs/a.txt")
    assert open(out["local_path"], "rb").read() == b"hello"
    assert "Authorization" not in httpx_mock.get_requests()[-1].headers


def test_download_refuses_path_outside_sandbox(httpx_mock, tmp_path):
    httpx_mock.add_response(url=f"{G}/me/drive/items/ABC!1", json=ITEM)
    httpx_mock.add_response(url=f"{G}/me/drive/items/ABC!1/content", content=b"x")
    with pytest.raises(PathNotAllowed):
        files.files_download("ABC!1", dest=str(tmp_path / "elsewhere" / "a.txt"))


def test_resolve_local_blocks_traversal():
    with pytest.raises(PathNotAllowed):
        config.resolve_local("../../etc/passwd")


def test_upload_small_uses_conflict_behavior(httpx_mock):
    src = config.workspace() / "up.txt"
    src.write_text("hi")
    httpx_mock.add_response(
        method="PUT",
        url=f"{G}/me/drive/root:/Docs/up.txt:/content?%40microsoft.graph.conflictBehavior=fail",
        json={**ITEM, "name": "up.txt"},
    )
    assert files.files_upload("up.txt", "/Docs/up.txt")["name"] == "up.txt"


def test_large_upload_uses_session(httpx_mock, monkeypatch):
    from broker365 import graph

    monkeypatch.setattr(graph, "SIMPLE_UPLOAD_MAX", 4)
    monkeypatch.setattr(graph, "UPLOAD_CHUNK", 4)
    httpx_mock.add_response(
        method="POST",
        url=f"{G}/me/drive/root:/big.bin:/createUploadSession",
        json={"uploadUrl": "https://up.example.com/s"},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://up.example.com/s",
        status_code=202,
        json={"nextExpectedRanges": ["4-"]},
    )
    httpx_mock.add_response(method="PUT", url="https://up.example.com/s", json=ITEM)
    files.create_bytes("/big.bin", b"123456")
    puts = [r for r in httpx_mock.get_requests() if r.method == "PUT"]
    assert [r.headers["Content-Range"] for r in puts] == ["bytes 0-3/6", "bytes 4-5/6"]
    assert all("Authorization" not in r.headers for r in puts)


def test_replace_bytes_sends_if_match_and_maps_412(httpx_mock):
    httpx_mock.add_response(
        method="PUT",
        url=f"{G}/me/drive/items/ABC!1/content",
        status_code=412,
        json={"error": {"code": "resourceModified", "message": "changed"}},
    )
    with pytest.raises(ConflictError):
        files.replace_bytes(ITEM, b"new")
    assert httpx_mock.get_requests()[0].headers["If-Match"] == '"etag-1"'


def test_refuses_foreign_absolute_url():
    from broker365 import graph

    with pytest.raises(Exception, match="non-Graph host"):
        graph.client("files").get("https://evil.example.com/steal")


def test_mkdir(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{G}/me/drive/root:/Projects:/children",
        json={**ITEM, "name": "2026", "folder": {}},
    )
    out = files.files_mkdir("/Projects/2026")
    assert out["type"] == "folder"
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["name"] == "2026"


def test_read_only_blocks_writes(monkeypatch):
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    with pytest.raises(ReadOnlyMode):
        services.call("files_delete", item="ABC!1")
    assert "files_delete" not in [t.name for t in services.available_tools()]


def test_post_not_retried_on_503(httpx_mock):
    from broker365.errors import GraphError

    httpx_mock.add_response(method="POST", url=f"{G}/me/sendMail", status_code=503)
    with pytest.raises(GraphError):
        __import__("broker365.graph").graph.client("mail").post("/me/sendMail", json={})
    assert len(httpx_mock.get_requests()) == 1


def test_network_error_is_broker_error(httpx_mock):
    import httpx

    from broker365.errors import NetworkError

    httpx_mock.add_exception(httpx.ConnectTimeout("boom"))
    with pytest.raises(NetworkError, match="ConnectTimeout"):
        files.files_get("ABC!1")


def test_download_uses_preauthenticated_url(httpx_mock):
    meta = {**ITEM, "@microsoft.graph.downloadUrl": "https://dl.example.com/pre"}
    httpx_mock.add_response(url=f"{G}/me/drive/items/ABC!1", json=meta)
    httpx_mock.add_response(url="https://dl.example.com/pre", content=b"data")
    data, _ = files.download_bytes("ABC!1")
    assert data == b"data"
    assert "Authorization" not in httpx_mock.get_requests()[-1].headers


def test_move_by_id_skips_parent_lookup(httpx_mock):
    httpx_mock.add_response(method="PATCH", url=f"{G}/me/drive/items/ABC!1", json=ITEM)
    files.files_move("ABC!1", new_parent="PARENT!9")
    assert json.loads(httpx_mock.get_requests()[0].content)["parentReference"] == {"id": "PARENT!9"}


REMOTE_PATHS = [
    "\\\\evil.example\\share\\a.pdf",
    "//evil.example/share/a",
    "\\\\evil@80\\x\\y",
    "\\\\?\\C:\\Windows\\win.ini",
    "\\\\.\\pipe\\x",
    "C:secret.txt",
]


@pytest.fixture
def no_resolve(monkeypatch, tmp_path):
    """Windows rules on any OS; fail loudly if anything resolves (touches) a path."""
    from pathlib import Path

    monkeypatch.setattr(config, "_WINDOWS", True)
    monkeypatch.setattr(config, "allowed_dirs", lambda: [tmp_path / "ws"])

    def boom(self, *a, **k):
        raise AssertionError(f"resolve() must not run for {self}")

    monkeypatch.setattr(Path, "resolve", boom)


@pytest.mark.parametrize("path", REMOTE_PATHS)
def test_resolve_local_rejects_remote_paths_before_touching_them(path, no_resolve):
    with pytest.raises(PathNotAllowed, match="UNC"):
        config.resolve_local(path)


def test_remote_rules_only_apply_on_windows(monkeypatch):
    monkeypatch.setattr(config, "_WINDOWS", False)
    assert not any(config._is_remote(p) for p in REMOTE_PATHS)


def test_absolute_path_inside_workspace_still_allowed():
    target = config.workspace() / "sub" / "a.pdf"
    assert config.resolve_local(str(target)) == target


@pytest.mark.skipif(config.os.name != "nt", reason="UNC paths are Windows-only")
def test_explicitly_allowed_share_is_usable(monkeypatch):
    from pathlib import Path

    share = Path("\\\\nas\\team")
    monkeypatch.setattr(config, "allowed_dirs", lambda: [config.workspace(), share])
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: self)  # no real SMB in tests
    assert config.resolve_local("\\\\nas\\team\\r.pdf") == share / "r.pdf"
