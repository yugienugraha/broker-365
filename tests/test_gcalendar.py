import json

import pytest

from broker365 import audit, config, services
from broker365.errors import Broker365Error, ReadOnlyMode
from broker365.services import gcalendar
from tests.test_gdrive import at, sent

CAL = gcalendar.CALENDAR
EVENTS = f"{CAL}/calendars/primary/events"

EVENT = {
    "id": "E1",
    "summary": "Standup",
    "start": {"dateTime": "2026-10-15T09:00:00+07:00", "timeZone": "Asia/Jakarta"},
    "end": {"dateTime": "2026-10-15T09:15:00+07:00", "timeZone": "Asia/Jakarta"},
    "organizer": {"email": "me@gmail.com"},
    "attendees": [{"email": "ann@x.com"}],
    "hangoutLink": "https://meet.google.com/abc",
    "htmlLink": "https://calendar.google.com/e1",
    "status": "confirmed",
}


def test_list_converts_window_to_utc_and_expands(httpx_mock):
    httpx_mock.add_response(**at("GET", f"{EVENTS}?"), json={"items": [EVENT]})
    out = gcalendar.gcal_list(
        "2026-10-15T00:00:00", "2026-10-16T00:00:00", time_zone="Asia/Jakarta"
    )
    assert out == [
        {
            "id": "E1",
            "subject": "Standup",
            "start": "2026-10-15T09:00:00+07:00",
            "end": "2026-10-15T09:15:00+07:00",
            "time_zone": "Asia/Jakarta",
            "all_day": False,
            "location": None,
            "organizer": "me@gmail.com",
            "attendees": ["ann@x.com"],
            "is_online_meeting": True,
            "join_url": "https://meet.google.com/abc",
            "web_link": "https://calendar.google.com/e1",
            "status": "confirmed",
        }
    ]
    params = sent(httpx_mock, "GET", CAL)[0].url.params
    assert (
        params["timeMin"] == "2026-10-14T17:00:00Z" and params["timeMax"] == "2026-10-15T17:00:00Z"
    )
    assert params["singleEvents"] == "true" and params["orderBy"] == "startTime"
    assert params["timeZone"] == "Asia/Jakarta" and params["maxResults"] == "50"


def test_list_keeps_explicit_offsets_and_rejects_bad_zone(httpx_mock):
    httpx_mock.add_response(**at("GET", f"{CAL}/calendars/team%40x.com/events"), json={})
    gcalendar.gcal_list("2026-10-15T00:00:00+02:00", "2026-10-15T12:00:00Z", calendar="team@x.com")
    req = sent(httpx_mock, "GET", CAL)[0]
    assert req.url.params["timeMin"] == "2026-10-14T22:00:00Z"
    assert b"/calendars/team%40x.com/events" in req.url.raw_path
    with pytest.raises(Broker365Error, match="Unknown time zone"):
        gcalendar.gcal_list("2026-10-15T00:00:00", "2026-10-16T00:00:00", time_zone="Mars/Base")


def test_create_no_invites_by_default(httpx_mock):
    httpx_mock.add_response(**at("POST", f"{EVENTS}?"), json=EVENT)
    gcalendar.gcal_create(
        "Standup", "2026-10-15T09:00:00", "2026-10-15T09:15:00", time_zone="Asia/Jakarta",
        attendees=["ann@x.com"], location="Room 1", description="Daily",
    )  # fmt: skip
    req = sent(httpx_mock, "POST", CAL)[0]
    assert req.url.params["sendUpdates"] == "none"
    assert json.loads(req.content) == {
        "summary": "Standup",
        "start": {"dateTime": "2026-10-15T09:00:00", "timeZone": "Asia/Jakarta"},
        "end": {"dateTime": "2026-10-15T09:15:00", "timeZone": "Asia/Jakarta"},
        "attendees": [{"email": "ann@x.com"}],
        "location": "Room 1",
        "description": "Daily",
    }


def test_create_all_day_with_invites(httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{EVENTS}?"), json={**EVENT, "start": {"date": "2026-10-20"}}
    )
    out = gcalendar.gcal_create(
        "Offsite",
        "2026-10-20",
        "2026-10-20",
        all_day=True,
        attendees=["a@x.com"],
        send_updates=True,
    )
    assert out["all_day"] is True
    req = sent(httpx_mock, "POST", CAL)[0]
    assert req.url.params["sendUpdates"] == "all"
    body = json.loads(req.content)
    assert body["start"] == {"date": "2026-10-20"} and body["end"] == {"date": "2026-10-21"}


@pytest.mark.parametrize(
    "start, end, all_day, match",
    [
        ("tomorrow", "2026-10-15T10:00:00", False, "ISO 8601"),
        ("2026-10-15T09:00:00", "2026-10-15", True, "dates"),
    ],
)
def test_create_validates_times(start, end, all_day, match, httpx_mock):
    with pytest.raises(Broker365Error, match=match):
        gcalendar.gcal_create("X", start, end, all_day=all_day)


TIMED = {
    "start": {"dateTime": "2026-10-15T09:00:00+07:00", "timeZone": "Asia/Jakarta"},
    "end": {"dateTime": "2026-10-15T09:15:00+07:00", "timeZone": "Asia/Jakarta"},
}
ALL_DAY = {"start": {"date": "2026-10-20"}, "end": {"date": "2026-10-22"}}


def update(httpx_mock, current, **kwargs):
    httpx_mock.add_response(**at("GET", f"{EVENTS}/E1"), json=current)
    httpx_mock.add_response(**at("PATCH", f"{EVENTS}/E1"), json=EVENT)
    gcalendar.gcal_update("E1", **kwargs)
    assert sent(httpx_mock, "GET", CAL)[-1].url.params["fields"] == "start,end"
    return json.loads(sent(httpx_mock, "PATCH", CAL)[-1].content)


def test_update_moves_event_keeping_its_length(httpx_mock):
    body = update(
        httpx_mock, TIMED, summary="New", start="2026-10-15 10:00", time_zone="Asia/Jakarta",
        send_updates=True,
    )  # fmt: skip
    assert body == {
        "summary": "New",
        "start": {"dateTime": "2026-10-15T10:00:00", "timeZone": "Asia/Jakarta"},
        "end": {"dateTime": "2026-10-15T10:15:00", "timeZone": "Asia/Jakarta"},
    }
    assert sent(httpx_mock, "PATCH", CAL)[0].url.params["sendUpdates"] == "all"


def test_update_end_only_keeps_start_offset(httpx_mock):
    body = update(httpx_mock, TIMED, end="2026-10-15T12:00:00")
    assert body["start"]["dateTime"] == "2026-10-15T11:45:00"


def test_update_all_day_event_keeps_its_span(httpx_mock):
    body = update(httpx_mock, ALL_DAY, start="2026-11-02")
    assert body == {"start": {"date": "2026-11-02"}, "end": {"date": "2026-11-04"}}


def test_update_switch_to_all_day_nulls_times(httpx_mock):
    body = update(httpx_mock, TIMED, start="2026-10-20", end="2026-10-21", all_day=True)
    assert body == {
        "start": {"date": "2026-10-20", "dateTime": None, "timeZone": None},
        "end": {"date": "2026-10-21", "dateTime": None, "timeZone": None},
    }


def test_update_switch_to_timed_nulls_date(httpx_mock):
    body = update(
        httpx_mock, ALL_DAY, start="2026-10-20T09:00:00", end="2026-10-20T10:00:00", all_day=False
    )
    assert body["start"] == {"dateTime": "2026-10-20T09:00:00", "timeZone": "UTC", "date": None}


def test_update_switch_needs_both_ends(httpx_mock):
    httpx_mock.add_response(**at("GET", f"{EVENTS}/E1"), json=TIMED)
    with pytest.raises(Broker365Error, match="both start and end"):
        gcalendar.gcal_update("E1", start="2026-10-20", all_day=True)
    assert sent(httpx_mock, "PATCH") == []


def test_update_text_only_skips_the_read(httpx_mock):
    httpx_mock.add_response(**at("PATCH", f"{EVENTS}/E1"), json=EVENT)
    gcalendar.gcal_update("E1", location="Room 2")
    assert sent(httpx_mock, "GET") == []
    with pytest.raises(Broker365Error, match="Nothing to update"):
        gcalendar.gcal_update("E1")


def test_delete_notify_flag(httpx_mock):
    httpx_mock.add_response(**at("DELETE", f"{EVENTS}/E1"), status_code=204)
    assert gcalendar.gcal_delete("E1") == {"deleted": True, "id": "E1"}
    assert sent(httpx_mock, "DELETE", CAL)[0].url.params["sendUpdates"] == "none"
    httpx_mock.add_response(**at("DELETE", f"{EVENTS}/E1"), status_code=204)
    gcalendar.gcal_delete("E1", notify=True)
    assert sent(httpx_mock, "DELETE", CAL)[1].url.params["sendUpdates"] == "all"


def test_free_busy(httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{CAL}/freeBusy"),
        json={
            "calendars": {
                "ann@x.com": {
                    "busy": [{"start": "2026-10-15T02:00:00Z", "end": "2026-10-15T03:00:00Z"}]
                },
                "bob@x.com": {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []},
            }
        },
    )
    out = gcalendar.gcal_free_busy(
        ["ann@x.com", "bob@x.com"], "2026-10-15T08:00:00", "2026-10-15T18:00:00", "Asia/Jakarta"
    )
    assert out == [
        {
            "email": "ann@x.com",
            "busy": [
                {"start": "2026-10-15T02:00:00Z", "end": "2026-10-15T03:00:00Z", "status": "busy"}
            ],
            "error": None,
        },
        {"email": "bob@x.com", "busy": [], "error": "notFound"},
    ]
    body = json.loads(sent(httpx_mock, "POST", CAL)[0].content)
    assert body == {
        "timeMin": "2026-10-15T01:00:00Z",
        "timeMax": "2026-10-15T11:00:00Z",
        "timeZone": "Asia/Jakarta",
        "items": [{"id": "ann@x.com"}, {"id": "bob@x.com"}],
    }


def test_flags_read_only_and_audit(httpx_mock, monkeypatch):
    tools = services.load_all()
    assert tools["gcal_create"].destructive and tools["gcal_delete"].destructive
    assert tools["gcal_update"].writes and not tools["gcal_update"].destructive
    httpx_mock.add_response(**at("POST", f"{EVENTS}?"), json=EVENT)
    services.call(
        "gcal_create", summary="Private dinner", start="2026-10-15T19:00:00",
        end="2026-10-15T21:00:00", attendees=["a@x.com"], description="Secret notes",
    )  # fmt: skip
    assert audit.read(1)[0]["target"] == {"attendees_count": 1}
    log = (config.broker_home() / "audit.log").read_text(encoding="utf-8")
    assert "Private dinner" not in log and "Secret" not in log
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    visible = {t.name for t in services.available_tools()}
    assert "gcal_list" in visible and "gcal_free_busy" in visible
    assert not {"gcal_create", "gcal_update", "gcal_delete"} & visible
    with pytest.raises(ReadOnlyMode):
        services.call("gcal_delete", event_id="E1")


def test_list_uses_fields_mask_and_page_param_table(httpx_mock):
    httpx_mock.add_response(**at("GET", f"{EVENTS}?"), json={"items": []})
    gcalendar.gcal_list("2026-10-15T00:00:00", "2026-10-16T00:00:00", limit=3000)
    params = sent(httpx_mock, "GET", CAL)[0].url.params
    assert params["fields"] == gcalendar.LIST_FIELDS
    assert params["maxResults"] == "2500"  # Calendar's maximum, from gapi.PAGE_PARAMS


@pytest.mark.parametrize("bad", ["2026-13-45", "2026-02-30", "20261015"])
def test_all_day_dates_must_be_real(bad, httpx_mock):
    with pytest.raises(Broker365Error, match="All-day events need dates"):
        gcalendar.gcal_create("X", bad, bad, all_day=True)


def test_free_busy_needs_an_address(httpx_mock):
    with pytest.raises(Broker365Error, match="At least one email address is required"):
        gcalendar.gcal_free_busy([], "2026-10-15T00:00:00", "2026-10-16T00:00:00")


def test_timed_values_are_normalized_and_dates_refused(httpx_mock):
    httpx_mock.add_response(**at("POST", f"{EVENTS}?"), json=EVENT)
    gcalendar.gcal_create("X", "2026-10-15 09:00", "2026-10-15T10:30:00+07:00")
    body = json.loads(sent(httpx_mock, "POST", CAL)[0].content)
    assert body["start"]["dateTime"] == "2026-10-15T09:00:00"
    assert body["end"]["dateTime"] == "2026-10-15T10:30:00+07:00"
    with pytest.raises(Broker365Error, match="use all_day=true for dates"):
        gcalendar.gcal_create("X", "2026-10-15", "2026-10-16")
