import json

import pytest

from broker365 import config, gapi, services
from broker365.errors import Broker365Error, ConflictError, ConsentRequired, ReadOnlyMode
from broker365.services import gdocs, gdrive
from tests.test_gdrive import FakeDrive, _multipart_meta, at, item, sent

DOCS = gdocs.DOCS
DRIVE = gapi.DRIVE
UPLOAD = gapi.DRIVE_UPLOAD


@pytest.fixture
def drive(httpx_mock):
    d = FakeDrive(httpx_mock)
    d.add(
        item("D1", "Docs", gdrive.FOLDER),
        item("DOC", "Plan", gdrive.DOC, parent="D1"),
        item("S1", "Budget", gdrive.SHEET),
    )
    return d


def para(text, style="NORMAL_TEXT", bullet=None, end=None):
    p = {"elements": [{"textRun": {"content": text}}], "paragraphStyle": {"namedStyleType": style}}
    if bullet is not None:
        p["bullet"] = {"listId": "l", "nestingLevel": bullet}
    return {"paragraph": p, **({"endIndex": end} if end else {})}


DOC = {
    "documentId": "DOC",
    "title": "Plan",
    "revisionId": "rev-1",
    "body": {
        "content": [
            {"sectionBreak": {}, "endIndex": 1},
            para("Plan\n", "TITLE"),
            para("Goals\n", "HEADING_2"),
            para("First\n", bullet=0),
            para("Nested\n", bullet=1),
            {
                "table": {
                    "tableRows": [
                        {"tableCells": [{"content": [para("a\n")]}, {"content": [para("b\n")]}]},
                        {"tableCells": [{"content": [para("1\n")]}, {"content": [para("2\n")]}]},
                    ]
                }
            },
            para("Line one\u000bline two\n", end=60),
        ]
    },
}


# ---- create / read ----------------------------------------------------------------------


def test_create_uploads_markdown_for_conversion(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", UPLOAD), json=item("NEW", "Notes", gdrive.DOC, parent="D1")
    )
    out = gdocs.gdocs_create("Notes", "# Title\n\n- **bold** item", "/Docs")
    assert out["id"] == "NEW" and out["kind"] == "google-doc"
    req = sent(httpx_mock, "POST", UPLOAD)[0]
    assert req.url.params["uploadType"] == "multipart"
    assert _multipart_meta(req) == {"name": "Notes", "parents": ["D1"], "mimeType": gdrive.DOC}
    assert b"Content-Type: text/markdown\r\n\r\n# Title" in req.content


def test_create_empty_doc_without_upload(drive, httpx_mock):
    httpx_mock.add_response(**at("POST", f"{DRIVE}/files"), json=item("E", "Empty", gdrive.DOC))
    gdocs.gdocs_create("Empty")
    body = json.loads(sent(httpx_mock, "POST", f"{DRIVE}/files")[0].content)
    assert body == {"name": "Empty", "mimeType": gdrive.DOC, "parents": ["root"]}
    # the root needs no lookup: only the name probe went to Drive
    assert [r.url.path for r in sent(httpx_mock, "GET", DRIVE)] == ["/drive/v3/files"]


def test_create_refuses_taken_title(drive):
    with pytest.raises(Broker365Error, match="already exists"):
        gdocs.gdocs_create("Plan", "x", "/Docs")


def test_read_flattens_structure(drive, httpx_mock):
    httpx_mock.add_response(**at("GET", f"{DOCS}/DOC"), json=DOC)
    out = gdocs.gdocs_read("/Docs/Plan")
    assert out["content"] == (
        "# Plan\n## Goals\n- First\n  - Nested\na\tb\n1\t2\nLine one\nline two"
    )
    assert out["revisionId"] == "rev-1" and out["title"] == "Plan"
    assert out["truncated"] is False


def test_read_truncates(drive, httpx_mock):
    httpx_mock.add_response(**at("GET", f"{DOCS}/DOC"), json=DOC)
    out = gdocs.gdocs_read("DOC", max_chars=6)
    assert out["content"] == "# Plan" and out["truncated"] is True


def test_non_doc_path_is_refused_from_drive(drive, httpx_mock):
    with pytest.raises(Broker365Error, match="not a Google Doc.*convert=true"):
        gdocs.gdocs_read("/Budget")
    assert sent(httpx_mock, "GET", DOCS) == []


def test_wrong_type_id_skips_drive_and_explains(drive, httpx_mock):
    httpx_mock.add_response(
        **at("GET", f"{DOCS}/S1"),
        status_code=404,
        json={"error": {"code": 404, "message": "Requested entity was not found."}},
    )
    with pytest.raises(Broker365Error, match="Not found, or not a Google Doc.*convert=true"):
        gdocs.gdocs_read("S1")
    assert sent(httpx_mock, "GET", DRIVE) == []  # an id costs no Drive lookup


# ---- writes -----------------------------------------------------------------------------


def test_append_inserts_before_final_newline(drive, httpx_mock):
    httpx_mock.add_response(**at("GET", f"{DOCS}/DOC"), json=DOC)
    httpx_mock.add_response(
        **at("POST", f"{DOCS}/DOC:batchUpdate"),
        json={"writeControl": {"requiredRevisionId": "rev-2"}},
    )
    out = gdocs.gdocs_append("DOC", "More text\n")
    assert out == {"id": "DOC", "inserted_at": 59, "revisionId": "rev-2"}
    body = json.loads(sent(httpx_mock, "POST", DOCS)[0].content)
    new = {"startIndex": 60, "endIndex": 69}  # after the leading newline, 9 characters
    assert body == {
        "requests": [
            {"insertText": {"location": {"index": 59}, "text": "\nMore text"}},
            {
                "updateParagraphStyle": {
                    "range": new,
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "fields": "namedStyleType",
                }
            },
            {"deleteParagraphBullets": {"range": new}},
        ],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }


def test_append_to_empty_doc_has_no_leading_newline(drive, httpx_mock):
    empty = {"revisionId": "r", "body": {"content": [{"endIndex": 1}, {"endIndex": 2}]}}
    httpx_mock.add_response(**at("GET", f"{DOCS}/DOC"), json=empty)
    httpx_mock.add_response(**at("POST", f"{DOCS}/DOC:batchUpdate"), json={})
    gdocs.gdocs_append("DOC", "Hello")
    reqs = json.loads(sent(httpx_mock, "POST", DOCS)[0].content)["requests"]
    assert reqs[0]["insertText"] == {"location": {"index": 1}, "text": "Hello"}
    assert reqs[2] == {"deleteParagraphBullets": {"range": {"startIndex": 1, "endIndex": 6}}}


def test_replace_text_with_revision_guard(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{DOCS}/DOC:batchUpdate"),
        json={
            "replies": [{"replaceAllText": {"occurrencesChanged": 3}}],
            "writeControl": {"requiredRevisionId": "rev-9"},
        },
    )
    out = gdocs.gdocs_replace_text("DOC", "old", "new", match_case=False, revision_id="rev-1")
    assert out["occurrencesChanged"] == 3 and out["revisionId"] == "rev-9"
    assert json.loads(sent(httpx_mock, "POST", DOCS)[0].content) == {
        "requests": [
            {
                "replaceAllText": {
                    "containsText": {"text": "old", "matchCase": False},
                    "replaceText": "new",
                }
            }
        ],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }


def test_replace_text_zero_matches(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{DOCS}/DOC:batchUpdate"), json={"replies": [{"replaceAllText": {}}]}
    )
    assert gdocs.gdocs_replace_text("DOC", "x", "y")["occurrencesChanged"] == 0
    assert "writeControl" not in json.loads(sent(httpx_mock, "POST", DOCS)[0].content)


def test_stale_revision_is_a_conflict(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{DOCS}/DOC:batchUpdate"),
        status_code=400,
        json={
            "error": {
                "code": 400,
                "message": "The required revision ID 'rev-1' does not match the latest revision.",
                "status": "INVALID_ARGUMENT",  # the live shape
            }
        },
    )
    with pytest.raises(ConflictError, match="changed since revision rev-1.*nothing was changed"):
        gdocs.gdocs_replace_text("DOC", "a", "b", revision_id="rev-1")


def test_unguarded_400_is_not_a_conflict(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{DOCS}/DOC:batchUpdate"),
        status_code=400,
        json={"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}},
    )
    with pytest.raises(Broker365Error) as exc:
        gdocs.gdocs_replace_text("DOC", "a", "b")
    assert not isinstance(exc.value, ConflictError)


def test_fill_template_copies_then_counts(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{DRIVE}/files/DOC/copy"), json=item("C1", "Letter Pat", gdrive.DOC)
    )
    httpx_mock.add_response(
        **at("POST", f"{DOCS}/C1:batchUpdate"),
        json={
            "replies": [
                {"replaceAllText": {"occurrencesChanged": 2}},
                {"replaceAllText": {}},
                {"replaceAllText": {"occurrencesChanged": 1}},
            ]
        },
    )
    out = gdocs.gdocs_fill_template(
        "/Docs/Plan",
        {"name": "Pat", "missing": "x", "amount": 12},
        copy_to="Letter Pat",
        remote_folder="/Docs",
    )
    assert out["id"] == "C1" and out["copy"]["name"] == "Letter Pat"
    assert out["replacements"] == {"name": 2, "missing": 0, "amount": 1}
    assert out["unmatched_keys"] == ["missing"]
    copy = json.loads(sent(httpx_mock, "POST", f"{DRIVE}/files/DOC/copy")[0].content)
    assert copy == {"name": "Letter Pat", "parents": ["D1"]}
    reqs = json.loads(sent(httpx_mock, "POST", DOCS)[0].content)["requests"]
    assert reqs[0] == {
        "replaceAllText": {
            "containsText": {"text": "{{name}}", "matchCase": True},
            "replaceText": "Pat",
        }
    }
    assert reqs[2]["replaceAllText"]["replaceText"] == "12"


@pytest.mark.parametrize("values", [{}, {"a}}": "x"}, {"": "x"}, ["a"]])
def test_fill_template_validates_keys(values, drive):
    with pytest.raises(Broker365Error):
        gdocs.gdocs_fill_template("DOC", values)


# ---- registry, audit --------------------------------------------------------------------


def test_read_only_hides_doc_writes(monkeypatch):
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    visible = {t.name for t in services.available_tools()}
    assert "gdocs_read" in visible
    assert not {"gdocs_create", "gdocs_append", "gdocs_replace_text"} & visible
    with pytest.raises(ReadOnlyMode):
        services.call("gdocs_append", item="DOC", markdown_or_text="x")


def test_audit_excludes_content(drive, httpx_mock):
    httpx_mock.add_response(**at("POST", f"{DOCS}/DOC:batchUpdate"), json={})
    services.call("gdocs_replace_text", item="DOC", find="secret-old", replace="secret-new")
    httpx_mock.add_response(**at("POST", UPLOAD), json=item("N", "Memo", gdrive.DOC))
    services.call("gdocs_create", title="Memo", markdown="classified body")
    log = (config.broker_home() / "audit.log").read_text(encoding="utf-8")
    first, second = (json.loads(line) for line in log.splitlines()[-2:])
    assert first["target"] == {"item": "DOC"}
    assert second["target"] == {"title": "Memo"}
    assert "secret" not in log and "classified" not in log


def test_read_numbers_ordered_lists_per_list_and_level(drive, httpx_mock):
    def li(text, list_id, level=0):
        el = para(text + "\n", bullet=level)
        el["paragraph"]["bullet"]["listId"] = list_id
        return el

    doc = {
        "documentId": "DOC",
        "lists": {
            "num": {
                "listProperties": {
                    "nestingLevels": [{"glyphType": "DECIMAL"}, {"glyphType": "ALPHA"}]
                }
            },
            "dots": {"listProperties": {"nestingLevels": [{"glyphSymbol": "\u25cf"}]}},
        },
        "body": {
            "content": [
                li("One", "num"),
                li("Sub a", "num", 1),
                li("Sub b", "num", 1),
                li("Two", "num"),
                li("Sub again", "num", 1),
                li("Dot", "dots"),
                li("Three", "num"),
            ]
        },
    }
    httpx_mock.add_response(**at("GET", f"{DOCS}/DOC"), json=doc)
    out = gdocs.gdocs_read("DOC")
    assert out["content"].splitlines() == [
        "1. One",
        "  1. Sub a",
        "  2. Sub b",
        "2. Two",
        "  1. Sub again",
        "- Dot",
        "3. Three",
    ]
    fields = sent(httpx_mock, "GET", DOCS)[0].url.params["fields"]
    assert (
        fields == gdocs.READ_FIELDS
        and "lists" in fields
        and "bullet(listId,nestingLevel)" in fields
    )


def test_fill_template_copy_needs_both_consents_first(drive, httpx_mock, monkeypatch):
    from broker365.errors import ConsentRequired
    from broker365.providers import google

    monkeypatch.setattr(google.GoogleProvider, "has_consent", lambda self, b, u=None: b != "gdocs")
    with pytest.raises(ConsentRequired, match="--scopes gdocs"):
        services.call("gdocs_fill_template", item="DOC", values={"a": "b"}, copy_to="Copy")
    assert httpx_mock.get_requests() == []  # no orphan copy


def test_fill_template_verifies_template_type_before_copying(drive, httpx_mock):
    with pytest.raises(Broker365Error, match="not a Google Doc"):
        gdocs.gdocs_fill_template("S1", {"a": "b"}, copy_to="Copy")
    assert sent(httpx_mock, "POST") == []


STALE = {
    "error": {
        "code": 400,
        "message": "The required revision ID 'rev-1' does not match the latest revision.",
        "status": "INVALID_ARGUMENT",
    }
}


def test_append_counts_utf16_units(drive, httpx_mock):
    empty = {"revisionId": "r", "body": {"content": [{"endIndex": 1}, {"endIndex": 2}]}}
    httpx_mock.add_response(**at("GET", f"{DOCS}/DOC"), json=empty)
    httpx_mock.add_response(**at("POST", f"{DOCS}/DOC:batchUpdate"), json={})
    gdocs.gdocs_append("DOC", "a\U0001f600")  # an emoji is 2 UTF-16 units
    reqs = json.loads(sent(httpx_mock, "POST", DOCS)[0].content)["requests"]
    assert reqs[1]["updateParagraphStyle"]["range"] == {"startIndex": 1, "endIndex": 4}


def test_append_retries_its_own_conflicts(drive, httpx_mock):
    first = {"revisionId": "rev-1", "body": {"content": [{"endIndex": 10}]}}
    second = {"revisionId": "rev-2", "body": {"content": [{"endIndex": 20}]}}
    httpx_mock.add_response(**at("GET", f"{DOCS}/DOC"), json=first)
    httpx_mock.add_response(**at("POST", f"{DOCS}/DOC:batchUpdate"), status_code=400, json=STALE)
    httpx_mock.add_response(**at("GET", f"{DOCS}/DOC"), json=second)
    httpx_mock.add_response(**at("POST", f"{DOCS}/DOC:batchUpdate"), json={})
    out = gdocs.gdocs_append("DOC", "x")
    assert out["inserted_at"] == 19
    posts = [json.loads(r.content) for r in sent(httpx_mock, "POST", DOCS)]
    assert [p["writeControl"]["requiredRevisionId"] for p in posts] == ["rev-1", "rev-2"]


def test_append_gives_up_without_mentioning_internal_revisions(drive, httpx_mock):
    for _ in range(gdocs.APPEND_ATTEMPTS):
        httpx_mock.add_response(
            **at("GET", f"{DOCS}/DOC"), json={"revisionId": "rev-1", "body": {"content": []}}
        )
        httpx_mock.add_response(
            **at("POST", f"{DOCS}/DOC:batchUpdate"), status_code=400, json=STALE
        )
    with pytest.raises(ConflictError) as exc:
        gdocs.gdocs_append("DOC", "x")
    assert "edited by someone else" in str(exc.value) and "rev-1" not in str(exc.value)


def test_fill_template_copy_name_conflict_fails_before_copying(drive, httpx_mock):
    drive.add(item("TAKEN", "Letter", gdrive.DOC, parent="D1"))
    with pytest.raises(Broker365Error, match="already exists.*conflict='rename'"):
        gdocs.gdocs_fill_template("DOC", {"a": "b"}, copy_to="Letter")
    assert sent(httpx_mock, "POST") == []


def test_fill_template_copy_rename(drive, httpx_mock):
    drive.add(item("TAKEN", "Letter", gdrive.DOC, parent="D1"))
    httpx_mock.add_response(**at("POST", f"{DRIVE}/files/DOC/copy"), json=item("C2", "Letter (1)"))
    httpx_mock.add_response(**at("POST", f"{DOCS}/C2:batchUpdate"), json={"replies": [{}]})
    gdocs.gdocs_fill_template("DOC", {"a": "b"}, copy_to="Letter", conflict="rename")
    copy = json.loads(sent(httpx_mock, "POST", f"{DRIVE}/files/DOC/copy")[0].content)
    assert copy == {"name": "Letter (1)", "parents": ["D1"]}  # next to the template


def test_fill_template_trashes_copy_when_filling_fails(drive, httpx_mock):
    httpx_mock.add_response(**at("POST", f"{DRIVE}/files/DOC/copy"), json=item("C3", "Out"))
    httpx_mock.add_response(
        **at("POST", f"{DOCS}/C3:batchUpdate"),
        status_code=400,
        json={"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}},
    )
    httpx_mock.add_response(
        **at("PATCH", f"{DRIVE}/files/C3"), json=item("C3", "Out", trashed=True)
    )
    with pytest.raises(Broker365Error, match="bad"):
        gdocs.gdocs_fill_template("DOC", {"a": "b"}, copy_to="Out")
    trash = sent(httpx_mock, "PATCH", f"{DRIVE}/files/C3")
    assert [json.loads(r.content) for r in trash] == [{"trashed": True}]
    assert sent(httpx_mock, "DELETE") == []


def test_fill_template_by_id_needs_no_drive(drive, httpx_mock, monkeypatch):
    from broker365.providers import google

    monkeypatch.setattr(google.GoogleProvider, "has_consent", lambda self, b, u=None: b != "gdrive")
    httpx_mock.add_response(**at("POST", f"{DOCS}/DOC:batchUpdate"), json={"replies": [{}]})
    out = services.call("gdocs_fill_template", item="DOC", values={"a": "b"})
    assert out["unmatched_keys"] == ["a"]
    with pytest.raises(ConsentRequired, match="gdrive"):
        services.call("gdocs_fill_template", item="DOC", values={"a": "b"}, copy_to="C")
    assert services.load_all()["gdocs_fill_template"].requires == ()
