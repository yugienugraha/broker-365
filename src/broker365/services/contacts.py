"""Outlook contacts."""

from __future__ import annotations

from typing import Any

from broker365 import graph
from broker365.services import tool
from broker365.services._shapes import address_of, compact, validate_emails

CONTACT_SELECT = "id,displayName,emailAddresses,businessPhones,mobilePhone,homePhones,companyName"


def _g() -> graph.GraphClient:
    return graph.client("contacts")


def _summarize(c: dict[str, Any]) -> dict[str, Any]:
    phones = [*(c.get("businessPhones") or []), *(c.get("homePhones") or [])]
    if c.get("mobilePhone"):
        phones.insert(0, c["mobilePhone"])
    return {
        "id": c.get("id"),
        "name": c.get("displayName"),
        "emails": [address_of(e) for e in c.get("emailAddresses") or []],
        "phones": phones,
        "company": c.get("companyName"),
    }


@tool("contacts")
def contacts_list(limit: int = 50, query: str | None = None) -> list[dict[str, Any]]:
    """List contacts sorted by name; `query` keeps only names starting with that text."""
    params = {"$select": CONTACT_SELECT}
    if query:
        params["$filter"] = f"startswith(displayName,'{graph.odata_str(query)}')"
    else:
        params["$orderby"] = "displayName"
    return [_summarize(c) for c in _g().paged("/me/contacts", params=params, limit=limit)]


@tool("contacts", writes=True)
def contacts_create(
    display_name: str,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    company: str | None = None,
) -> dict[str, Any]:
    """Create a contact with optional email addresses, phone numbers and company."""
    addresses = [{"address": a, "name": display_name} for a in validate_emails(emails)]
    contact = {
        "displayName": display_name,
        **compact(
            {
                "emailAddresses": addresses or None,
                "businessPhones": phones or None,
                "companyName": company or None,
            }
        ),
    }
    return _summarize(_g().post("/me/contacts", json=contact))


@tool("contacts", destructive=True)
def contacts_delete(contact_id: str) -> dict[str, Any]:
    """Delete a contact."""
    _g().delete(f"/me/contacts/{graph.seg(contact_id, 'contact id')}")
    return {"deleted": True, "id": contact_id}
