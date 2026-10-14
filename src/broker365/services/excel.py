"""Excel workbooks (.xlsx) via the Graph Workbook API, each tool call inside its own
persistent session (sessionless if createSession is refused). Personal accounts are
probed once; if the Workbook API is unavailable, an openpyxl download/edit/re-upload
round-trip (eTag guarded) runs behind the same tool signatures.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from broker365 import auth, config, graph
from broker365.errors import Broker365Error, GraphError
from broker365.graph import item_ref
from broker365.services import files, tool

WORKBOOK, OPENPYXL = "workbook", "openpyxl"
EXCEL_EXTS = (".xlsx", ".xlsm")
MAX_ROWS, MAX_COLS = 1_048_576, 16_384
CHUNK_CELLS = 5000
OPENPYXL_NOTE = (
    "Edited by download, openpyxl and re-upload: formulas are returned as text and are not "
    "recalculated until the file is opened in Excel."
)
_CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]+)$")
_FUNC = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")

# ---- A1 addresses -----------------------------------------------------------------------


@dataclass(frozen=True)
class Area:
    row1: int
    col1: int
    row2: int
    col2: int

    @property
    def rows(self) -> int:
        return self.row2 - self.row1 + 1

    @property
    def cols(self) -> int:
        return self.col2 - self.col1 + 1

    @property
    def address(self) -> str:
        a = f"{get_column_letter(self.col1)}{self.row1}"
        b = f"{get_column_letter(self.col2)}{self.row2}"
        return a if a == b else f"{a}:{b}"


def _cell(text: str, address: str) -> tuple[int, int]:
    m = _CELL.match(text.strip())
    if not m:
        if re.fullmatch(r"\$?[A-Za-z]{1,3}|\$?[0-9]+", text.strip()):
            raise Broker365Error(
                f"Unbounded range '{address}' is not allowed; give both corners, e.g. 'A1:C10'"
            )
        raise Broker365Error(f"Invalid cell address '{address}' (expected e.g. 'B2' or 'A1:C10')")
    row, col = int(m.group(2)), column_index_from_string(m.group(1).upper())
    if not (1 <= row <= MAX_ROWS and 1 <= col <= MAX_COLS):
        raise Broker365Error(f"Address '{address}' is outside the Excel grid")
    return row, col


def parse_address(address: str) -> Area:
    """'B2' or 'A1:C3' → Area. Sheet prefixes and unbounded ranges ('A:B', '1:3') are refused."""
    a = (address or "").strip()
    if "!" in a:
        raise Broker365Error("Pass the sheet name separately; use an address like 'A1:C10'")
    parts = a.split(":")
    if not a or len(parts) > 2:
        raise Broker365Error(f"Invalid range address '{address}' (expected e.g. 'A1:C10')")
    (r1, c1), (r2, c2) = _cell(parts[0], a), _cell(parts[-1], a)
    return Area(min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2))


def _strip_sheet(address: str) -> str:
    return address.rsplit("!", 1)[-1]


def _check_values(values: Any, area: Area | None = None) -> list[list[Any]]:
    if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values):
        raise Broker365Error("values must be a non-empty 2-D array (a list of rows)")
    for row in values:
        for v in row:
            if v is not None and not isinstance(v, str | int | float | bool):
                raise Broker365Error("Cell values must be strings, numbers, booleans or null")
    if area is not None:
        widths = {len(r) for r in values}
        if len(values) != area.rows or widths != {area.cols}:
            got = f"{len(values)}x{'/'.join(str(w) for w in sorted(widths))}"
            raise Broker365Error(
                f"values are {got} but {area.address} is {area.rows}x{area.cols} (rows x columns)"
            )
    return values


def _pad(rows: list[list[Any]]) -> list[list[Any]]:
    width = max(len(r) for r in rows)
    if not width:
        raise Broker365Error("rows must contain at least one value")
    return [r + [None] * (width - len(r)) for r in rows]


def _bands(area: Area, values: list[list[Any]]) -> Iterator[tuple[Area, list[list[Any]]]]:
    step = max(1, CHUNK_CELLS // area.cols)
    for i in range(0, area.rows, step):
        band = values[i : i + step]
        r1 = area.row1 + i
        yield Area(r1, area.col1, r1 + len(band) - 1, area.col2), band


# ---- backends ---------------------------------------------------------------------------


def _sheet_seg(sheet: str) -> str:
    return f"worksheets('{graph.seg(graph.odata_str(sheet or ''), 'sheet name')}')"


def _range_seg(sheet: str, area: Area) -> str:
    return f"{_sheet_seg(sheet)}/range(address='{area.address}')"


@dataclass
class _WB:
    g: graph.GraphClient
    base: str
    headers: dict[str, str] = field(default_factory=dict)

    def get(self, path: str, **kw: Any) -> Any:
        return self.g.get(f"{self.base}/{path}", headers=self.headers, **kw)

    def post(self, path: str, json: Any = None) -> Any:
        return self.g.post(f"{self.base}/{path}", json=json, headers=self.headers)

    def patch(self, path: str, json: Any) -> Any:
        return self.g.patch(f"{self.base}/{path}", json=json, headers=self.headers)

    def delete(self, path: str) -> None:
        self.g.delete(f"{self.base}/{path}", headers=self.headers)


@contextmanager
def _workbook(item: str, writes: bool = True) -> Iterator[_WB]:
    """One persistent session per tool call. Measured live (Oct 2026): sessionless reads of a
    just-written workbook lag 3-6 s, while a new session sees the latest writes at once.
    Closing it right away keeps the post-write overwrite lock short (~20 s, as sessionless)."""
    wb = _WB(files._g(), f"{item_ref(item)}/workbook")
    try:
        # Reads use a non-persistent session (Microsoft's recommendation for read-only work).
        sid = wb.post("createSession", {"persistChanges": writes})["id"]
    except GraphError as e:
        # Docs list personal accounts as unsupported (it worked live in Oct 2026). Only fall
        # back for "can't have a session"; throttling, 404s etc. surface as real errors.
        if not (_unsupported(e) or e.status == 400):
            raise
        sid = None
    wb.headers = {"workbook-session-id": sid} if sid else {}
    try:
        yield wb
    finally:
        if sid:
            try:  # closing twice is harmless, so 503/504 may be retried
                wb.g.post(f"{wb.base}/closeSession", headers=wb.headers, idempotent=True)
            except Broker365Error:
                pass


def _unsupported(err: GraphError) -> bool:
    return "notsupported" in (err.code or "").lower() or err.status == 501


def _remember(username: str, backend: str) -> None:
    cache = dict(config.load_settings().get("excel_backend", {}))
    cache[username] = backend
    config.update_settings(excel_backend=cache)


def reset_probe() -> None:
    """Forget the cached per-account backend choice so personal accounts are probed again."""
    config.update_settings(excel_backend=None)


def _probe(item: str, username: str) -> str:
    try:
        files._g().get(f"{item_ref(item)}/workbook/worksheets")
    except GraphError as e:
        if _unsupported(e):
            _remember(username, OPENPYXL)
        return OPENPYXL  # 400/403/404 may be about this file only: fall back without caching
    _remember(username, WORKBOOK)
    return WORKBOOK


def _select(item: str) -> tuple[str, str | None]:
    """(backend, username-if-personal)."""
    acct = auth.current_account()
    if not auth.is_personal_account(acct):
        return WORKBOOK, None
    username = acct["username"]
    cached = config.load_settings().get("excel_backend", {}).get(username)
    if cached in (WORKBOOK, OPENPYXL):
        return cached, username
    return _probe(item, username), username


def _load(data: bytes, name: str) -> Workbook:
    try:
        # keep_vba so a .xlsm keeps its macros (and its content type) on re-upload
        return openpyxl.load_workbook(io.BytesIO(data), keep_vba=name.lower().endswith(".xlsm"))
    except (InvalidFileException, zipfile.BadZipFile, KeyError) as e:
        raise Broker365Error("Not a valid .xlsx workbook") from e


# An op returns extra result fields, or None when the caller builds the result itself.
_Op = Callable[[Any], dict[str, Any] | None]


def _with_openpyxl(item: str, op: _Op, writes: bool) -> dict[str, Any]:
    data, meta = files.download_office(item, EXCEL_EXTS, "Excel")
    wb = _load(data, meta.get("name", ""))
    result = op(wb) or {}
    if writes:
        files.replace_bytes(meta, files.to_bytes(wb))
    return {**result, "backend": OPENPYXL, "note": OPENPYXL_NOTE}


def _run(
    item: str,
    wb_op: _Op,
    px_op: _Op | None,
    *,
    writes: bool = False,
    feature: str = "This operation",
) -> dict[str, Any]:
    """Run wb_op via the Workbook API or px_op via an openpyxl round-trip. px_op=None marks a
    Workbook-only `feature`, refused on the openpyxl backend before anything is downloaded."""
    backend, username = _select(item)
    if backend == WORKBOOK:
        try:
            with _workbook(item, writes) as wb:
                return {**(wb_op(wb) or {}), "backend": WORKBOOK}
        except GraphError as e:
            if username is None or not _unsupported(e):
                raise
            _remember(username, OPENPYXL)
    if px_op is None:
        raise Broker365Error(
            f"{feature} needs the Excel Workbook API, which this personal account doesn't "
            "support. It works with a work/school account."
        )
    return _with_openpyxl(item, px_op, writes)


# ---- openpyxl helpers -------------------------------------------------------------------


def _ws(wb: Workbook, sheet: str) -> Worksheet:
    for name in wb.sheetnames:
        if name.lower() == (sheet or "").lower():
            return wb[name]
    raise Broker365Error(f"No sheet named '{sheet}' (sheets: {', '.join(wb.sheetnames)})")


def _jsonable(v: Any) -> Any:
    if isinstance(v, dt.datetime | dt.date | dt.time):
        return v.isoformat()
    if v is None or isinstance(v, str | int | float | bool):
        return v
    return str(v)


def _px_read(ws: Worksheet, area: Area) -> list[list[Any]]:
    return [
        [_jsonable(v) for v in row]
        for row in ws.iter_rows(
            min_row=area.row1,
            max_row=area.row2,
            min_col=area.col1,
            max_col=area.col2,
            values_only=True,
        )
    ]


def _px_write(ws: Worksheet, area: Area, values: list[list[Any]]) -> None:
    for i, row in enumerate(values):
        for j, v in enumerate(row):
            if v is not None:
                ws.cell(row=area.row1 + i, column=area.col1 + j, value=None if v == "" else v)


def _px_used(ws: Worksheet) -> Area | None:
    """Bounding box of cells holding values (formatted-but-empty cells don't count, matching
    the Workbook API's valuesOnly used range)."""
    cells = [(c.row, c.column) for row in ws.iter_rows() for c in row if c.value is not None]
    if not cells:
        return None
    rows, cols = zip(*cells, strict=True)
    return Area(min(rows), min(cols), max(rows), max(cols))


def _blank(values: Any) -> bool:
    """Graph reports an empty sheet's used range as a single empty cell."""
    return (values or [[""]]) == [[""]]


def _wb_used(wb: _WB, sheet: str) -> tuple[Area | None, dict[str, Any]]:
    used = wb.get(f"{_sheet_seg(sheet)}/usedRange", params={"$select": "address,values"})
    if _blank(used.get("values")):
        return None, used
    return parse_address(_strip_sheet(used["address"])), used


def _wb_used_area(wb: _WB, sheet: str) -> Area | None:
    """Area holding values (formatting ignored), without pulling the values themselves unless
    the answer is a single cell, which is also how an empty sheet looks."""
    path = f"{_sheet_seg(sheet)}/usedRange(valuesOnly=true)"
    area = parse_address(_strip_sheet(wb.get(path, params={"$select": "address"})["address"]))
    if area.rows == area.cols == 1 and _blank(wb.get(path, params={"$select": "values"})["values"]):
        return None
    return area


def _wb_write(wb: _WB, sheet: str, area: Area, values: list[list[Any]]) -> None:
    for band, chunk in _bands(area, values):
        wb.patch(_range_seg(sheet, band), {"values": chunk})


def _written(area: Area) -> dict[str, Any]:
    return {"address": area.address, "rows": area.rows, "columns": area.cols}


# ---- tools ------------------------------------------------------------------------------


@tool("files", writes=True)
def excel_create(
    remote_path: str,
    sheets: list[str] | None = None,
    conflict: Literal["fail", "replace", "rename"] = "fail",
) -> dict[str, Any]:
    """Create a new empty .xlsx workbook at an absolute OneDrive path ('/Budget.xlsx'),
    optionally with the given sheet names."""
    if not remote_path.lower().endswith(".xlsx"):
        raise Broker365Error("Workbook path must end in .xlsx, e.g. /Budget.xlsx")
    names = sheets or ["Sheet1"]
    if len({n.lower() for n in names}) != len(names):
        raise Broker365Error("Sheet names must be unique")
    wb = Workbook()
    wb.active.title = names[0]
    for name in names[1:]:
        wb.create_sheet(name)
    out = files.create_bytes(remote_path, files.to_bytes(wb), conflict)
    return {**out, "sheets": wb.sheetnames, "backend": OPENPYXL}


@tool("files")
def excel_list_sheets(item: str) -> dict[str, Any]:
    """List the worksheets of a workbook. `item` is an absolute OneDrive path ('/Budget.xlsx')
    or an item id."""

    def wb_op(wb: _WB) -> dict[str, Any]:
        rows = wb.get("worksheets").get("value", [])
        return {"sheets": [{k: s.get(k) for k in ("name", "position", "visibility")} for s in rows]}

    def px_op(wb: Workbook) -> dict[str, Any]:
        return {
            "sheets": [
                {"name": ws.title, "position": i, "visibility": ws.sheet_state}
                for i, ws in enumerate(wb.worksheets)
            ]
        }

    return _run(item, wb_op, px_op)


@tool("files", writes=True)
def excel_add_sheet(item: str, name: str) -> dict[str, Any]:
    """Add a worksheet named `name` to a workbook (`item`: '/Budget.xlsx' or an item id)."""

    def wb_op(wb: _WB) -> dict[str, Any]:
        ws = wb.post("worksheets/add", {"name": name})
        return {"name": ws.get("name"), "position": ws.get("position")}

    def px_op(wb: Workbook) -> dict[str, Any]:
        if name.lower() in (n.lower() for n in wb.sheetnames):
            raise Broker365Error(f"A sheet named '{name}' already exists")
        ws = wb.create_sheet(name)
        return {"name": ws.title, "position": wb.worksheets.index(ws)}

    return _run(item, wb_op, px_op, writes=True)


@tool("files", writes=True)
def excel_rename_sheet(item: str, sheet: str, new_name: str) -> dict[str, Any]:
    """Rename worksheet `sheet` to `new_name` (`item`: '/Budget.xlsx' or an item id)."""

    def wb_op(wb: _WB) -> dict[str, Any]:
        return {"name": wb.patch(_sheet_seg(sheet), {"name": new_name}).get("name", new_name)}

    def px_op(wb: Workbook) -> dict[str, Any]:
        ws = _ws(wb, sheet)
        ws.title = new_name
        return {"name": ws.title}

    return _run(item, wb_op, px_op, writes=True)


@tool("files", destructive=True)
def excel_delete_sheet(item: str, sheet: str) -> dict[str, Any]:
    """Delete worksheet `sheet` and all its data (`item`: '/Budget.xlsx' or an item id)."""

    def wb_op(wb: _WB) -> dict[str, Any]:
        wb.delete(_sheet_seg(sheet))
        return {"deleted": True, "sheet": sheet}

    def px_op(wb: Workbook) -> dict[str, Any]:
        ws = _ws(wb, sheet)
        if len(wb.worksheets) == 1:
            raise Broker365Error("A workbook must keep at least one sheet")
        wb.remove(ws)
        return {"deleted": True, "sheet": ws.title}

    return _run(item, wb_op, px_op, writes=True)


@tool("files")
def excel_read_range(item: str, sheet: str, address: str) -> dict[str, Any]:
    """Read cell values and formulas from a range like 'A1:C10' (or a single cell 'B2').
    `item` is an absolute OneDrive path ('/Budget.xlsx') or an item id."""
    area = parse_address(address)

    def wb_op(wb: _WB) -> dict[str, Any]:
        r = wb.get(_range_seg(sheet, area), params={"$select": "address,values,formulas,text"})
        return {
            "address": _strip_sheet(r.get("address", area.address)),
            "values": r.get("values"),
            "formulas": r.get("formulas"),
            "text": r.get("text"),
        }

    def px_op(wb: Workbook) -> dict[str, Any]:
        values = _px_read(_ws(wb, sheet), area)
        return {"address": area.address, "values": values, "formulas": values}

    return _run(item, wb_op, px_op)


@tool("files")
def excel_used_range(item: str, sheet: str) -> dict[str, Any]:
    """Read the used area of a sheet (its address, e.g. 'A1:D20', and values).
    `item` is an absolute OneDrive path ('/Budget.xlsx') or an item id."""

    def wb_op(wb: _WB) -> dict[str, Any]:
        area, used = _wb_used(wb, sheet)
        return {
            "address": area.address if area else None,
            "values": used.get("values") if area else [],
        }

    def px_op(wb: Workbook) -> dict[str, Any]:
        ws = _ws(wb, sheet)
        area = _px_used(ws)
        return {
            "address": area.address if area else None,
            "values": _px_read(ws, area) if area else [],
        }

    return _run(item, wb_op, px_op)


@tool("files", writes=True)
def excel_write_range(item: str, sheet: str, address: str, values: list[list[Any]]) -> dict:
    """Write a 2-D array of values into a range like 'A1:C3' whose size must match; strings
    starting with '=' are formulas, null leaves a cell unchanged and "" clears it.
    `item` is an absolute OneDrive path ('/Budget.xlsx') or an item id."""
    area = parse_address(address)
    _check_values(values, area)
    out = _run(
        item,
        lambda wb: _wb_write(wb, sheet, area, values),
        lambda wb: _px_write(_ws(wb, sheet), area, values),
        writes=True,
    )
    return {**_written(area), **out}


@tool("files", writes=True)
def excel_append_rows(item: str, sheet: str, rows: list[list[Any]]) -> dict[str, Any]:
    """Append rows below the last used row of a sheet (strings starting with '=' are formulas).
    `item` is an absolute OneDrive path ('/Budget.xlsx') or an item id."""
    rows = _pad(_check_values(rows))

    def target(used: Area | None) -> Area:
        row1, col1 = (used.row2 + 1, used.col1) if used else (1, 1)
        area = Area(row1, col1, row1 + len(rows) - 1, col1 + len(rows[0]) - 1)
        if area.row2 > MAX_ROWS or area.col2 > MAX_COLS:
            raise Broker365Error("Appending these rows would exceed the Excel grid")
        return area

    def wb_op(wb: _WB) -> dict[str, Any]:
        area = target(_wb_used_area(wb, sheet))
        _wb_write(wb, sheet, area, rows)
        return _written(area)

    def px_op(wb: Workbook) -> dict[str, Any]:
        ws = _ws(wb, sheet)
        area = target(_px_used(ws))
        _px_write(ws, area, rows)
        return _written(area)

    return _run(item, wb_op, px_op, writes=True)


@tool("files", writes=True)
def excel_table_add_rows(item: str, table: str, rows: list[list[Any]]) -> dict[str, Any]:
    """Append rows to an Excel table (by table name or id); each row must have one value per
    column. Work/school accounts only. `item` is '/Budget.xlsx' or an item id."""
    _check_values(rows)

    def wb_op(wb: _WB) -> dict[str, Any]:
        r = wb.post(f"tables/{graph.seg(table, 'table name')}/rows", {"values": rows})
        return {"table": table, "index": (r or {}).get("index"), "rows": len(rows)}

    return _run(item, wb_op, None, writes=True, feature="Excel tables")


@tool("files")
def excel_calc(item: str, function: str, args: dict[str, Any]) -> dict[str, Any]:
    """Evaluate an Excel worksheet function (e.g. 'pmt', 'vlookup') with named arguments via
    the Workbook API. Work/school accounts only. `item` is '/Budget.xlsx' or an item id."""
    if not _FUNC.match(function or ""):
        raise Broker365Error(f"Invalid function name '{function}'")

    def wb_op(wb: _WB) -> dict[str, Any]:
        r = wb.post(f"functions/{function.lower()}", args or {}) or {}
        return {"function": function, "value": r.get("value"), "error": r.get("error")}

    return _run(item, wb_op, None, feature="Excel functions")
