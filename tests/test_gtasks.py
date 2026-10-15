import json

import pytest

from broker365 import services
from broker365.errors import Broker365Error, ReadOnlyMode
from broker365.services import gtasks
from tests.test_gdrive import at, sent

TASKS = gtasks.TASKS
DEFAULT = f"{TASKS}/lists/@default/tasks"
TASK = {
    "id": "T1",
    "title": "Pay rent",
    "status": "needsAction",
    "due": "2026-10-31T00:00:00.000Z",
    "notes": "via bank",
    "updated": "2026-10-15T00:00:00.000Z",
}


def test_lists(httpx_mock):
    httpx_mock.add_response(
        **at("GET", f"{TASKS}/users/@me/lists"), json={"items": [{"id": "L1", "title": "My Tasks"}]}
    )
    assert gtasks.gtasks_lists() == [{"id": "L1", "name": "My Tasks", "updated": None}]
    assert sent(httpx_mock, "GET", TASKS)[0].url.params["maxResults"] == "100"


def test_list_open_tasks_of_default_list(httpx_mock):
    httpx_mock.add_response(**at("GET", DEFAULT), json={"items": [TASK]})
    out = gtasks.gtasks_list()
    assert out[0] | {} == {
        "id": "T1",
        "title": "Pay rent",
        "status": "notStarted",
        "due": "2026-10-31",
        "body": "via bank",
        "updated": "2026-10-15T00:00:00.000Z",
        "completed": None,
        "web_link": None,
    }
    params = sent(httpx_mock, "GET", TASKS)[0].url.params
    assert params["showCompleted"] == "false" and params["maxResults"] == "100"


def test_list_with_completed_and_other_list(httpx_mock):
    httpx_mock.add_response(**at("GET", f"{TASKS}/lists/L%2F1/tasks"), json={"items": []})
    gtasks.gtasks_list("L/1", include_completed=True, limit=5)
    params = sent(httpx_mock, "GET", TASKS)[0].url.params
    assert params["showCompleted"] == "true" and params["showHidden"] == "true"
    assert params["maxResults"] == "5"


def test_create(httpx_mock):
    httpx_mock.add_response(**at("POST", DEFAULT), json=TASK)
    gtasks.gtasks_create("Pay rent", notes="via bank", due="2026-10-31")
    assert json.loads(sent(httpx_mock, "POST", TASKS)[0].content) == {
        "title": "Pay rent",
        "due": "2026-10-31T00:00:00.000Z",
        "notes": "via bank",
    }
    for bad in ("next week", "2026-13-45", "2026-02-30"):
        with pytest.raises(Broker365Error, match="due must be a date in YYYY-MM-DD format"):
            gtasks.gtasks_create("x", due=bad)


def test_complete(httpx_mock):
    httpx_mock.add_response(
        **at("PATCH", f"{DEFAULT}/T1"),
        json={**TASK, "status": "completed", "completed": "2026-10-15T10:00:00.000Z"},
    )
    out = gtasks.gtasks_complete("T1")
    assert out["status"] == "completed" and out["completed"].startswith("2026-10-15")
    assert json.loads(sent(httpx_mock, "PATCH", TASKS)[0].content) == {"status": "completed"}


def test_read_only(monkeypatch):
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    visible = {t.name for t in services.available_tools()}
    assert {"gtasks_lists", "gtasks_list"} <= visible
    assert not {"gtasks_create", "gtasks_complete"} & visible
    with pytest.raises(ReadOnlyMode):
        services.call("gtasks_complete", task_id="T1")
