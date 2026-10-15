"""Google Sheets (Sheets v4) for a connected Google account: the gsheets_* tools.

Spreadsheets are addressed like Drive files (id or absolute path). Ranges use A1 notation
with an optional tab: 'A1:C10', 'Budget 2026!A1:B2' (tab names that need it are quoted for
you), "'Q1!Sales'!A1" (a quoted name may contain '!'; '' is a quote), or just a tab name for
its whole used area. A bare value that looks like cells ('Q1', 'FY24') is a tab of exactly
that name if the spreadsheet has one, otherwise cells on the first tab.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from broker365 import config
from broker365.errors import Broker365Error
from broker365.services import _gnative, gdrive, require_consent, tool
from broker365.services._shapes import check_2d
from broker365.transport import seg

BUNDLE = "gsheets"
GOOGLE = config.GOOGLE
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
SHEET = _gnative.Native(
    BUNDLE, SHEETS, gdrive.SHEET, "Google Sheet", "the excel_* tools for workbooks in OneDrive"
)
# A1 cell areas without a tab: a cell ('B2', '$A$1') or a range with a colon ('A1:C10',
# 'A:C', '2:5'). A bare column ('Log') is not one, so it reads as a tab name.
_REF = r"(\$?[A-Za-z]{1,3}\$?\d*|\$?\d+)"
_CELLS = re.compile(rf"\$?[A-Za-z]{{1,3}}\$?\d+|{_REF}:{_REF}", re.ASCII)
_PLAIN_TAB = re.compile(r"[A-Za-z0-9_]+", re.ASCII)
TAB_FIELDS = "properties.title,sheets.properties(sheetId,title,index,gridProperties)"
Render = Literal["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"]


def quote_tab(tab: str) -> str:
    """A tab name as it must appear in A1 notation: quoted (with '' for ') unless it is plain
    letters/digits/underscores that can't be read as cells."""
    if _PLAIN_TAB.fullmatch(tab) and not _CELLS.fullmatch(tab):
        return tab
    return "'" + tab.replace("'", "''") + "'"


def _parse(range_: str) -> tuple[str | None, str | None]:
    """(tab name or None, cells or None) of a range, syntax only. A leading quote runs to its
    matching closing quote ('' is an escaped quote); only then is '!' looked for."""
    r = (range_ or "").strip()
    if not r:
        raise Broker365Error("A range is required, e.g. 'A1:C10' or 'Budget!A1:B2'")
    bad = Broker365Error(f"Invalid range '{range_}' (expected e.g. 'Budget!A1:B2')")
    if r.startswith("'"):
        name, i = [], 1
        while True:
            if i >= len(r):
                raise Broker365Error(f"Unterminated quote in range '{range_}'")
            if r[i] == "'":
                if r[i + 1 : i + 2] == "'":
                    name.append("'")
                    i += 2
                    continue
                break
            name.append(r[i])
            i += 1
        rest = r[i + 1 :]
        if not name or (rest and (not rest.startswith("!") or len(rest) == 1)):
            raise bad
        return "".join(name), rest[1:] or None
    if "!" in r:
        tab, cells = r.rsplit("!", 1)
        if not tab or not cells:
            raise bad
        return tab, cells
    return None, r


def _tab_titles(sheet_id: str) -> set[str]:
    out = SHEET.client().get(SHEET.url(sheet_id), params={"fields": "sheets.properties.title"})
    return {(s.get("properties") or {}).get("title") for s in out.get("sheets") or []}


def a1(range_: str, sheet_id: str | None = None) -> str:
    """Normalize an A1 range: quote the tab part where needed. A bare value that isn't a cell
    area is a tab name. A bare value that is one ('Q1', 'FY24', 'Jan1') is ambiguous: with
    `sheet_id`, the tab titles are read once and an exactly matching tab wins (the whole
    tab); otherwise (or without `sheet_id`) it is cells on the first tab."""
    tab, cells = _parse(range_)
    if tab is not None:
        return f"{quote_tab(tab)}!{cells}" if cells else quote_tab(tab)
    assert cells is not None
    if not _CELLS.fullmatch(cells):
        return quote_tab(cells)
    if sheet_id and cells in _tab_titles(sheet_id):
        return quote_tab(cells)
    return cells


def _values(values: Any) -> list[list[Any]]:
    """A non-empty 2-D list of scalars; None becomes an empty cell."""
    return [["" if v is None else v for v in row] for row in check_2d(values)]


def _values_url(sheet_id: str, rng: str, action: str = "") -> str:
    """URL of a normalized (a1) range's values."""
    return SHEET.url(sheet_id, f"/values/{seg(rng, 'range')}{action}")


def _input_option(raw: bool) -> str:
    return "RAW" if raw else "USER_ENTERED"


def _tab_info(s: dict[str, Any]) -> dict[str, Any]:
    p = s.get("properties") or {}
    grid = p.get("gridProperties") or {}
    return {
        "sheetId": p.get("sheetId"),
        "title": p.get("title"),
        "index": p.get("index"),
        "rowCount": grid.get("rowCount"),
        "colCount": grid.get("columnCount"),
    }


def _write(sheet_id: str, rng: str, values: list[list[Any]], raw: bool) -> dict[str, Any]:
    """Write to a normalized (a1) range."""
    out = SHEET.client().put(
        _values_url(sheet_id, rng),
        params={"valueInputOption": _input_option(raw)},
        json={"range": rng, "majorDimension": "ROWS", "values": values},
    )
    return {
        k: (out or {}).get(k)
        for k in ("updatedRange", "updatedRows", "updatedColumns", "updatedCells")
    }


def _create(
    title: str, remote_folder: str, conflict: str, tabs: list[str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(file summary, its tabs) of a new spreadsheet with `tabs` (default: one). In a Drive
    folder when Drive is granted (or the folder isn't the root), else with the Sheets API in
    My Drive's root."""
    client = SHEET.client()
    root = remote_folder.strip() in ("", "/")
    if root and not _gnative.has_consent(gdrive.BUNDLE):
        body: dict[str, Any] = {"properties": {"title": title}}
        if tabs:
            body["sheets"] = [{"properties": {"title": t}} for t in tabs]
        out = client.post(SHEETS, json=body)
        made = {
            "id": out.get("spreadsheetId"),
            "name": (out.get("properties") or {}).get("title"),
            "webViewLink": out.get("spreadsheetUrl"),
            "note": (
                "Created in My Drive's root with the Sheets API; the title wasn't checked for "
                "duplicates because Drive access (gdrive) isn't granted."
            ),
        }
        return made, out.get("sheets") or []
    made = gdrive.create(gdrive.folder_ref(remote_folder), title, gdrive.SHEET, conflict)
    if not tabs:
        return made, client.get(SHEET.url(made["id"]), params={"fields": TAB_FIELDS})["sheets"]
    # A new spreadsheet's only tab has sheetId 0: rename it and add the others in one call.
    requests: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": 0, "title": tabs[0]},
                "fields": "title",
            }
        }
    ]
    requests += [{"addSheet": {"properties": {"title": t}}} for t in tabs[1:]]
    out = client.post(
        SHEET.url(made["id"], ":batchUpdate"),
        json={
            "requests": requests,
            "includeSpreadsheetInResponse": True,
            "responseIncludeGridData": False,
        },
    )
    return made, (out.get("updatedSpreadsheet") or {}).get("sheets") or []


# ---- tools ------------------------------------------------------------------------------


@tool(BUNDLE, GOOGLE, writes=True, audit=("title", "remote_folder"))
def gsheets_create(
    title: str,
    remote_folder: str = "/",
    tabs: list[str] | None = None,
    values: list[list[Any]] | None = None,
    conflict: Literal["fail", "rename"] = "fail",
) -> dict[str, Any]:
    """Create a Google Sheet called `title` in a Drive folder (path or id; '/' = My Drive).
    `tabs` names the tabs (the first renames the default one); `values` (2-D list) is written
    to the first tab from A1, formulas included. conflict: 'fail' if the title is taken in
    the folder, or 'rename'. Without Drive access (gdrive) it can only create in '/', and
    then can't check for duplicate titles (the result has a `note` saying so)."""
    names = [t.strip() for t in tabs or []]
    if any(not t for t in names) or len({t.lower() for t in names}) != len(names):
        raise Broker365Error("Tab names must be non-empty and unique")
    rows = _values(values) if values is not None else None
    require_consent(GOOGLE, (BUNDLE,))  # don't leave an empty file if Sheets isn't granted
    made, sheets = _create(title, remote_folder, conflict, names)
    out: dict[str, Any] = {**made, "tabs": [_tab_info(s) for s in sheets]}
    if rows:
        first = out["tabs"][0]["title"] if out["tabs"] else None
        target = f"{quote_tab(first)}!A1" if first else "A1"
        out["written"] = _write(made["id"], target, rows, False)
    return out


@tool(BUNDLE, GOOGLE)
def gsheets_list_tabs(item: str) -> dict[str, Any]:
    """The tabs of a Google Sheet (id or path): title, sheetId, index, row/column count."""
    meta = SHEET.resolve(item)
    info = SHEET.client().get(SHEET.url(meta["id"]), params={"fields": TAB_FIELDS})
    return {
        "id": meta["id"],
        "name": (info.get("properties") or {}).get("title") or meta.get("name"),
        "tabs": [_tab_info(s) for s in info.get("sheets") or []],
    }


@tool(BUNDLE, GOOGLE, writes=True, audit=("title",))
def gsheets_add_tab(item: str, title: str, rows: int = 1000, cols: int = 26) -> dict[str, Any]:
    """Add a tab to a Google Sheet (id or path)."""
    if not title.strip():
        raise Broker365Error("A tab title is required")
    if rows < 1 or cols < 1:
        raise Broker365Error("rows and cols must be at least 1")
    meta = SHEET.resolve(item)
    grid = {"rowCount": rows, "columnCount": cols}
    request = {"addSheet": {"properties": {"title": title, "gridProperties": grid}}}
    replies, _ = SHEET.update(meta["id"], [request])
    added = _gnative.reply(replies, 0, "addSheet").get("properties") or {}
    return {"id": meta["id"], "tab": _tab_info({"properties": added})}


@tool(BUNDLE, GOOGLE)
def gsheets_read_range(item: str, range: str, render: Render = "FORMATTED_VALUE") -> dict:
    """Read cells of a Google Sheet. `range`: 'A1:C10', 'Tab name!A1:B5' or a tab name.
    render: FORMATTED_VALUE (as shown), UNFORMATTED_VALUE (raw numbers) or FORMULA.
    Trailing empty rows/cells are omitted by Google."""
    if render not in ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"):
        raise Broker365Error("render must be FORMATTED_VALUE, UNFORMATTED_VALUE or FORMULA")
    _parse(range)  # validate before any lookup
    meta = SHEET.resolve(item)
    url = _values_url(meta["id"], a1(range, meta["id"]))
    out = SHEET.client().get(url, params={"valueRenderOption": render})
    values = out.get("values") or []
    return {
        "id": meta["id"],
        "range": out.get("range"),
        "values": values,
        "rows": len(values),
        "cols": max((len(r) for r in values), default=0),
    }


@tool(BUNDLE, GOOGLE, writes=True, audit=("range",))
def gsheets_write_range(
    item: str, range: str, values: list[list[Any]], raw: bool = False
) -> dict[str, Any]:
    """Write a 2-D list of values to a Google Sheet range (e.g. 'Budget!A1'). Strings are
    interpreted as typed in the UI ('=SUM(A1:A3)' is a formula, '3%' a number) unless
    raw=true, which stores them literally."""
    rows = _values(values)
    _parse(range)
    meta = SHEET.resolve(item)
    return {"id": meta["id"], **_write(meta["id"], a1(range, meta["id"]), rows, raw)}


@tool(BUNDLE, GOOGLE, writes=True, audit=("range_or_tab",))
def gsheets_append_rows(
    item: str, range_or_tab: str, rows: list[list[Any]], raw: bool = False
) -> dict[str, Any]:
    """Append rows below the table found in `range_or_tab` (a tab name or range); new rows
    are inserted, never overwriting. Values are interpreted as typed unless raw=true."""
    values = _values(rows)
    _parse(range_or_tab)
    meta = SHEET.resolve(item)
    out = SHEET.client().post(
        _values_url(meta["id"], a1(range_or_tab, meta["id"]), ":append"),
        params={"valueInputOption": _input_option(raw), "insertDataOption": "INSERT_ROWS"},
        json={"majorDimension": "ROWS", "values": values},
    )
    updates = out.get("updates") or {}
    return {
        "id": meta["id"],
        "tableRange": out.get("tableRange"),
        "updatedRange": updates.get("updatedRange"),
        "updatedRows": updates.get("updatedRows"),
        "updatedCells": updates.get("updatedCells"),
    }


@tool(BUNDLE, GOOGLE, destructive=True, audit=("range",))
def gsheets_clear_range(item: str, range: str) -> dict[str, Any]:
    """Clear the values (not formatting) of a Google Sheet range, e.g. 'Budget!A2:D100'."""
    _parse(range)
    meta = SHEET.resolve(item)
    url = _values_url(meta["id"], a1(range, meta["id"]), ":clear")
    out = SHEET.client().post(url, json={}, idempotent=True)
    return {"id": meta["id"], "clearedRange": out.get("clearedRange")}
