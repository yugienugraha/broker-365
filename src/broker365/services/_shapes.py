"""Shared resource shapes: email addresses, recipients, dateTimeTimeZone (Graph) and 2-D cell
value arrays (Excel and Google Sheets)."""

from __future__ import annotations

import re
from typing import Any

from broker365.errors import Broker365Error

_EMAIL = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")


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
