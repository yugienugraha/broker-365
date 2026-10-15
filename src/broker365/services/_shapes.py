"""Shared resource shapes and conversions used by both providers: email addresses and
recipients, HTML → text, datetimes (dateTimeTimeZone, UTC windows) and 2-D cell arrays."""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from broker365.errors import Broker365Error

_EMAIL = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")
_DROP = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.I | re.S)
_BREAK = re.compile(r"<br\s*/?>|</(p|div|li|tr|h[1-6]|table|blockquote)\s*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def validate_emails(addrs: list[str] | None) -> list[str]:
    """Stripped addresses, or Broker365Error on the first invalid one."""
    out = []
    for a in addrs or []:
        a = a.strip()
        if not _EMAIL.match(a):
            raise Broker365Error(f"Invalid email address: {a!r}")
        out.append(a)
    return out


def recipients(addrs: list[str] | None) -> list[dict[str, Any]]:
    """Graph recipient objects for validated addresses."""
    return [{"emailAddress": {"address": a}} for a in validate_emails(addrs)]


def address_of(ea: dict[str, Any] | None, named: bool = False) -> str | None:
    """Format a Graph emailAddress object: 'address', or 'Name <address>' when `named`."""
    if not ea:
        return None
    name, address = ea.get("name"), ea.get("address")
    return f"{name} <{address}>" if named and name and name != address else address


def addr(r: dict[str, Any] | None, named: bool = True) -> str | None:
    """Format a Graph recipient (organizer, attendee, from/to …), see address_of."""
    return address_of((r or {}).get("emailAddress"), named)


def date_time_tz(value: str, tz: str) -> dict[str, str]:
    """A Graph dateTimeTimeZone object."""
    return {"dateTime": value, "timeZone": tz}


def date_time(obj: dict[str, Any], key: str) -> str | None:
    """The dateTime of the dateTimeTimeZone at obj[key], if any."""
    return (obj.get(key) or {}).get("dateTime")


def compact(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop the None values (fields that weren't given)."""
    return {k: v for k, v in fields.items() if v is not None}


def check_2d(values: Any) -> list[list[Any]]:
    """`values` if it is a non-empty 2-D list of scalar cells (str, number, bool or null)."""
    if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values):
        raise Broker365Error("values must be a non-empty 2-D array (a list of rows)")
    for row in values:
        for v in row:
            if v is not None and not isinstance(v, str | int | float | bool):
                raise Broker365Error("Cell values must be strings, numbers, booleans or null")
    return values


def html_to_text(content: str) -> str:
    """Readable plain text from an HTML mail body or page."""
    text = _BREAK.sub("\n", _DROP.sub("", content))
    text = html.unescape(_TAG.sub("", text)).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def parse_iso(value: str) -> datetime:
    """An ISO 8601 datetime (with or without offset), or Broker365Error."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise Broker365Error(f"Not an ISO 8601 datetime: {value!r}") from exc


def iso_date(value: str, message: str | None = None) -> date:
    """A real calendar date written YYYY-MM-DD (2026-13-45 is refused), or Broker365Error
    with `message`."""
    try:
        if isinstance(value, str) and _DATE.fullmatch(value):
            return date.fromisoformat(value)
    except ValueError:
        pass
    raise Broker365Error(message or f"Not a date in YYYY-MM-DD format: {value!r}")


def to_utc(value: str, time_zone: str) -> str:
    """An ISO 8601 datetime as UTC ('...Z'). Offset-less values are read in `time_zone`
    (calendar APIs would otherwise take them as UTC)."""
    dt = parse_iso(value)
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=UTC if time_zone.upper() == "UTC" else ZoneInfo(time_zone))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise Broker365Error(
                f"Unknown time zone {time_zone!r} for start/end; use an IANA name such as "
                "'Asia/Jakarta', or include a UTC offset in the datetimes"
            ) from exc
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
