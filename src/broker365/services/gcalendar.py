"""Google Calendar (Calendar v3) for a connected Google account: the gcal_* tools.

Datetimes are ISO 8601 without offset, interpreted in `time_zone` (an IANA name such as
'UTC' or 'Asia/Jakarta'), like the Outlook calendar tools; all-day events take dates
(YYYY-MM-DD). Attendees are only emailed when send_updates=true.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from broker365 import config, gapi
from broker365.errors import Broker365Error
from broker365.services import _mail, tool
from broker365.services._shapes import (
    compact,
    date_time_tz,
    iso_date,
    parse_iso,
    to_utc,
    validate_emails,
)
from broker365.transport import seg

BUNDLE = "gcalendar"
GOOGLE = config.GOOGLE
CALENDAR = "https://www.googleapis.com/calendar/v3"
# Exactly what _summarize reads.
LIST_FIELDS = (
    "nextPageToken,items(id,summary,start,end,location,organizer/email,attendees/email,"
    "hangoutLink,conferenceData/conferenceId,htmlLink,status)"
)


def _g() -> gapi.GoogleClient:
    return gapi.client(BUNDLE)


def _events_url(calendar: str, event_id: str | None = None) -> str:
    url = f"{CALENDAR}/calendars/{seg(calendar, 'calendar id')}/events"
    return f"{url}/{seg(event_id, 'event id')}" if event_id is not None else url


def _updates(send_updates: bool) -> dict[str, str]:
    return {"sendUpdates": "all" if send_updates else "none"}


def _when(e: dict[str, Any], key: str) -> str | None:
    t = e.get(key) or {}
    return t.get("dateTime") or t.get("date")


def _summarize(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": e.get("id"),
        "subject": e.get("summary"),
        "start": _when(e, "start"),
        "end": _when(e, "end"),
        "time_zone": (e.get("start") or {}).get("timeZone"),
        "all_day": "date" in (e.get("start") or {}),
        "location": e.get("location"),
        "organizer": (e.get("organizer") or {}).get("email"),
        "attendees": [a.get("email") for a in e.get("attendees") or []],
        "is_online_meeting": bool(e.get("hangoutLink") or e.get("conferenceData")),
        "join_url": e.get("hangoutLink"),
        "web_link": e.get("htmlLink"),
        "status": e.get("status"),
    }


_DATE_ONLY = re.compile(r"\s*\d{4}-\d{2}-\d{2}\s*", re.ASCII)
ALL_DAY_NEEDS = "All-day events need dates (YYYY-MM-DD)"


def _day(value: str):
    return iso_date(value, f"{ALL_DAY_NEEDS}, got {value!r}")


def _slot(value: str, time_zone: str) -> dict[str, str]:
    """A timed start/end in `time_zone`, normalized to ISO 8601 ('2026-10-15 09:00' →
    '2026-10-15T09:00:00'). A bare date is refused: that is what all_day is for."""
    if _DATE_ONLY.fullmatch(value):
        raise Broker365Error(
            f"{value!r} is a date without a time: use all_day=true for dates, or give a time "
            "such as 2026-10-15T09:00:00"
        )
    return date_time_tz(parse_iso(value).isoformat(timespec="seconds"), time_zone)


def _times(start: str, end: str, time_zone: str, all_day: bool) -> dict[str, Any]:
    """Event start/end. All-day: dates, end exclusive (an end on or before the start becomes
    the day after the start, i.e. a one-day event)."""
    if all_day:
        first, last = _day(start), _day(end)
        if last <= first:
            last = first + timedelta(days=1)
        return {"start": {"date": first.isoformat()}, "end": {"date": last.isoformat()}}
    return {"start": _slot(start, time_zone), "end": _slot(end, time_zone)}


def _changed_times(
    current: dict[str, Any],
    start: str | None,
    end: str | None,
    time_zone: str,
    all_day: bool | None,
) -> dict[str, Any]:
    """New start and end (always both, of one kind) for an existing event. all_day=None keeps
    the event's kind; a missing start or end keeps its duration. Switching kinds needs both
    and nulls the other kind's keys, as Google requires."""
    was_all_day = "date" in (current.get("start") or {})
    to_all_day = was_all_day if all_day is None else all_day
    switching = to_all_day != was_all_day
    if start is None or end is None:
        if switching:
            raise Broker365Error("Give both start and end when switching between all-day and timed")
        if start is None and end is None:
            return {}
        cur_start, cur_end = current.get("start") or {}, current.get("end") or {}
        if to_all_day:
            span = _day(cur_end["date"]) - _day(cur_start["date"])
            if start is None:
                start = (_day(end) - span).isoformat()  # type: ignore[arg-type]
            else:
                end = (_day(start) + span).isoformat()
        else:
            span = parse_iso(cur_end["dateTime"]) - parse_iso(cur_start["dateTime"])
            if start is None:
                start = (parse_iso(end) - span).isoformat(timespec="seconds")  # type: ignore[arg-type]
            else:
                end = (parse_iso(start) + span).isoformat(timespec="seconds")
    times = _times(start, end, time_zone, to_all_day)
    if switching:
        for key in ("start", "end"):
            nulls = {"dateTime": None, "timeZone": None} if to_all_day else {"date": None}
            times[key] = {**times[key], **nulls}
    return times


# ---- tools ------------------------------------------------------------------------------


@tool(BUNDLE, GOOGLE)
def gcal_list(
    start: str,
    end: str,
    calendar: str = "primary",
    limit: int = 50,
    time_zone: str = "UTC",
) -> list[dict[str, Any]]:
    """List Google Calendar events (recurring ones expanded) between `start` and `end`, ISO
    8601 datetimes such as 2026-10-14T00:00:00 read in `time_zone`; results are returned in
    `time_zone` too."""
    params = {
        "timeMin": to_utc(start, time_zone),
        "timeMax": to_utc(end, time_zone),
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeZone": time_zone,
        "fields": LIST_FIELDS,
    }
    return [_summarize(e) for e in _g().paged(_events_url(calendar), params, "items", limit)]


@tool(BUNDLE, GOOGLE, destructive=True, audit=("calendar", "send_updates"))
def gcal_create(
    summary: str,
    start: str,
    end: str,
    time_zone: str = "UTC",
    attendees: list[str] | None = None,
    location: str | None = None,
    description: str | None = None,
    all_day: bool = False,
    calendar: str = "primary",
    send_updates: bool = False,
) -> dict[str, Any]:
    """Create a Google Calendar event; `start`/`end` are ISO 8601 datetimes in `time_zone`
    (dates YYYY-MM-DD with all_day=true). Attendees get an email invitation only with
    send_updates=true."""
    event = {
        "summary": summary,
        **_times(start, end, time_zone, all_day),
        **compact(
            {
                "attendees": [{"email": a} for a in validate_emails(attendees)] or None,
                "location": location,
                "description": description,
            }
        ),
    }
    out = _g().post(_events_url(calendar), json=event, params=_updates(send_updates))
    return _summarize(out)


@tool(BUNDLE, GOOGLE, writes=True, audit=("calendar", "send_updates"))
def gcal_update(
    event_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    time_zone: str = "UTC",
    location: str | None = None,
    description: str | None = None,
    calendar: str = "primary",
    send_updates: bool = False,
    all_day: bool | None = None,
) -> dict[str, Any]:
    """Change a Google Calendar event; only the fields you pass are updated. `start`/`end`
    are ISO 8601 datetimes in `time_zone` (dates for all-day events); give just one to move
    the event and keep its length. all_day=true/false switches the kind (then give both).
    Attendees are emailed only with send_updates=true."""
    url = _events_url(calendar, event_id)
    patch = compact({"summary": summary, "location": location, "description": description})
    if start is not None or end is not None or all_day is not None:
        current = _g().get(url, params={"fields": "start,end"}) or {}
        patch |= _changed_times(current, start, end, time_zone, all_day)
    if not patch:
        raise Broker365Error("Nothing to update: pass at least one field")
    out = _g().patch(url, json=patch, params=_updates(send_updates))
    return _summarize(out)


@tool(BUNDLE, GOOGLE, destructive=True, audit=("calendar", "notify"))
def gcal_delete(event_id: str, calendar: str = "primary", notify: bool = False) -> dict[str, Any]:
    """Delete a Google Calendar event. notify=true emails attendees a cancellation."""
    _g().request("DELETE", _events_url(calendar, event_id), params=_updates(notify))
    return {"deleted": True, "id": event_id}


@tool(BUNDLE, GOOGLE)
def gcal_free_busy(
    emails: list[str], start: str, end: str, time_zone: str = "UTC"
) -> list[dict[str, Any]]:
    """Busy periods of people's (or calendars') Google Calendars between ISO 8601 `start` and
    `end` in `time_zone`. Only calendars shared with this account report details."""
    ids = _mail.required_emails(emails, "email address")
    payload = {
        "timeMin": to_utc(start, time_zone),
        "timeMax": to_utc(end, time_zone),
        "timeZone": time_zone,
        "items": [{"id": e} for e in ids],
    }
    res = _g().post(f"{CALENDAR}/freeBusy", json=payload, idempotent=True) or {}
    calendars = res.get("calendars") or {}
    out = []
    for email in ids:
        c = calendars.get(email) or {}
        errors = [e.get("reason") for e in c.get("errors") or []]
        out.append(
            {
                "email": email,
                "busy": [
                    {"start": b.get("start"), "end": b.get("end"), "status": "busy"}
                    for b in c.get("busy") or []
                ],
                "error": ", ".join(e for e in errors if e) or None,
            }
        )
    return out
