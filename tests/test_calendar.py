import json
import re

import pytest

from broker365.errors import Broker365Error
from broker365.services import calendar
from tests.conftest import G

EVENT = {
    "id": "e1",
    "subject": "Sync",
    "start": {"dateTime": "2026-10-14T09:00:00", "timeZone": "Europe/Amsterdam"},
    "end": {"dateTime": "2026-10-14T09:30:00", "timeZone": "Europe/Amsterdam"},
    "organizer": {"emailAddress": {"address": "me@contoso.com"}},
    "attendees": [{"emailAddress": {"address": "bob@contoso.com"}}],
    "onlineMeeting": {"joinUrl": "https://teams/join"},
    "isOnlineMeeting": True,
}


def _url(path: str) -> re.Pattern:
    return re.compile(re.escape(f"{G}{path}") + r"(\?.*)?$")


def test_list_sends_prefer_timezone_on_every_page(httpx_mock):
    httpx_mock.add_response(
        url=_url("/me/calendarView"), json={"value": [EVENT], "@odata.nextLink": f"{G}/next"}
    )
    httpx_mock.add_response(url=f"{G}/next", json={"value": [{**EVENT, "id": "e2"}]})
    out = calendar.calendar_list(
        "2026-10-14T00:00:00", "2026-10-15T00:00:00", time_zone="Europe/Amsterdam"
    )
    assert [e["id"] for e in out] == ["e1", "e2"]
    assert out[0]["join_url"] == "https://teams/join"
    assert out[0]["attendees"] == ["bob@contoso.com"]
    reqs = httpx_mock.get_requests()
    assert all(r.headers["Prefer"] == 'outlook.timezone="Europe/Amsterdam"' for r in reqs)
    # midnight Amsterdam (CEST, UTC+2) is 22:00Z the previous day
    assert "startDateTime=2026-10-13T22%3A00%3A00Z" in str(reqs[0].url)


def test_create_online_meeting(httpx_mock):
    httpx_mock.add_response(method="POST", url=f"{G}/me/events", json=EVENT)
    calendar.calendar_create(
        "Sync",
        "2026-10-14T09:00:00",
        "2026-10-14T09:30:00",
        time_zone="Europe/Amsterdam",
        attendees=["bob@contoso.com"],
        online_meeting=True,
    )
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["isOnlineMeeting"] is True
    assert body["onlineMeetingProvider"] == "teamsForBusiness"
    assert body["start"] == {"dateTime": "2026-10-14T09:00:00", "timeZone": "Europe/Amsterdam"}
    assert body["attendees"][0] == {
        "emailAddress": {"address": "bob@contoso.com"},
        "type": "required",
    }


def test_update_sends_only_provided_fields(httpx_mock):
    httpx_mock.add_response(method="PATCH", url=f"{G}/me/events/e1", json=EVENT)
    calendar.calendar_update("e1", subject="New", location="Room 1")
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body == {"subject": "New", "location": {"displayName": "Room 1"}}


def test_update_requires_a_field():
    with pytest.raises(Broker365Error):
        calendar.calendar_update("e1")


def test_delete_quotes_id(httpx_mock):
    httpx_mock.add_response(method="DELETE", url=f"{G}/me/events/e%2F1", status_code=204)
    assert calendar.calendar_delete("e/1")["deleted"] is True


def test_free_busy(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{G}/me/calendar/getSchedule",
        json={
            "value": [
                {"scheduleId": "bob@contoso.com", "availabilityView": "0220", "scheduleItems": []}
            ]
        },
    )
    out = calendar.calendar_free_busy(
        ["bob@contoso.com"], "2026-10-14T09:00:00", "2026-10-14T11:00:00"
    )
    assert out[0]["availability_view"] == "0220"
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["schedules"] == ["bob@contoso.com"]
    assert body["availabilityViewInterval"] == 30
    assert body["endTime"] == {"dateTime": "2026-10-14T11:00:00", "timeZone": "UTC"}


def test_free_busy_personal_account_error(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{G}/me/calendar/getSchedule",
        status_code=400,
        json={"error": {"code": "ErrorInvalidRequest", "message": "no"}},
    )
    with pytest.raises(Broker365Error, match="work or school"):
        calendar.calendar_free_busy(
            ["bob@contoso.com"], "2026-10-14T09:00:00", "2026-10-14T11:00:00"
        )


def test_calendar_list_rejects_unknown_zone():
    import pytest

    from broker365.errors import Broker365Error

    with pytest.raises(Broker365Error, match="IANA"):
        calendar.calendar_list("2026-10-14T00:00:00", "2026-10-15T00:00:00", time_zone="Nowhere/X")
