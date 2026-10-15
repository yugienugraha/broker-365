"""Outlook calendar. Datetimes are ISO 8601 without offset, interpreted in `time_zone`
(an IANA or Windows time zone name, e.g. 'UTC', 'Europe/Amsterdam')."""

from __future__ import annotations

from typing import Any

from broker365 import graph
from broker365.errors import Broker365Error, GraphError
from broker365.services import tool
from broker365.services._shapes import (
    addr,
    compact,
    date_time,
    date_time_tz,
    recipients,
    to_utc,
    validate_emails,
)

EVENT_SELECT = (
    "id,subject,start,end,location,organizer,attendees,isOnlineMeeting,onlineMeeting,"
    "onlineMeetingUrl,webLink,isAllDay,showAs"
)


def _g() -> graph.GraphClient:
    return graph.client("calendar")


def _event_path(event_id: str) -> str:
    return f"/me/events/{graph.seg(event_id, 'event id')}"


def _text_body(body: str) -> dict[str, str]:
    return {"contentType": "Text", "content": body}


def _summarize(e: dict[str, Any]) -> dict[str, Any]:
    join = (e.get("onlineMeeting") or {}).get("joinUrl") or e.get("onlineMeetingUrl")
    return {
        "id": e.get("id"),
        "subject": e.get("subject"),
        "start": date_time(e, "start"),
        "end": date_time(e, "end"),
        "time_zone": (e.get("start") or {}).get("timeZone"),
        "all_day": e.get("isAllDay"),
        "location": (e.get("location") or {}).get("displayName") or None,
        "organizer": addr(e.get("organizer"), named=False),
        "attendees": [addr(a, named=False) for a in e.get("attendees") or []],
        "is_online_meeting": e.get("isOnlineMeeting"),
        "join_url": join,
        "web_link": e.get("webLink"),
    }


@tool("calendar")
def calendar_list(
    start: str, end: str, limit: int = 50, time_zone: str = "UTC"
) -> list[dict[str, Any]]:
    """List calendar events (recurring ones expanded) between `start` and `end`, ISO 8601
    datetimes such as 2026-10-14T00:00:00. Results are returned in `time_zone`."""
    params = {
        "startDateTime": to_utc(start, time_zone),
        "endDateTime": to_utc(end, time_zone),
        "$select": EVENT_SELECT,
        "$orderby": "start/dateTime",
    }
    headers = {"Prefer": f'outlook.timezone="{time_zone}"'}
    events = _g().paged("/me/calendarView", params=params, limit=limit, headers=headers)
    return [_summarize(e) for e in events]


@tool("calendar", destructive=True)
def calendar_create(
    subject: str,
    start: str,
    end: str,
    time_zone: str = "UTC",
    attendees: list[str] | None = None,
    location: str | None = None,
    body: str | None = None,
    online_meeting: bool = False,
) -> dict[str, Any]:
    """Create an event; `start`/`end` are ISO 8601 datetimes in `time_zone`. Attendees are
    emailed an invitation. online_meeting=true adds a Teams link (work/school accounts)."""
    event: dict[str, Any] = {
        "subject": subject,
        "start": date_time_tz(start, time_zone),
        "end": date_time_tz(end, time_zone),
        **compact(
            {
                "attendees": [{**r, "type": "required"} for r in recipients(attendees)] or None,
                "location": {"displayName": location} if location else None,
                "body": _text_body(body) if body else None,
            }
        ),
    }
    if online_meeting:
        event |= {"isOnlineMeeting": True, "onlineMeetingProvider": "teamsForBusiness"}
    return _summarize(_g().post("/me/events", json=event))


@tool("calendar", writes=True)
def calendar_update(
    event_id: str,
    subject: str | None = None,
    start: str | None = None,
    end: str | None = None,
    time_zone: str = "UTC",
    location: str | None = None,
    body: str | None = None,
) -> dict[str, Any]:
    """Change an event; only the fields you pass are updated. `start`/`end` are ISO 8601
    datetimes in `time_zone`. Attendees are notified of the change."""
    patch = compact(
        {
            "subject": subject,
            "start": None if start is None else date_time_tz(start, time_zone),
            "end": None if end is None else date_time_tz(end, time_zone),
            "location": None if location is None else {"displayName": location},
            "body": None if body is None else _text_body(body),
        }
    )
    if not patch:
        raise Broker365Error("Nothing to update: pass at least one field")
    return _summarize(_g().patch(_event_path(event_id), json=patch))


@tool("calendar", destructive=True)
def calendar_delete(event_id: str) -> dict[str, Any]:
    """Delete an event. If you organized it, attendees receive a cancellation."""
    _g().delete(_event_path(event_id))
    return {"deleted": True, "id": event_id}


@tool("calendar")
def calendar_free_busy(
    emails: list[str],
    start: str,
    end: str,
    interval_minutes: int = 30,
    time_zone: str = "UTC",
) -> list[dict[str, Any]]:
    """Free/busy for people (work/school accounts only) between ISO 8601 `start` and `end` in
    `time_zone`. availability_view has one digit per interval: 0 free, 1 tentative, 2 busy,
    3 out of office, 4 working elsewhere."""
    payload = {
        "schedules": validate_emails(emails),
        "startTime": date_time_tz(start, time_zone),
        "endTime": date_time_tz(end, time_zone),
        "availabilityViewInterval": interval_minutes,
    }
    try:
        res = _g().post("/me/calendar/getSchedule", json=payload)
    except GraphError as e:
        raise Broker365Error(
            f"Free/busy lookup failed ({e.code}: {e.message}). Note getSchedule is only "
            "available to work or school accounts; personal accounts can use calendar_list."
        ) from e
    return [
        {
            "email": s.get("scheduleId"),
            "availability_view": s.get("availabilityView"),
            "busy": [
                {
                    "start": date_time(i, "start"),
                    "end": date_time(i, "end"),
                    "status": i.get("status"),
                    "subject": i.get("subject"),
                }
                for i in s.get("scheduleItems") or []
            ],
            "error": (s.get("error") or {}).get("message"),
        }
        for s in (res or {}).get("value", [])
    ]
