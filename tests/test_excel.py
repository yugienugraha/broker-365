import io
import json

import openpyxl
import pytest

from broker365 import auth, config
from broker365.errors import Broker365Error
from broker365.services import excel
from tests.conftest import G

ITEM = {
    "id": "X!1",
    "name": "b.xlsx",
    "eTag": '"etag-1"',
    "file": {},
    "parentReference": {"path": "/drive/root:"},
}
WB = f"{G}/me/drive/items/X!1/workbook"
RANGE = f"{WB}/worksheets('Sheet1')/range(address='A1:B2')"
SELECT = "?%24select=address%2Cvalues%2Cformulas%2Ctext"
USED = f"{WB}/worksheets('Sheet1')/usedRange(valuesOnly=true)"


def _account(monkeypatch, username: str, personal: bool) -> None:
    account = {"username": username}
    monkeypatch.setattr(auth, "current_account", lambda username=None: account)
    monkeypatch.setattr(auth, "is_personal_account", lambda a: personal)


# excel resolves the account once and checks it with is_personal_account, so these override
# the conftest fixtures (which patch auth.is_personal).
@pytest.fixture
def personal(monkeypatch):
    _account(monkeypatch, "u@example.com", True)


@pytest.fixture
def business(monkeypatch):
    _account(monkeypatch, "w@contoso.com", False)


def xlsx(rows=(("a", 1), ("b", 2))) -> bytes:
    wb = openpyxl.Workbook()
    wb.active.title = "Sheet1"
    for r in rows:
        wb.active.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def cached() -> dict:
    return config.load_settings().get("excel_backend", {})


# ---- address parser ---------------------------------------------------------------------


def test_parse_address():
    assert excel.parse_address("B2") == excel.Area(2, 2, 2, 2)
    assert excel.parse_address("$c$3:a1") == excel.Area(1, 1, 3, 3)
    assert excel.parse_address("AA10:XFD12").cols == 16384 - 27 + 1
    assert excel.parse_address("AB1").col1 == 28 and excel.Area(1, 28, 1, 28).address == "AB1"
    assert excel.parse_address("A1:C3").address == "A1:C3"


@pytest.mark.parametrize("bad", ["A:B", "1:3", "C", "A1:B", ""])
def test_parse_address_rejects_unbounded_or_invalid(bad):
    with pytest.raises(Broker365Error):
        excel.parse_address(bad)


def test_parse_address_rejects_sheet_prefix_and_out_of_grid():
    with pytest.raises(Broker365Error, match="sheet"):
        excel.parse_address("Sheet1!A1")
    with pytest.raises(Broker365Error, match="grid"):
        excel.parse_address("XFE1")


def test_write_dimension_mismatch_makes_no_request(business):
    with pytest.raises(Broker365Error, match="2x2"):
        excel.excel_write_range("X!1", "Sheet1", "A1:B2", [[1, 2, 3], [4, 5, 6]])
    with pytest.raises(Broker365Error, match="unbounded|Unbounded"):
        excel.excel_write_range("X!1", "Sheet1", "A:B", [[1, 2]])


def test_sheet_names_are_quoted():
    assert excel._sheet_seg("Bob's Q1") == "worksheets('Bob%27%27s%20Q1')"
    with pytest.raises(Broker365Error, match="sheet name"):
        excel._sheet_seg("")


# ---- business: Workbook API -------------------------------------------------------------


def test_business_read_is_sessionless(httpx_mock, business):
    httpx_mock.add_response(
        url=RANGE + SELECT,
        json={
            "address": "Sheet1!A1:B2",
            "values": [[1, 2], [3, 3]],
            "formulas": [[1, 2], [3, "=A2"]],
            "text": [["1", "2"], ["3", "3"]],
        },
    )
    out = excel.excel_read_range("X!1", "Sheet1", "A1:B2")
    assert out["address"] == "A1:B2" and out["formulas"][1][1] == "=A2"
    assert out["backend"] == "workbook"
    assert "workbook-session-id" not in httpx_mock.get_requests()[0].headers


def test_business_small_append_is_sessionless(httpx_mock, business):
    httpx_mock.add_response(url=f"{USED}?%24select=address", json={"address": "Sheet1!A1:B3"})
    httpx_mock.add_response(
        method="PATCH", url=f"{WB}/worksheets('Sheet1')/range(address='A4:B5')", json={}
    )
    out = excel.excel_append_rows("X!1", "Sheet1", [["x", "=A1*2"], ["y"]])
    assert out["address"] == "A4:B5" and out["backend"] == "workbook"
    reqs = httpx_mock.get_requests()
    assert all("workbook-session-id" not in r.headers for r in reqs)
    assert json.loads(reqs[1].content) == {"values": [["x", "=A1*2"], ["y", None]]}


def test_business_large_append_uses_session(httpx_mock, business, monkeypatch):
    monkeypatch.setattr(excel, "CHUNK_CELLS", 2)
    httpx_mock.add_response(method="POST", url=f"{WB}/createSession", json={"id": "S1"})
    httpx_mock.add_response(url=f"{USED}?%24select=address", json={"address": "Sheet1!B2"})
    httpx_mock.add_response(url=f"{USED}?%24select=values", json={"values": [[7]]})
    for band in ("B3:C3", "B4:C4"):
        httpx_mock.add_response(
            method="PATCH", url=f"{WB}/worksheets('Sheet1')/range(address='{band}')", json={}
        )
    httpx_mock.add_response(method="POST", url=f"{WB}/closeSession", status_code=204)
    out = excel.excel_append_rows("X!1", "Sheet1", [["x", 1], ["y", 2]])
    assert out["address"] == "B3:C4"
    reqs = httpx_mock.get_requests()
    assert json.loads(reqs[0].content) == {"persistChanges": True}
    assert all(r.headers["workbook-session-id"] == "S1" for r in reqs[1:])


def test_business_large_write_is_chunked_in_session(httpx_mock, business, monkeypatch):
    monkeypatch.setattr(excel, "CHUNK_CELLS", 4)
    httpx_mock.add_response(method="POST", url=f"{WB}/createSession", json={"id": "S2"})
    for band in ("A1:B2", "A3:B3"):
        httpx_mock.add_response(
            method="PATCH", url=f"{WB}/worksheets('Sheet1')/range(address='{band}')", json={}
        )
    httpx_mock.add_response(method="POST", url=f"{WB}/closeSession", status_code=500)
    out = excel.excel_write_range("X!1", "Sheet1", "A1:B3", [[1, 2], [3, 4], [5, 6]])
    assert out == {"address": "A1:B3", "rows": 3, "columns": 2, "backend": "workbook"}
    patches = [r for r in httpx_mock.get_requests() if r.method == "PATCH"]
    assert [json.loads(r.content)["values"] for r in patches] == [[[1, 2], [3, 4]], [[5, 6]]]
    assert all(r.headers["workbook-session-id"] == "S2" for r in patches)


def test_business_calc_and_table_rows(httpx_mock, business):
    httpx_mock.add_response(method="POST", url=f"{WB}/functions/pmt", json={"value": -1037.03})
    httpx_mock.add_response(method="POST", url=f"{WB}/tables/Table1/rows", json={"index": 5})
    assert (
        excel.excel_calc("X!1", "PMT", {"rate": 0.0045, "nper": 12, "pv": 12000})["value"]
        == -1037.03
    )
    assert excel.excel_table_add_rows("X!1", "Table1", [[1, 2]])["index"] == 5


# ---- personal: probe --------------------------------------------------------------------


def test_personal_probe_success_is_sessionless_and_cached(httpx_mock, personal):
    httpx_mock.add_response(url=f"{WB}/worksheets", json={"value": []})
    httpx_mock.add_response(url=f"{USED}?%24select=address", json={"address": "Sheet1!A1"})
    httpx_mock.add_response(url=f"{USED}?%24select=values", json={"values": [[""]]})
    httpx_mock.add_response(
        method="PATCH", url=f"{WB}/worksheets('Sheet1')/range(address='A1:B1')", json={}
    )
    out = excel.excel_append_rows("X!1", "Sheet1", [[1, 2]])
    assert out["address"] == "A1:B1" and out["backend"] == "workbook"
    assert cached() == {"u@example.com": "workbook"}
    assert not any("Session" in str(r.url) for r in httpx_mock.get_requests())
    assert all("workbook-session-id" not in r.headers for r in httpx_mock.get_requests())


def test_personal_probe_failure_uses_openpyxl(httpx_mock, personal):
    httpx_mock.add_response(
        url=f"{WB}/worksheets",
        status_code=400,
        json={"error": {"code": "notSupported", "message": "no"}},
    )
    httpx_mock.add_response(url=f"{G}/me/drive/items/X!1", json=ITEM)
    httpx_mock.add_response(url=f"{G}/me/drive/items/X!1/content", content=xlsx())
    httpx_mock.add_response(method="PUT", url=f"{G}/me/drive/items/X!1/content", json=ITEM)
    out = excel.excel_write_range("X!1", "sheet1", "B1:C2", [[10, "=B1*2"], [None, ""]])
    assert out["backend"] == "openpyxl" and "note" in out
    assert cached() == {"u@example.com": "openpyxl"}
    put = httpx_mock.get_requests()[-1]
    assert put.headers["If-Match"] == '"etag-1"'
    ws = openpyxl.load_workbook(io.BytesIO(put.content))["Sheet1"]
    assert [[c.value for c in row] for row in ws["A1:C2"]] == [["a", 10, "=B1*2"], ["b", 2, None]]


def test_personal_unclear_probe_failure_is_not_cached(httpx_mock, personal):
    httpx_mock.add_response(
        url=f"{WB}/worksheets",
        status_code=404,
        json={"error": {"code": "itemNotFound", "message": "?"}},
    )
    httpx_mock.add_response(url=f"{G}/me/drive/items/X!1", json=ITEM)
    httpx_mock.add_response(url=f"{G}/me/drive/items/X!1/content", content=xlsx())
    out = excel.excel_read_range("X!1", "Sheet1", "A1:B2")
    assert out["values"] == [["a", 1], ["b", 2]] and out["backend"] == "openpyxl"
    assert cached() == {}


def test_personal_cached_workbook_switches_on_not_supported(httpx_mock, personal):
    config.update_settings(excel_backend={"u@example.com": "workbook", "other@x.com": "openpyxl"})
    httpx_mock.add_response(
        url=f"{WB}/worksheets",
        status_code=501,
        json={"error": {"code": "notImplemented", "message": "no"}},
    )
    httpx_mock.add_response(url=f"{G}/me/drive/items/X!1", json=ITEM)
    httpx_mock.add_response(url=f"{G}/me/drive/items/X!1/content", content=xlsx())
    out = excel.excel_list_sheets("X!1")
    assert out["sheets"] == [{"name": "Sheet1", "position": 0, "visibility": "visible"}]
    assert cached() == {"u@example.com": "openpyxl", "other@x.com": "openpyxl"}


def test_openpyxl_append_and_table_unsupported(httpx_mock, personal):
    config.update_settings(excel_backend={"u@example.com": "openpyxl"})
    httpx_mock.add_response(url=f"{G}/me/drive/items/X!1", json=ITEM)
    httpx_mock.add_response(url=f"{G}/me/drive/items/X!1/content", content=xlsx())
    httpx_mock.add_response(method="PUT", url=f"{G}/me/drive/items/X!1/content", json=ITEM)
    assert excel.excel_append_rows("X!1", "Sheet1", [["c", 3]])["address"] == "A3:B3"
    ws = openpyxl.load_workbook(io.BytesIO(httpx_mock.get_requests()[-1].content))["Sheet1"]
    assert ws["A3"].value == "c"
    calls = len(httpx_mock.get_requests())
    with pytest.raises(Broker365Error, match="work/school"):
        excel.excel_table_add_rows("X!1", "Table1", [[1]])
    with pytest.raises(Broker365Error, match="work/school"):
        excel.excel_calc("X!1", "pmt", {})
    assert len(httpx_mock.get_requests()) == calls  # refused before downloading


def test_openpyxl_rejects_non_excel_file(httpx_mock, personal):
    config.update_settings(excel_backend={"u@example.com": "openpyxl"})
    httpx_mock.add_response(url=f"{G}/me/drive/items/X!1", json={**ITEM, "name": "b.csv"})
    with pytest.raises(Broker365Error, match="Excel file"):
        excel.excel_read_range("X!1", "Sheet1", "A1")
    assert len(httpx_mock.get_requests()) == 1  # rejected before downloading content


def test_reset_probe_clears_cache():
    config.update_settings(excel_backend={"u@example.com": "openpyxl"}, read_only=True)
    excel.reset_probe()
    assert cached() == {} and config.load_settings()["read_only"] is True


# ---- create -----------------------------------------------------------------------------


def test_create_uploads_valid_xlsx(httpx_mock):
    httpx_mock.add_response(
        method="PUT",
        url=f"{G}/me/drive/root:/Budget.xlsx:/content?%40microsoft.graph.conflictBehavior=fail",
        json={**ITEM, "name": "Budget.xlsx"},
    )
    out = excel.excel_create("/Budget.xlsx", ["Income", "Costs"])
    assert out["name"] == "Budget.xlsx" and out["sheets"] == ["Income", "Costs"]
    wb = openpyxl.load_workbook(io.BytesIO(httpx_mock.get_requests()[0].content))
    assert wb.sheetnames == ["Income", "Costs"]
    with pytest.raises(Broker365Error):
        excel.excel_create("/Budget.csv")
