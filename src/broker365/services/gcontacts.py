"""Google Contacts (People v1) for a connected Google account: the gcontacts_* tools."""

from __future__ import annotations

import threading
from typing import Any

from broker365 import config, gapi
from broker365.errors import Broker365Error
from broker365.providers import google as google_provider
from broker365.services import tool
from broker365.services._shapes import compact, validate_emails

BUNDLE = "gcontacts"
GOOGLE = config.GOOGLE
PEOPLE = "https://people.googleapis.com/v1"
FIELDS = "names,emailAddresses,phoneNumbers,organizations"
SEARCH_MAX = 30  # people:searchContacts' largest pageSize
# Accounts (lowercase email) whose searchContacts cache was warmed with the empty query
# Google asks for before the first real search.
_warmed: set[str] = set()
_warm_lock = threading.Lock()


def _g() -> gapi.GoogleClient:
    return gapi.client(BUNDLE)


def _first(person: dict[str, Any], key: str, field: str) -> str | None:
    items = person.get(key) or []
    return items[0].get(field) if items else None


def _summarize(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p.get("resourceName"),
        "name": _first(p, "names", "displayName"),
        "emails": [e.get("value") for e in p.get("emailAddresses") or [] if e.get("value")],
        "phones": [n.get("value") for n in p.get("phoneNumbers") or [] if n.get("value")],
        "company": _first(p, "organizations", "name"),
    }


def _search(query: str, limit: int) -> dict[str, Any]:
    params = {"query": query, "readMask": FIELDS, "pageSize": max(1, min(limit, SEARCH_MAX))}
    return _g().get(f"{PEOPLE}/people:searchContacts", params=params) or {}


@tool(BUNDLE, GOOGLE)
def gcontacts_list(limit: int = 100) -> list[dict[str, Any]]:
    """List Google contacts sorted by first name."""
    params = {"personFields": FIELDS, "sortOrder": "FIRST_NAME_ASCENDING"}
    people = _g().paged(f"{PEOPLE}/people/me/connections", params, "connections", limit)
    return [_summarize(p) for p in people]


@tool(BUNDLE, GOOGLE)
def gcontacts_search(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search Google contacts by name, email, phone or company (prefix match); at most 30."""
    if not query.strip():
        raise Broker365Error("A search query is required")
    account = google_provider.account_email().lower()
    with _warm_lock:
        cold = account not in _warmed
        _warmed.add(account)
    if cold:
        _search("", 1)  # Google: warm the search cache with an empty query first
    return [_summarize(r.get("person") or {}) for r in _search(query, limit).get("results") or []]


@tool(BUNDLE, GOOGLE, writes=True)
def gcontacts_create(
    name: str, email: str | None = None, phone: str | None = None, company: str | None = None
) -> dict[str, Any]:
    """Create a Google contact with an optional email address, phone number and company."""
    if not name.strip():
        raise Broker365Error("A contact name is required")
    person = compact(
        {
            "names": [{"unstructuredName": name}],
            "emailAddresses": [{"value": validate_emails([email])[0]}] if email else None,
            "phoneNumbers": [{"value": phone}] if phone else None,
            "organizations": [{"name": company}] if company else None,
        }
    )
    out = _g().post(f"{PEOPLE}/people:createContact", json=person, params={"personFields": FIELDS})
    return _summarize(out)
