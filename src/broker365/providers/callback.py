"""The static page a browser lands on after an OAuth redirect, shared by the CLI's loopback
listener and the dashboard's callback route: no script, the message HTML-escaped, and headers
that forbid everything (the page needs nothing but its own markup)."""

from __future__ import annotations

import html

HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def page(message: str, home_link: bool = False) -> str:
    """The page with `message` escaped; `home_link` adds a plain link to "/" (the dashboard)."""
    link = '<p><a href="/">Return to the broker365 dashboard</a></p>' if home_link else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>broker365</title></head>"
        f"<body><main><p>{html.escape(message)}</p>{link}</main></body></html>"
    )
