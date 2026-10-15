import json
import re

import httpx
import pytest

from broker365 import config, gapi, services
from broker365.errors import Broker365Error, PathNotAllowed, ReadOnlyMode
from broker365.services import gdrive

DRIVE = gapi.DRIVE
UPLOAD = gapi.DRIVE_UPLOAD
FOLDER = gdrive.FOLDER


def item(id, name, mime="text/plain", parent="ROOT", **extra):
    return {
        "id": id,
        "name": name,
        "mimeType": mime,
        "parents": [parent],
        "size": "5",
        "webViewLink": f"https://drive.google.com/file/d/{id}/view",
        "owners": [{"emailAddress": "pat@gmail.com"}],
        "shared": False,
        "trashed": False,
        **extra,
    }


ROOT = {"id": "ROOT", "name": "My Drive", "mimeType": FOLDER}


def _unq(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


class FakeDrive:
    """GET files/{id} and the files.list queries the tools send for path resolution and
    listing; everything else is mocked per test."""

    def __init__(self, httpx_mock):
        self.items: dict[str, dict] = {"ROOT": ROOT}
        self.queries: list[str] = []
        self.incomplete = False  # what files.list reports as incompleteSearch
        httpx_mock.add_callback(
            self._list,
            method="GET",
            url=re.compile(re.escape(f"{DRIVE}/files") + r"(\?.*)?$"),
            is_reusable=True,
            is_optional=True,
        )
        httpx_mock.add_callback(
            self._get,
            method="GET",
            # metadata only: content (alt=media) is mocked per test
            url=re.compile(re.escape(f"{DRIVE}/files/") + r"[^/?]+(\?(?!.*alt=media).*)?$"),
            is_reusable=True,
            is_optional=True,
        )

    def add(self, *metas):
        for m in metas:
            self.items[m["id"]] = m

    def _get(self, request):
        file_id = request.url.path.rsplit("/", 1)[1]
        if file_id == "root":
            return httpx.Response(200, json=ROOT)
        if file_id in self.items:
            return httpx.Response(200, json=self.items[file_id])
        body = {"error": {"code": 404, "message": f"File not found: {file_id}.", "errors": []}}
        return httpx.Response(404, json=body)

    def _list(self, request):
        q = request.url.params["q"]
        self.queries.append(q)
        assert request.url.params["supportsAllDrives"] == "true"
        assert request.url.params["includeItemsFromAllDrives"] == "true"
        name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        prefix = re.match(r"name contains '((?:[^'\\]|\\.)*)'", q)
        parent = re.search(r"'((?:[^'\\]|\\.)*)' in parents", q)
        parent_id = parent and _unq(parent.group(1)).replace("root", "ROOT")
        found = [
            m
            for m in self.items.values()
            if m is not ROOT
            and (not name or m["name"] == _unq(name.group(1)))
            and (not prefix or m["name"].startswith(_unq(prefix.group(1))))
            and (not parent or parent_id in m.get("parents", []))
            and ("trashed = false" not in q or not m.get("trashed"))
            and (
                "mimeType = 'application/vnd.google-apps.folder'" not in q
                or m["mimeType"] == FOLDER
            )
        ]
        return httpx.Response(200, json={"files": found, "incompleteSearch": self.incomplete})


@pytest.fixture
def drive(httpx_mock):
    d = FakeDrive(httpx_mock)
    d.add(
        item("D1", "Docs", FOLDER),
        item("F1", "a.txt", parent="D1"),
        item("DOC", "Plan", gdrive.DOC, parent="D1"),
        item("SHEET", "Budget", gdrive.SHEET, parent="D1"),
        item("BIN", "photo.png", "image/png", parent="D1"),
    )
    return d


def at(method, prefix):
    """httpx_mock matcher for `method` on any URL starting with `prefix`."""
    return {"method": method, "url": re.compile(re.escape(prefix) + r".*")}


def sent(httpx_mock, method, prefix=""):
    """Requests sent with `method` to URLs starting with `prefix`."""
    return [
        r for r in httpx_mock.get_requests() if r.method == method and str(r.url).startswith(prefix)
    ]


# ---- addressing -------------------------------------------------------------------------


def test_path_resolution_walks_names(drive, httpx_mock):
    out = gdrive.gdrive_get("/Docs/a.txt")
    assert out["id"] == "F1" and out["kind"] == "file" and out["size"] == 5
    assert out["owners"] == ["pat@gmail.com"]
    assert drive.queries == [
        f"name = 'Docs' and 'root' in parents and trashed = false and mimeType = '{FOLDER}'",
        "name = 'a.txt' and 'D1' in parents and trashed = false",
    ]
    probe, last = (r.url.params for r in httpx_mock.get_requests())  # no GET of files/root
    assert probe["fields"] == "nextPageToken,incompleteSearch,files(id,name,mimeType,driveId)"
    assert probe["pageSize"] == "2"
    assert last["fields"] == f"nextPageToken,incompleteSearch,files({gdrive.FIELDS})"
    assert probe["corpora"] == last["corpora"] == "user" and "driveId" not in last


def test_path_names_are_escaped(drive):
    drive.add(item("Q", "O'Brien \\ notes.txt", parent="D1"))
    assert gdrive.gdrive_get("/Docs/O'Brien \\ notes.txt")["id"] == "Q"
    assert drive.queries[-1].startswith("name = 'O\\'Brien \\\\ notes.txt' and")


def test_ambiguous_path_lists_candidates(drive):
    drive.add(item("F2", "a.txt", "text/markdown", parent="D1"))
    with pytest.raises(Broker365Error, match=r"F1 \(text/plain\); F2 \(text/markdown\).*file id"):
        gdrive.gdrive_get("/Docs/a.txt")


def test_missing_path(drive):
    with pytest.raises(Broker365Error, match="'/Docs/zz.txt' not found"):
        gdrive.gdrive_get("/Docs/zz.txt")


def test_unknown_id_is_friendly_404(drive):
    with pytest.raises(Broker365Error, match="not found or no access"):
        gdrive.gdrive_get("NOPE")


def test_ids_are_quoted_into_one_segment(drive, httpx_mock):
    with pytest.raises(Broker365Error):
        gdrive.gdrive_get("../about")
    assert all(b"/about" not in r.url.raw_path for r in httpx_mock.get_requests())


# ---- read tools -------------------------------------------------------------------------


def test_list_root(drive, httpx_mock):
    out = gdrive.gdrive_list("/")
    assert [f["name"] for f in out] == ["Docs"]
    assert out[0]["kind"] == "folder"
    params = httpx_mock.get_requests()[-1].url.params
    assert params["q"] == "'ROOT' in parents and trashed = false"
    assert params["corpora"] == "user" and params["orderBy"] == "folder,name"


def test_list_kinds_and_trashed(drive):
    drive.add(item("T", "old.txt", parent="D1", trashed=True))
    kinds = {f["name"]: f["kind"] for f in gdrive.gdrive_list("/Docs")}
    assert kinds == {
        "a.txt": "file",
        "Plan": "google-doc",
        "Budget": "google-sheet",
        "photo.png": "file",
    }
    assert "old.txt" in {f["name"] for f in gdrive.gdrive_list("D1", include_trashed=True)}


def test_list_refuses_file(drive):
    with pytest.raises(Broker365Error, match="not a folder"):
        gdrive.gdrive_list("F1")


def test_search_full_text_and_raw(drive):
    gdrive.gdrive_search("it's")
    assert drive.queries[-1] == (
        "(fullText contains 'it\\'s' or name contains 'it\\'s') and trashed = false"
    )
    gdrive.gdrive_search("mimeType = 'application/pdf'", raw=True)
    assert drive.queries[-1] == "mimeType = 'application/pdf'"


def test_read_text_native_doc_exports_plain_text(drive, httpx_mock):
    httpx_mock.add_response(
        url=f"{DRIVE}/files/DOC/export?mimeType=text%2Fplain", content="﻿Hi".encode()
    )
    out = gdrive.gdrive_read_text("DOC")
    assert out["content"] == "Hi" and out["truncated"] is False


def test_read_text_sheet_is_first_sheet_csv(drive, httpx_mock):
    httpx_mock.add_response(url=f"{DRIVE}/files/SHEET/export?mimeType=text%2Fcsv", content=b"a,b")
    out = gdrive.gdrive_read_text("/Docs/Budget")
    assert out["content"] == "a,b" and "first sheet" in out["note"]


def test_read_text_plain_file_truncates(drive, httpx_mock):
    httpx_mock.add_response(
        url=f"{DRIVE}/files/F1?supportsAllDrives=true&alt=media", content=b"hello world"
    )
    out = gdrive.gdrive_read_text("F1", max_bytes=5)
    assert out["content"] == "hello" and out["truncated"] is True


def test_read_text_binary_points_to_download(drive):
    with pytest.raises(Broker365Error, match="gdrive_download"):
        gdrive.gdrive_read_text("BIN")


def test_download_binary(drive, httpx_mock):
    httpx_mock.add_response(
        url=f"{DRIVE}/files/BIN?supportsAllDrives=true&alt=media", content=b"\x89PNG"
    )
    out = gdrive.gdrive_download("/Docs/photo.png")
    assert open(out["local_path"], "rb").read() == b"\x89PNG"
    assert out["local_path"].endswith("photo.png")


def test_download_native_points_to_export(drive):
    with pytest.raises(Broker365Error, match="gdrive_export"):
        gdrive.gdrive_download("DOC")


def test_download_refuses_dest_outside_sandbox(drive, httpx_mock, tmp_path):
    with pytest.raises(PathNotAllowed):
        gdrive.gdrive_download("BIN", dest=str(tmp_path / "elsewhere" / "p.png"))
    assert not any("alt=media" in str(r.url) for r in httpx_mock.get_requests())


def test_download_over_cap_leaves_nothing_behind(drive, httpx_mock, monkeypatch):
    monkeypatch.setattr(gapi, "DOWNLOAD_MAX", 3)
    httpx_mock.add_response(
        url=f"{DRIVE}/files/BIN?supportsAllDrives=true&alt=media", content=b"x" * 9
    )
    with pytest.raises(Broker365Error, match="larger than 3 bytes"):
        gdrive.gdrive_download("BIN")
    assert list(config.workspace().iterdir()) == []


def test_download_keeps_existing_file_without_overwrite(drive, httpx_mock):
    (config.workspace() / "photo.png").write_bytes(b"old")
    with pytest.raises(Broker365Error, match="overwrite=true"):
        gdrive.gdrive_download("BIN")
    httpx_mock.add_response(
        url=f"{DRIVE}/files/BIN?supportsAllDrives=true&alt=media", content=b"new"
    )
    out = gdrive.gdrive_download("BIN", overwrite=True)
    assert open(out["local_path"], "rb").read() == b"new"
    assert sorted(p.name for p in config.workspace().iterdir()) == ["photo.png"]


def test_download_name_with_slash_stays_one_file(drive, httpx_mock):
    drive.add(item("S", "../a/b.txt", "application/octet-stream"))
    httpx_mock.add_response(url=f"{DRIVE}/files/S?supportsAllDrives=true&alt=media", content=b"x")
    out = gdrive.gdrive_download("S")
    assert out["local_path"] == str(config.workspace() / "_a_b.txt")


def test_export_doc_as_docx(drive, httpx_mock):
    mime = gdrive.FORMAT_MIME["docx"]
    httpx_mock.add_response(
        url=httpx.URL(f"{DRIVE}/files/DOC/export", params={"mimeType": mime}), content=b"PK"
    )
    out = gdrive.gdrive_export("DOC", "docx")
    assert out["local_path"].endswith("Plan.docx")
    assert open(out["local_path"], "rb").read() == b"PK"


def test_export_validates_format_per_type(drive):
    with pytest.raises(Broker365Error, match="google-doc can be exported as: pdf, docx"):
        gdrive.gdrive_export("DOC", "xlsx")
    with pytest.raises(Broker365Error, match="gdrive_download"):
        gdrive.gdrive_export("F1", "pdf")


def test_export_size_limit_is_explained(drive, httpx_mock):
    httpx_mock.add_response(
        url=f"{DRIVE}/files/SHEET/export?mimeType=application%2Fpdf",
        status_code=403,
        json={"error": {"code": 403, "message": "too large", "errors": [
            {"reason": "exportSizeLimitExceeded"}
        ]}},
    )  # fmt: skip
    with pytest.raises(Broker365Error, match="10 MB"):
        gdrive.gdrive_export("SHEET", "pdf")


# ---- write tools ------------------------------------------------------------------------


def _local(name="up.txt", data=b"hi"):
    (config.workspace() / name).write_bytes(data)
    return name


def _multipart_meta(request):
    boundary = request.headers["Content-Type"].split("boundary=")[1]
    part = request.content.split(f"--{boundary}".encode())[1]
    return json.loads(part.split(b"\r\n\r\n", 1)[1])


def test_upload_new_file(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST", url=re.compile(re.escape(UPLOAD) + r"\?.*"), json=item("N", "up.txt")
    )
    out = gdrive.gdrive_upload(_local(), "/Docs")
    assert out["id"] == "N"
    req = sent(httpx_mock, "POST")[0]
    assert req.url.params["uploadType"] == "multipart"
    assert req.url.params["supportsAllDrives"] == "true"
    assert _multipart_meta(req) == {"name": "up.txt", "parents": ["D1"]}


def test_upload_conflict_fail(drive):
    with pytest.raises(Broker365Error, match="already exists.*F1.*conflict='replace'"):
        gdrive.gdrive_upload(_local("a.txt"), "/Docs")


def test_upload_conflict_replace_updates_content(drive, httpx_mock):
    httpx_mock.add_response(
        method="PATCH",
        url=re.compile(re.escape(f"{UPLOAD}/F1") + r"\?.*"),
        json=item("F1", "a.txt"),
    )
    out = gdrive.gdrive_upload(_local("a.txt"), "D1", conflict="replace")
    assert out["id"] == "F1" and out["replaced"] is True
    assert _multipart_meta(sent(httpx_mock, "PATCH")[0]) == {}


def test_upload_conflict_replace_needs_single_match(drive):
    drive.add(item("F2", "a.txt", parent="D1"))
    with pytest.raises(Broker365Error, match="Several items"):
        gdrive.gdrive_upload(_local("a.txt"), "D1", conflict="replace")


def test_upload_conflict_rename(drive, httpx_mock):
    drive.add(item("F2", "a (1).txt", parent="D1"))
    httpx_mock.add_response(
        method="POST", url=re.compile(re.escape(UPLOAD) + r"\?.*"), json=item("N", "a (2).txt")
    )
    drive.add(item("F3", "a (3).txt", parent="D1"))
    gdrive.gdrive_upload(_local("a.txt"), "/Docs", conflict="rename")
    assert _multipart_meta(sent(httpx_mock, "POST")[0])["name"] == "a (2).txt"
    # one listing of the folder's names (not `name contains`, which matches tokens only)
    scans = [
        r for r in sent(httpx_mock, "GET") if r.url.params.get("fields", "").endswith("files(name)")
    ]
    assert [r.url.params["q"] for r in scans] == ["'D1' in parents and trashed = false"]
    assert "contains" not in "".join(drive.queries)


def test_upload_convert_docx(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=re.compile(re.escape(UPLOAD) + r"\?.*"),
        json=item("N", "Report", gdrive.DOC),
    )
    out = gdrive.gdrive_upload(_local("Report.docx", b"PK"), "/Docs", convert=True)
    assert out["kind"] == "google-doc"
    req = sent(httpx_mock, "POST")[0]
    assert _multipart_meta(req) == {"name": "Report", "parents": ["D1"], "mimeType": gdrive.DOC}
    assert gdrive.FORMAT_MIME["docx"].encode() in req.content


@pytest.mark.parametrize(
    ("filename", "native"),
    [
        ("n.md", gdrive.DOC),
        ("n.txt", gdrive.DOC),
        ("n.tsv", gdrive.SHEET),
        ("n.odp", gdrive.SLIDES),
    ],
)
def test_upload_convert_other_importable_types(drive, httpx_mock, filename, native):
    httpx_mock.add_response(
        method="POST", url=re.compile(re.escape(UPLOAD) + r"\?.*"), json=item("N", "n", native)
    )
    gdrive.gdrive_upload(_local(filename), "/Docs", convert=True)
    assert _multipart_meta(sent(httpx_mock, "POST")[0])["mimeType"] == native


def test_upload_convert_rejects_other_types(drive):
    with pytest.raises(Broker365Error, match="convert=true supports"):
        gdrive.gdrive_upload(_local("x.png"), "/Docs", convert=True)


def test_upload_refuses_local_path_outside_sandbox(tmp_path, httpx_mock):
    secret = tmp_path / "outside.txt"
    secret.write_text("x")
    with pytest.raises(PathNotAllowed):
        gdrive.gdrive_upload(str(secret), "/")
    assert httpx_mock.get_requests() == []


def test_write_text(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST", url=re.compile(re.escape(UPLOAD) + r"\?.*"), json=item("N", "n.md")
    )
    gdrive.gdrive_write_text("/Docs/n.md", "# hi")
    req = sent(httpx_mock, "POST")[0]
    assert _multipart_meta(req) == {"name": "n.md", "parents": ["D1"]}
    assert b"Content-Type: text/markdown\r\n\r\n# hi" in req.content


def test_write_text_requires_absolute_path(drive):
    with pytest.raises(Broker365Error, match="absolute"):
        gdrive.gdrive_write_text("n.md", "x")


def test_mkdir_creates(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=re.compile(re.escape(f"{DRIVE}/files") + r"\?.*"),
        json=item("NEW", "2026", FOLDER, parent="D1"),
    )
    out = gdrive.gdrive_mkdir("/Docs/2026")
    assert out["kind"] == "folder" and "existed" not in out
    body = json.loads(sent(httpx_mock, "POST")[0].content)
    assert body == {"name": "2026", "mimeType": FOLDER, "parents": ["D1"]}


def test_mkdir_existing_is_idempotent(drive, httpx_mock):
    out = gdrive.gdrive_mkdir("/Docs")
    assert out["id"] == "D1" and out["existed"] is True
    assert sent(httpx_mock, "POST") == []


def test_move_swaps_parents_and_renames(drive, httpx_mock):
    drive.add(item("D2", "Archive", FOLDER))
    httpx_mock.add_response(
        method="PATCH",
        url=re.compile(re.escape(f"{DRIVE}/files/F1") + r"\?.*"),
        json=item("F1", "b.txt", parent="D2"),
    )
    gdrive.gdrive_move("/Docs/a.txt", new_parent="/Archive", new_name="b.txt")
    req = sent(httpx_mock, "PATCH")[0]
    assert req.url.params["addParents"] == "D2" and req.url.params["removeParents"] == "D1"
    assert json.loads(req.content) == {"name": "b.txt"}


def test_move_needs_a_change(drive):
    with pytest.raises(Broker365Error, match="new_parent and/or new_name"):
        gdrive.gdrive_move("F1")


def test_copy(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=re.compile(re.escape(f"{DRIVE}/files/F1/copy") + r"\?.*"),
        json=item("C", "c.txt"),
    )
    assert gdrive.gdrive_copy("F1", new_parent="/", new_name="c.txt")["id"] == "C"
    assert json.loads(sent(httpx_mock, "POST")[0].content) == {"name": "c.txt", "parents": ["ROOT"]}


def test_copy_refuses_folder(drive):
    with pytest.raises(Broker365Error, match="is a folder"):
        gdrive.gdrive_copy("D1")


def test_trash_uses_update_not_delete(drive, httpx_mock):
    httpx_mock.add_response(
        method="PATCH",
        url=re.compile(re.escape(f"{DRIVE}/files/F1") + r"\?.*"),
        json=item("F1", "a.txt", parent="D1", trashed=True),
    )
    out = gdrive.gdrive_trash("/Docs/a.txt")
    assert out["trashed"] is True
    assert json.loads(sent(httpx_mock, "PATCH")[0].content) == {"trashed": True}
    assert sent(httpx_mock, "DELETE") == []


def test_share_with_user(drive, httpx_mock):
    perm = {"id": "P", "role": "writer", "type": "user", "emailAddress": "lee@x.com"}
    httpx_mock.add_response(
        method="POST",
        url=re.compile(re.escape(f"{DRIVE}/files/F1/permissions") + r"\?.*"),
        json=perm,
    )
    out = gdrive.gdrive_share("F1", email="lee@x.com", role="writer", notify=True, message="hi")
    assert out["permission"] == perm and out["webViewLink"].endswith("/F1/view")
    req = sent(httpx_mock, "POST")[0]
    assert json.loads(req.content) == {
        "role": "writer",
        "type": "user",
        "emailAddress": "lee@x.com",
    }
    assert req.url.params["sendNotificationEmail"] == "true"
    assert req.url.params["emailMessage"] == "hi"


def test_share_anyone_with_link(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=re.compile(re.escape(f"{DRIVE}/files/F1/permissions") + r"\?.*"),
        json={"id": "anyoneWithLink"},
    )
    out = gdrive.gdrive_share("F1", type="anyone")
    assert out["access"] == "anyone with the link"
    req = sent(httpx_mock, "POST")[0]
    assert json.loads(req.content) == {
        "role": "reader",
        "type": "anyone",
        "allowFileDiscovery": False,
    }
    assert "sendNotificationEmail" not in req.url.params


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"email": "a@b.c", "role": "owner"}, "role must be"),
        ({"email": "a@b.c", "type": "everyone"}, "type must be"),
        ({"type": "user"}, "needs an email"),
        ({"type": "group", "email": "nope"}, "needs an email"),
        ({"type": "domain"}, "needs a domain"),
    ],
)
def test_share_validation(kwargs, match, httpx_mock):
    with pytest.raises(Broker365Error, match=match):
        gdrive.gdrive_share("F1", **kwargs)
    assert httpx_mock.get_requests() == []


# ---- registry ---------------------------------------------------------------------------


def test_tools_are_google_gdrive_and_flagged():
    tools = {t.name: t for t in services.load_all().values() if t.name.startswith("gdrive_")}
    assert all(t.provider == "google" and t.bundle == "gdrive" for t in tools.values())
    writes = {n for n, t in tools.items() if t.writes}
    assert writes == {
        "gdrive_upload", "gdrive_write_text", "gdrive_mkdir", "gdrive_move", "gdrive_copy",
        "gdrive_trash", "gdrive_share",
    }  # fmt: skip
    assert {n for n, t in tools.items() if t.destructive} == {"gdrive_trash", "gdrive_share"}


def test_read_only_hides_and_blocks_write_tools(monkeypatch):
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    visible = {t.name for t in services.available_tools()}
    assert "gdrive_list" in visible and "gdrive_export" in visible
    assert not {"gdrive_upload", "gdrive_trash", "gdrive_share"} & visible
    with pytest.raises(ReadOnlyMode):
        services.call("gdrive_trash", item="F1")


def test_audit_records_targets_not_content(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST", url=re.compile(re.escape(UPLOAD) + r"\?.*"), json=item("N", "n.txt")
    )
    services.call("gdrive_write_text", remote_path="/Docs/n.txt", text="top secret body")
    log = (config.broker_home() / "audit.log").read_text(encoding="utf-8")
    entry = json.loads(log.splitlines()[-1])
    assert entry["tool"] == "gdrive_write_text" and entry["target"]["remote_path"] == "/Docs/n.txt"
    assert "top secret body" not in log


def test_share_audit_logs_recipient_role_and_type(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=re.compile(re.escape(f"{DRIVE}/files/F1/permissions") + r"\?.*"),
        json={"id": "P"},
    )
    services.call(
        "gdrive_share", item="F1", email="lee@x.com", role="writer", type="user", message="hi"
    )
    entry = json.loads((config.broker_home() / "audit.log").read_text().splitlines()[-1])
    assert entry["target"] == {"item": "F1", "email": "lee@x.com", "role": "writer", "type": "user"}
    assert "hi" not in json.dumps(entry)  # the message is content


def test_audit_extra_keys_are_per_tool():
    tools = services.load_all()
    assert tools["gdrive_share"].audit == ("email", "role", "type", "domain")
    assert tools["gdrive_upload"].audit == ("remote_folder", "conflict", "convert")
    assert tools["files_upload"].audit == ()


# ---- drive scoping, incomplete searches ---------------------------------------------------


def test_shared_drive_folder_queries_use_its_drive(drive, httpx_mock):
    drive.add(
        item("SD", "Team", FOLDER, parent="SDROOT", driveId="DRV1"),
        item("SF", "plan.txt", parent="SD", driveId="DRV1"),
    )
    gdrive.gdrive_list("SD")
    params = httpx_mock.get_requests()[-1].url.params
    assert params["corpora"] == "drive" and params["driveId"] == "DRV1"
    assert params["includeItemsFromAllDrives"] == "true"


def test_path_walk_carries_drive_id(drive, httpx_mock):
    drive.add(
        item("SD", "Team", FOLDER, driveId="DRV1"),
        item("SF", "plan.txt", parent="SD", driveId="DRV1"),
    )
    assert gdrive.gdrive_get("/Team/plan.txt")["id"] == "SF"
    first, second = (r.url.params for r in httpx_mock.get_requests())
    assert first["corpora"] == "user"
    assert second["corpora"] == "drive" and second["driveId"] == "DRV1"


def test_incomplete_probe_is_an_error(drive):
    drive.incomplete = True
    with pytest.raises(Broker365Error, match="incomplete.*file id"):
        gdrive.gdrive_get("/Docs/a.txt")


def test_search_uses_all_drives_and_flags_incomplete(drive, httpx_mock):
    out = gdrive.gdrive_search("a")
    assert isinstance(out, list)
    assert httpx_mock.get_requests()[-1].url.params["corpora"] == "allDrives"
    drive.incomplete = True
    out = gdrive.gdrive_search("a")
    assert out["incompleteSearch"] is True and isinstance(out["files"], list)


# ---- conversion, sharing, export streaming --------------------------------------------------


def test_convert_cannot_replace(drive, httpx_mock):
    drive.add(item("R", "Report", gdrive.DOC, parent="D1"))
    with pytest.raises(Broker365Error, match="can't be replaced while converting.*rename"):
        gdrive.gdrive_upload(
            _local("Report.docx", b"PK"), "/Docs", conflict="replace", convert=True
        )
    assert sent(httpx_mock, "POST") == sent(httpx_mock, "PATCH") == []


def test_share_with_non_google_address_explains_notify(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=re.compile(re.escape(f"{DRIVE}/files/F1/permissions") + r"\?.*"),
        status_code=400,
        json={"error": {"code": 400, "message": "Bad Request", "errors": [
            {"reason": "invalidSharingRequest", "message": "...notify..."}
        ]}},
    )  # fmt: skip
    with pytest.raises(Broker365Error, match="no Google account.*notify=true"):
        gdrive.gdrive_share("F1", email="x@example.org")


def test_share_other_400_is_not_rewritten(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=re.compile(re.escape(f"{DRIVE}/files/F1/permissions") + r"\?.*"),
        status_code=400,
        json={"error": {"code": 400, "message": "bad role", "errors": [{"reason": "invalid"}]}},
    )
    with pytest.raises(Broker365Error, match="Google 400 invalid: bad role"):
        gdrive.gdrive_share("F1", email="x@example.org")


def test_export_streams_and_leaves_nothing_on_size_error(drive, httpx_mock):
    httpx_mock.add_response(
        url=f"{DRIVE}/files/SHEET/export?mimeType=application%2Fpdf",
        status_code=403,
        json={"error": {"code": 403, "message": "big", "errors": [
            {"reason": "exportSizeLimitExceeded"}
        ]}},
    )  # fmt: skip
    with pytest.raises(Broker365Error, match="10 MB"):
        gdrive.gdrive_export("SHEET", "pdf")
    assert list(config.workspace().iterdir()) == []
