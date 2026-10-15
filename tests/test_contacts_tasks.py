import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from broker365 import services
from broker365.errors import Broker365Error
from broker365.services import contacts, tasks
from tests.conftest import G

LISTS = {
    "value": [
        {"id": "L-other", "displayName": "Groceries", "wellknownListName": "none"},
        {"id": "L-def", "displayName": "Tasks", "wellknownListName": "defaultList"},
    ]
}


def _url(path: str) -> re.Pattern:
    return re.compile(re.escape(f"{G}{path}") + r"(\?.*)?$")


def _query(req) -> dict:
    return parse_qs(urlsplit(str(req.url)).query)


def test_contacts_filter_escapes_quotes(httpx_mock):
    httpx_mock.add_response(
        url=_url("/me/contacts"),
        json={
            "value": [
                {
                    "id": "c1",
                    "displayName": "O'Brien",
                    "mobilePhone": "+1",
                    "emailAddresses": [{"address": "ob@x.com"}],
                }
            ]
        },
    )
    out = contacts.contacts_list(query="O'Br")
    assert out[0] == {
        "id": "c1",
        "name": "O'Brien",
        "emails": ["ob@x.com"],
        "phones": ["+1"],
        "company": None,
    }
    assert _query(httpx_mock.get_requests()[0])["$filter"] == ["startswith(displayName,'O''Br')"]


def test_contacts_create_and_delete(httpx_mock):
    httpx_mock.add_response(method="POST", url=f"{G}/me/contacts", json={"id": "c2"})
    httpx_mock.add_response(method="DELETE", url=f"{G}/me/contacts/c2", status_code=204)
    contacts.contacts_create("Ann", emails=["ann@x.com"], phones=["+31"], company="Acme")
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["emailAddresses"] == [{"address": "ann@x.com", "name": "Ann"}]
    assert body["businessPhones"] == ["+31"]
    assert body["companyName"] == "Acme"
    assert contacts.contacts_delete("c2")["deleted"] is True
    with pytest.raises(Broker365Error):
        contacts.contacts_create("Bad", emails=["nope"])


def test_tasks_default_list_resolution(httpx_mock):
    httpx_mock.add_response(url=f"{G}/me/todo/lists?%24top=100", json=LISTS)
    httpx_mock.add_response(
        url=_url("/me/todo/lists/L-def/tasks"),
        json={
            "value": [
                {
                    "id": "t1",
                    "title": "Do it",
                    "status": "notStarted",
                    "dueDateTime": {"dateTime": "2026-10-20T00:00:00.0000000", "timeZone": "UTC"},
                }
            ]
        },
    )
    out = tasks.tasks_list()
    assert out[0]["due"] == "2026-10-20"
    assert _query(httpx_mock.get_requests()[1])["$filter"] == ["status ne 'completed'"]


def test_tasks_create_due_and_explicit_list(httpx_mock):
    httpx_mock.add_response(
        method="POST", url=f"{G}/me/todo/lists/L-other/tasks", json={"id": "t2", "title": "Milk"}
    )
    tasks.tasks_create("Milk", list_id="L-other", due="2026-10-20", importance="high")
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["dueDateTime"] == {"dateTime": "2026-10-20T00:00:00", "timeZone": "UTC"}
    assert body["importance"] == "high"
    with pytest.raises(Broker365Error):
        tasks.tasks_create("x", list_id="L", due="20 Oct")


def test_tasks_complete_and_delete(httpx_mock):
    httpx_mock.add_response(url=f"{G}/me/todo/lists?%24top=100", json=LISTS)
    httpx_mock.add_response(
        method="PATCH",
        url=f"{G}/me/todo/lists/L-def/tasks/t1",
        json={"id": "t1", "status": "completed"},
    )
    httpx_mock.add_response(
        method="DELETE", url=f"{G}/me/todo/lists/L-def/tasks/t1", status_code=204
    )
    assert tasks.tasks_complete("t1")["status"] == "completed"
    assert tasks.tasks_delete("t1", list_id="L-def")["deleted"] is True
    assert json.loads(httpx_mock.get_requests()[1].content) == {"status": "completed"}


def test_tasks_no_default_list(httpx_mock):
    httpx_mock.add_response(url=f"{G}/me/todo/lists?%24top=100", json={"value": []})
    with pytest.raises(Broker365Error, match="default"):
        tasks.tasks_list()


def test_tools_registered_with_flags():
    t = services.load_all()
    assert t["mail_send"].destructive and t["mail_reply"].destructive
    assert t["calendar_create"].destructive and t["tasks_delete"].destructive
    assert t["mail_draft"].writes and not t["mail_draft"].destructive
    assert not t["mail_list"].writes and not t["tasks_lists"].writes


@pytest.mark.parametrize("bad", ["2026-13-45", "2026-02-30"])
def test_tasks_create_rejects_impossible_dates(bad, httpx_mock):
    with pytest.raises(Broker365Error, match="due must be a date in YYYY-MM-DD format"):
        tasks.tasks_create("x", list_id="L", due=bad)
    assert httpx_mock.get_requests() == []
