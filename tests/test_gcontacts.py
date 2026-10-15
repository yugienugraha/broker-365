import json

import pytest

from broker365 import audit, services
from broker365.errors import Broker365Error
from broker365.providers import google
from broker365.services import gcontacts
from tests.test_gdrive import at, sent

PEOPLE = gcontacts.PEOPLE
PERSON = {
    "resourceName": "people/c1",
    "names": [{"displayName": "Ann Lee"}],
    "emailAddresses": [{"value": "ann@x.com"}],
    "phoneNumbers": [{"value": "+62 812"}],
    "organizations": [{"name": "Acme"}],
}
SUMMARY = {
    "id": "people/c1",
    "name": "Ann Lee",
    "emails": ["ann@x.com"],
    "phones": ["+62 812"],
    "company": "Acme",
}


@pytest.fixture(autouse=True)
def cold_cache(monkeypatch):
    account = {"email": "pat@gmail.com"}
    monkeypatch.setattr(gcontacts, "_warmed", set())
    monkeypatch.setattr(google, "account_email", lambda username=None: account["email"])
    return account


def test_list(httpx_mock):
    httpx_mock.add_response(
        **at("GET", f"{PEOPLE}/people/me/connections"), json={"connections": [PERSON]}
    )
    assert gcontacts.gcontacts_list(limit=10) == [SUMMARY]
    params = sent(httpx_mock, "GET", PEOPLE)[0].url.params
    assert params["personFields"] == gcontacts.FIELDS and params["pageSize"] == "10"


def test_search_warms_up_once(httpx_mock):
    url = f"{PEOPLE}/people:searchContacts"
    httpx_mock.add_response(**at("GET", url), json={})
    httpx_mock.add_response(**at("GET", url), json={"results": [{"person": PERSON}]})
    httpx_mock.add_response(**at("GET", url), json={"results": []})
    assert gcontacts.gcontacts_search("ann", limit=50) == [SUMMARY]
    assert gcontacts.gcontacts_search("bob") == []
    queries = [r.url.params["query"] for r in sent(httpx_mock, "GET", PEOPLE)]
    assert queries == ["", "ann", "bob"]  # one warm-up per process
    assert sent(httpx_mock, "GET", PEOPLE)[1].url.params["pageSize"] == "30"
    assert sent(httpx_mock, "GET", PEOPLE)[1].url.params["readMask"] == gcontacts.FIELDS


def test_create(httpx_mock):
    httpx_mock.add_response(**at("POST", f"{PEOPLE}/people:createContact"), json=PERSON)
    assert gcontacts.gcontacts_create("Ann Lee", "ann@x.com", "+62 812", "Acme") == SUMMARY
    req = sent(httpx_mock, "POST", PEOPLE)[0]
    assert json.loads(req.content) == {
        "names": [{"unstructuredName": "Ann Lee"}],
        "emailAddresses": [{"value": "ann@x.com"}],
        "phoneNumbers": [{"value": "+62 812"}],
        "organizations": [{"name": "Acme"}],
    }
    assert req.url.params["personFields"] == gcontacts.FIELDS


def test_create_validates(httpx_mock):
    with pytest.raises(Broker365Error, match="Invalid email"):
        gcontacts.gcontacts_create("Ann", email="not-an-email")
    with pytest.raises(Broker365Error, match="name is required"):
        gcontacts.gcontacts_create(" ")
    assert httpx_mock.get_requests() == []


def test_read_only_and_audit(httpx_mock, monkeypatch):
    httpx_mock.add_response(**at("POST", f"{PEOPLE}/people:createContact"), json=PERSON)
    services.call("gcontacts_create", name="Ann Lee", email="ann@x.com")
    assert audit.read(1)[0]["target"] == {"name": "Ann Lee"}
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    visible = {t.name for t in services.available_tools()}
    assert "gcontacts_search" in visible and "gcontacts_create" not in visible


def test_warm_up_is_per_account(httpx_mock, cold_cache):
    url = f"{PEOPLE}/people:searchContacts"
    httpx_mock.add_response(**at("GET", url), json={}, is_reusable=True)
    gcontacts.gcontacts_search("a")
    gcontacts.gcontacts_search("b")
    cold_cache["email"] = "Lee@corp.example"
    gcontacts.gcontacts_search("c")
    queries = [r.url.params["query"] for r in sent(httpx_mock, "GET", PEOPLE)]
    assert queries == ["", "a", "b", "", "c"]
    assert gcontacts._warmed == {"pat@gmail.com", "lee@corp.example"}
