import json
import re

import pytest

from broker365 import config, gapi, services
from broker365.errors import Broker365Error, ConsentRequired, ReadOnlyMode
from broker365.providers import google
from broker365.services import gdrive, gsheets
from tests.test_gdrive import FakeDrive, at, item, sent

SHEETS = gsheets.SHEETS
DRIVE = gapi.DRIVE


@pytest.fixture
def drive(httpx_mock):
    d = FakeDrive(httpx_mock)
    d.add(
        item("D1", "Docs", gdrive.FOLDER),
        item("S1", "Budget", gdrive.SHEET),
        item("DOC", "Plan", gdrive.DOC),
    )
    # tab titles, read when a bare range looks like cells ('A1', 'Q1')
    httpx_mock.add_response(
        method="GET",
        url=re.compile(re.escape(SHEETS) + r"/[^/?]+\?fields=sheets.properties.title$"),
        json={"sheets": [{"properties": {"title": "Sheet1"}}, {"properties": {"title": "Q1"}}]},
        is_reusable=True,
        is_optional=True,
    )
    return d


@pytest.fixture
def no_drive_consent(monkeypatch):
    """Only gsheets granted: the Drive (gdrive) client can't get a token."""

    def token(self):
        if self.bundle == "gdrive":
            raise ConsentRequired("gdrive", "google")
        return "g-token"

    monkeypatch.setattr(gapi.GoogleClient, "_token", token)
    monkeypatch.setattr(google.GoogleProvider, "has_consent", lambda self, b, u=None: b != "gdrive")


TABS = {
    "properties": {"title": "Budget"},
    "sheets": [
        {
            "properties": {
                "sheetId": 0,
                "title": "Sheet1",
                "index": 0,
                "gridProperties": {"rowCount": 1000, "columnCount": 26},
            }
        }
    ],
}


# ---- A1 ranges --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given, expected",
    [
        ("A1:C3", "A1:C3"),
        ("A:C", "A:C"),
        ("$A$1", "$A$1"),
        ("Sheet1!A1:B2", "Sheet1!A1:B2"),
        ("Budget 2026!A1:B2", "'Budget 2026'!A1:B2"),
        ("O'Brien!A1", "'O''Brien'!A1"),
        ("'Already quoted'!A1", "'Already quoted'!A1"),
        ("'Q1!Sales'", "'Q1!Sales'"),  # a quoted name may contain '!'
        ("'Q1!Sales'!A1", "'Q1!Sales'!A1"),
        ("'It''s'!B2", "'It''s'!B2"),
        ("Log", "Log"),  # a bare column isn't a cell area: a tab name
        ("A1:B", "A1:B"),
        ("AB1!A1", "'AB1'!A1"),
        ("My Tab", "'My Tab'"),
        ("Sheet1", "Sheet1"),
        ("Données!B2", "'Données'!B2"),
    ],
)
def test_a1_quotes_tab_names(given, expected):
    assert gsheets.a1(given) == expected


@pytest.mark.parametrize("bad", ["", "!A1", "Tab!", "'open", "'Tab'A1", "'Tab'!", "''!A1"])
def test_a1_rejects_incomplete_ranges(bad):
    with pytest.raises(Broker365Error):
        gsheets.a1(bad)


@pytest.mark.parametrize("values", [[], [1, 2], "x", [[{"a": 1}]], [[1], "row"]])
def test_values_must_be_2d_scalars(values):
    with pytest.raises(Broker365Error):
        gsheets.gsheets_write_range("S1", "A1", values)


# ---- reads ------------------------------------------------------------------------------


def test_read_range_quotes_and_renders(drive, httpx_mock):
    httpx_mock.add_response(
        **at("GET", f"{SHEETS}/S1/values/"),
        json={"range": "'My Tab'!A1:B2", "values": [["a", "1"], ["b"]]},
    )
    out = gsheets.gsheets_read_range("/Budget", "My Tab!A1:B2", render="FORMULA")
    assert out["values"] == [["a", "1"], ["b"]] and out["rows"] == 2 and out["cols"] == 2
    req = sent(httpx_mock, "GET", SHEETS)[0]
    assert req.url.raw_path.startswith(b"/v4/spreadsheets/S1/values/%27My%20Tab%27%21A1%3AB2")
    assert req.url.params["valueRenderOption"] == "FORMULA"
    assert req.headers["Authorization"] == "Bearer g-token"


def test_read_range_rejects_unknown_render(drive):
    with pytest.raises(Broker365Error, match="render must be"):
        gsheets.gsheets_read_range("S1", "A1", render="HTML")


def test_list_tabs(drive, httpx_mock):
    httpx_mock.add_response(**at("GET", f"{SHEETS}/S1?"), json=TABS)
    out = gsheets.gsheets_list_tabs("S1")
    assert out["tabs"] == [
        {
            "sheetId": 0,
            "title": "Sheet1",
            "index": 0,
            "rowCount": 1000,
            "colCount": 26,
        }
    ]
    assert sent(httpx_mock, "GET", SHEETS)[0].url.params["fields"] == gsheets.TAB_FIELDS


def test_non_sheet_is_refused_with_hint(drive, httpx_mock):
    with pytest.raises(Broker365Error, match="not a Google Sheet.*convert=true.*excel_"):
        gsheets.gsheets_read_range("/Plan", "A1")
    assert sent(httpx_mock, "GET", SHEETS) == []


def test_id_works_without_drive_consent(drive, httpx_mock, no_drive_consent):
    httpx_mock.add_response(**at("GET", f"{SHEETS}/S1/values/"), json={"values": [["x"]]})
    assert gsheets.gsheets_read_range("S1", "A1")["values"] == [["x"]]
    with pytest.raises(ConsentRequired, match="gdrive"):
        gsheets.gsheets_read_range("/Budget", "A1")  # a path needs Drive


# ---- writes -----------------------------------------------------------------------------


def test_write_range_user_entered_by_default(drive, httpx_mock):
    httpx_mock.add_response(
        **at("PUT", f"{SHEETS}/S1/values/"),
        json={"updatedRange": "Sheet1!A1:B1", "updatedCells": 2},
    )
    out = gsheets.gsheets_write_range("S1", "Sheet1!A1", [["=SUM(1,2)", None]])
    assert out["updatedCells"] == 2
    req = sent(httpx_mock, "PUT", SHEETS)[0]
    assert req.url.params["valueInputOption"] == "USER_ENTERED"
    assert json.loads(req.content) == {
        "range": "Sheet1!A1",
        "majorDimension": "ROWS",
        "values": [["=SUM(1,2)", ""]],
    }


def test_write_range_raw(drive, httpx_mock):
    httpx_mock.add_response(**at("PUT", f"{SHEETS}/S1/values/"), json={})
    gsheets.gsheets_write_range("S1", "A1", [["=not a formula"]], raw=True)
    assert sent(httpx_mock, "PUT", SHEETS)[0].url.params["valueInputOption"] == "RAW"


def test_append_rows_inserts(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{SHEETS}/S1/values/"),
        json={
            "tableRange": "Log!A1:B3",
            "updates": {"updatedRange": "Log!A4:B4", "updatedRows": 1},
        },
    )
    out = gsheets.gsheets_append_rows("S1", "Log", [["2026-10-15", 3]])
    assert out["updatedRange"] == "Log!A4:B4"
    req = sent(httpx_mock, "POST", SHEETS)[0]
    assert req.url.raw_path.split(b"?")[0].endswith(b"/values/Log:append")
    assert req.url.params["insertDataOption"] == "INSERT_ROWS"
    assert req.url.params["valueInputOption"] == "USER_ENTERED"
    assert json.loads(req.content) == {"majorDimension": "ROWS", "values": [["2026-10-15", 3]]}


def test_clear_range(drive, httpx_mock):
    httpx_mock.add_response(**at("POST", f"{SHEETS}/S1/values/"), json={"clearedRange": "A1:B2"})
    assert gsheets.gsheets_clear_range("S1", "A1:B2")["clearedRange"] == "A1:B2"
    assert (
        sent(httpx_mock, "POST", SHEETS)[0].url.raw_path.split(b"?")[0].endswith(b"A1%3AB2:clear")
    )


def test_add_tab(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{SHEETS}/S1:batchUpdate"),
        json={"replies": [{"addSheet": {"properties": {"sheetId": 7, "title": "Q4"}}}]},
    )
    out = gsheets.gsheets_add_tab("S1", "Q4", rows=50, cols=5)
    assert out["tab"]["sheetId"] == 7
    assert json.loads(sent(httpx_mock, "POST", SHEETS)[0].content) == {
        "requests": [
            {
                "addSheet": {
                    "properties": {
                        "title": "Q4",
                        "gridProperties": {"rowCount": 50, "columnCount": 5},
                    }
                }
            }
        ]
    }


# ---- create -----------------------------------------------------------------------------


def test_create_in_folder_via_drive_with_tabs_and_values(drive, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=re.compile(re.escape(f"{DRIVE}/files") + r"\?.*"),
        json=item("NEW", "Plan 2026", gdrive.SHEET, parent="D1"),
    )
    renamed = {
        "sheets": [
            {"properties": {"sheetId": 0, "title": "Data", "index": 0}},
            {"properties": {"sheetId": 5, "title": "Notes", "index": 1}},
        ]
    }
    httpx_mock.add_response(
        **at("POST", f"{SHEETS}/NEW:batchUpdate"),
        json={"replies": [{}, {}], "updatedSpreadsheet": renamed},
    )
    httpx_mock.add_response(**at("PUT", f"{SHEETS}/NEW/values/"), json={"updatedCells": 2})
    out = gsheets.gsheets_create(
        "Plan 2026", "/Docs", tabs=["Data", "Notes"], values=[["a", "=1+1"]]
    )
    assert out["id"] == "NEW" and [t["title"] for t in out["tabs"]] == ["Data", "Notes"]
    create = sent(httpx_mock, "POST", f"{DRIVE}/files")[0]
    assert json.loads(create.content) == {
        "name": "Plan 2026",
        "mimeType": gdrive.SHEET,
        "parents": ["D1"],
    }
    # fewer calls: Drive create, one batchUpdate returning the tabs, the values; no GET
    assert sent(httpx_mock, "GET", SHEETS) == []
    body = json.loads(sent(httpx_mock, "POST", SHEETS)[0].content)
    assert body["includeSpreadsheetInResponse"] is True
    assert body["responseIncludeGridData"] is False
    batch = body["requests"]
    assert batch == [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": 0, "title": "Data"},
                "fields": "title",
            }
        },
        {"addSheet": {"properties": {"title": "Notes"}}},
    ]
    put = sent(httpx_mock, "PUT", SHEETS)[0]
    assert json.loads(put.content)["range"] == "Data!A1"
    assert put.url.params["valueInputOption"] == "USER_ENTERED"


def test_create_refuses_taken_title(drive):
    with pytest.raises(Broker365Error, match="already exists.*conflict='rename'"):
        gsheets.gsheets_create("Budget", "/")


def test_create_in_root_falls_back_to_sheets_api(httpx_mock, no_drive_consent):
    httpx_mock.add_response(
        method="POST",
        url=SHEETS,
        json={
            "spreadsheetId": "X",
            "properties": {"title": "T"},
            "spreadsheetUrl": "https://u",
            "sheets": [{"properties": {"sheetId": 0, "title": "A"}}],
        },
    )
    out = gsheets.gsheets_create("T", tabs=["A"])
    assert out["id"] == "X" and out["webViewLink"] == "https://u"
    assert json.loads(sent(httpx_mock, "POST", SHEETS)[0].content) == {
        "properties": {"title": "T"},
        "sheets": [{"properties": {"title": "A"}}],
    }


def test_create_in_folder_needs_drive(httpx_mock, no_drive_consent):
    with pytest.raises(ConsentRequired, match="gdrive"):
        gsheets.gsheets_create("T", "/Docs")
    assert httpx_mock.get_requests() == []


# ---- registry, audit --------------------------------------------------------------------


def test_flags_and_read_only(monkeypatch):
    tools = services.load_all()
    assert tools["gsheets_clear_range"].destructive
    assert {n for n, t in tools.items() if n.startswith("gsheets_") and not t.writes} == {
        "gsheets_list_tabs",
        "gsheets_read_range",
    }
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    visible = {t.name for t in services.available_tools()}
    assert "gsheets_read_range" in visible and "gsheets_write_range" not in visible
    with pytest.raises(ReadOnlyMode):
        services.call("gsheets_clear_range", item="S1", range="A1")


def test_audit_logs_range_not_values(drive, httpx_mock):
    httpx_mock.add_response(**at("PUT", f"{SHEETS}/S1/values/"), json={})
    services.call("gsheets_write_range", item="S1", range="Pay!A1", values=[["salary 9999"]])
    log = (config.broker_home() / "audit.log").read_text(encoding="utf-8")
    entry = json.loads(log.splitlines()[-1])
    assert entry["target"] == {"item": "S1", "range": "Pay!A1"}
    assert "9999" not in log


def test_wrong_type_id_is_explained_by_the_sheets_api(drive, httpx_mock):
    httpx_mock.add_response(
        **at("GET", f"{SHEETS}/DOC/values/"),
        status_code=400,
        json={
            "error": {
                "code": 400,
                "message": "This operation is not supported for this document",
                "status": "FAILED_PRECONDITION",
            }
        },
    )
    with pytest.raises(Broker365Error, match="not a Google Sheet.*excel_"):
        gsheets.gsheets_read_range("DOC", "A1")
    assert sent(httpx_mock, "GET", DRIVE) == []


def test_bad_range_400_is_not_rewritten(drive, httpx_mock):
    httpx_mock.add_response(
        **at("GET", f"{SHEETS}/S1/values/"),
        status_code=400,
        json={"error": {"code": 400, "message": "Unable to parse range: Nope!A1"}},
    )
    with pytest.raises(Broker365Error, match="Unable to parse range"):
        gsheets.gsheets_read_range("S1", "Nope!A1")


def test_create_without_tabs_reads_tabs_once(drive, httpx_mock):
    httpx_mock.add_response(**at("POST", f"{DRIVE}/files"), json=item("NEW", "Plain", gdrive.SHEET))
    httpx_mock.add_response(**at("GET", f"{SHEETS}/NEW?"), json=TABS)
    httpx_mock.add_response(**at("PUT", f"{SHEETS}/NEW/values/"), json={})
    out = gsheets.gsheets_create("Plain", values=[["x"]])
    assert [t["title"] for t in out["tabs"]] == ["Sheet1"]
    assert sent(httpx_mock, "POST", SHEETS) == []  # no batchUpdate
    assert len(sent(httpx_mock, "GET", SHEETS)) == 1
    assert json.loads(sent(httpx_mock, "PUT", SHEETS)[0].content)["range"] == "Sheet1!A1"
    create = json.loads(sent(httpx_mock, "POST", f"{DRIVE}/files")[0].content)
    assert create["parents"] == ["root"]


def titles_requests(httpx_mock):
    return [r for r in sent(httpx_mock, "GET", SHEETS) if "properties.title" in str(r.url)]


def test_cell_like_tab_name_wins_when_it_exists(drive, httpx_mock):
    httpx_mock.add_response(**at("GET", f"{SHEETS}/S1/values/"), json={"values": []})
    gsheets.gsheets_read_range("S1", "Q1")
    values = [r for r in sent(httpx_mock, "GET", SHEETS) if "/values/" in str(r.url)][0]
    assert values.url.raw_path.split(b"?")[0].endswith(b"/values/%27Q1%27")
    assert len(titles_requests(httpx_mock)) == 1


def test_cell_like_value_without_such_tab_is_cells(drive, httpx_mock):
    httpx_mock.add_response(**at("GET", f"{SHEETS}/S1/values/"), json={"values": []})
    gsheets.gsheets_read_range("S1", "FY24")
    values = [r for r in sent(httpx_mock, "GET", SHEETS) if "/values/" in str(r.url)][0]
    assert values.url.raw_path.split(b"?")[0].endswith(b"/values/FY24")


def test_explicit_tab_needs_no_lookup(drive, httpx_mock):
    httpx_mock.add_response(**at("GET", f"{SHEETS}/S1/values/"), json={"values": []})
    gsheets.gsheets_read_range("S1", "'Q1!Sales'")
    gsheets_url = [r for r in sent(httpx_mock, "GET", SHEETS) if "/values/" in str(r.url)][0]
    assert gsheets_url.url.raw_path.split(b"?")[0].endswith(b"/values/%27Q1%21Sales%27")
    assert titles_requests(httpx_mock) == []


def test_root_fallback_notes_unchecked_duplicates(httpx_mock, no_drive_consent):
    httpx_mock.add_response(method="POST", url=SHEETS, json={"spreadsheetId": "X", "sheets": []})
    out = gsheets.gsheets_create("Budget")
    assert "wasn't checked for duplicates" in out["note"]
