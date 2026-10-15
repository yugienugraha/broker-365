import json
import re

import pytest

from broker365 import config, gapi, services
from broker365.errors import Broker365Error, ConflictError, ConsentRequired, ReadOnlyMode
from broker365.services import gdrive, gslides
from tests.test_gdrive import FakeDrive, at, item, sent

SLIDES = gslides.SLIDES
DRIVE = gapi.DRIVE


@pytest.fixture
def drive(httpx_mock):
    d = FakeDrive(httpx_mock)
    d.add(
        item("D1", "Decks", gdrive.FOLDER),
        item("P1", "Pitch", gdrive.SLIDES, parent="D1"),
        item("DOC", "Plan", gdrive.DOC),
    )
    return d


def test_create_flow_maps_placeholders_and_drops_blank_slide(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{DRIVE}/files"), json=item("NEW", "Q4", gdrive.SLIDES, parent="D1")
    )
    httpx_mock.add_response(**at("GET", f"{SLIDES}/NEW"), json={"slides": [{"objectId": "p"}]})
    httpx_mock.add_response(**at("POST", f"{SLIDES}/NEW:batchUpdate"), json={})
    out = gslides.gslides_create(
        "Q4",
        [
            {"title": "Q4 review"},
            {"title": "Numbers", "bullets": ["Revenue up", "Costs down"]},
            {"title": "Next", "body": "Ship G5"},
        ],
        remote_folder="/Decks",
    )
    assert out["id"] == "NEW" and len(out["slides"]) == 3
    assert json.loads(sent(httpx_mock, "POST", f"{DRIVE}/files")[0].content) == {
        "name": "Q4",
        "mimeType": gdrive.SLIDES,
        "parents": ["D1"],
    }
    assert sent(httpx_mock, "GET", SLIDES)[0].url.params["fields"] == "slides.objectId"
    reqs = json.loads(sent(httpx_mock, "POST", SLIDES)[0].content)["requests"]
    creates = [r["createSlide"] for r in reqs if "createSlide" in r]
    assert [c["slideLayoutReference"]["predefinedLayout"] for c in creates] == [
        "TITLE",
        "TITLE_AND_BODY",
        "TITLE_AND_BODY",
    ]
    first, second, _ = creates
    assert [m["layoutPlaceholder"] for m in first["placeholderIdMappings"]] == [
        {"type": "CENTERED_TITLE", "index": 0}
    ]
    title_id, body_id = (m["objectId"] for m in second["placeholderIdMappings"])
    assert {
        "insertText": {"objectId": body_id, "insertionIndex": 0, "text": "Revenue up\nCosts down"}
    } in reqs
    assert {"insertText": {"objectId": title_id, "insertionIndex": 0, "text": "Numbers"}} in reqs
    bullets = [r for r in reqs if "createParagraphBullets" in r]
    assert bullets == [
        {
            "createParagraphBullets": {
                "objectId": body_id,
                "textRange": {"type": "ALL"},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
            }
        }
    ]
    assert reqs[-1] == {"deleteObject": {"objectId": "p"}}
    ids = [c["objectId"] for c in creates] + [title_id, body_id]
    assert all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_:-]{4,49}", i) for i in ids)


def test_create_without_slides_keeps_default(drive, httpx_mock):
    httpx_mock.add_response(**at("POST", f"{DRIVE}/files"), json=item("E", "Empty", gdrive.SLIDES))
    out = gslides.gslides_create("Empty")
    assert out["slides"] == [] and sent(httpx_mock, "POST", SLIDES) == []


def test_create_checks_slides_consent_first(drive, httpx_mock, monkeypatch):
    from broker365.providers import google

    monkeypatch.setattr(
        google.GoogleProvider, "has_consent", lambda self, b, u=None: b != "gslides"
    )
    with pytest.raises(ConsentRequired, match="--scopes gslides"):
        services.call("gslides_create", title="Q4", slides=[{"title": "x"}])
    assert httpx_mock.get_requests() == []  # no empty deck left behind
    assert services.load_all()["gslides_create"].requires == ("gdrive",)


def test_body_and_bullets_together_are_refused(drive):
    with pytest.raises(Broker365Error, match="either body or bullets"):
        gslides.gslides_add_slide("P1", "T", body="x", bullets=["y"])


def test_read_detects_title_and_body(drive, httpx_mock):
    def shape(oid, kind, text):
        sh = {"text": {"textElements": [{"textRun": {"content": text}}]}}
        if kind:
            sh["placeholder"] = {"type": kind}
        return {"objectId": oid, "shape": sh}

    pres = {
        "presentationId": "P1",
        "title": "Pitch",
        "revisionId": "r1",
        "slides": [
            {"objectId": "s1", "pageElements": [shape("t", "CENTERED_TITLE", "Hello\n")]},
            {
                "objectId": "s2",
                "pageElements": [
                    shape("t2", "TITLE", "Agenda\n"),
                    shape("b2", "BODY", "One\nTwo\n"),
                    shape("x", None, "Note\n"),
                    {"objectId": "img", "image": {}},
                ],
            },
        ],
    }
    httpx_mock.add_response(**at("GET", f"{SLIDES}/P1"), json=pres)
    out = gslides.gslides_read("/Decks/Pitch")
    assert out["revisionId"] == "r1"
    assert [(s["index"], s["title"], s["body"]) for s in out["slides"]] == [
        (0, "Hello", []),
        (1, "Agenda", ["One\nTwo"]),
    ]
    assert out["slides"][1]["shapes"][2] == {"objectId": "x", "placeholder": None, "text": "Note"}
    assert sent(httpx_mock, "GET", SLIDES)[0].url.params["fields"] == gslides.READ_FIELDS


def test_add_slide_at_index(drive, httpx_mock):
    httpx_mock.add_response(**at("POST", f"{SLIDES}/P1:batchUpdate"), json={})
    out = gslides.gslides_add_slide("P1", "Closing", index=2)
    reqs = json.loads(sent(httpx_mock, "POST", SLIDES)[0].content)["requests"]
    create = reqs[0]["createSlide"]
    assert create["insertionIndex"] == 2 and create["objectId"] == out["slideId"]
    assert create["slideLayoutReference"] == {"predefinedLayout": "TITLE_ONLY"}
    assert len(reqs) == 2  # no body text, no bullets


def test_non_presentation_is_refused(drive, httpx_mock):
    with pytest.raises(Broker365Error, match="not a Google Slides presentation.*ppt_"):
        gslides.gslides_read("/Plan")
    httpx_mock.add_response(**at("GET", f"{SLIDES}/DOC"), status_code=404, json={"error": {}})
    with pytest.raises(Broker365Error, match="not a Google Slides presentation.*ppt_"):
        gslides.gslides_read("DOC")


def test_replace_text_conflict(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{SLIDES}/P1:batchUpdate"),
        status_code=400,
        json={
            "error": {
                "code": 400,
                "message": "The required revision ID 'r0' does not match the latest revision.",
                "status": "INVALID_ARGUMENT",
            }
        },
    )
    with pytest.raises(ConflictError, match="presentation changed since revision r0"):
        gslides.gslides_replace_text("P1", "a", "b", revision_id="r0")
    body = json.loads(sent(httpx_mock, "POST", SLIDES)[0].content)
    assert body["writeControl"] == {"requiredRevisionId": "r0"}


def test_replace_text_counts(drive, httpx_mock):
    httpx_mock.add_response(
        **at("POST", f"{SLIDES}/P1:batchUpdate"),
        json={"replies": [{"replaceAllText": {"occurrencesChanged": 4}}]},
    )
    assert gslides.gslides_replace_text("P1", "2025", "2026")["occurrencesChanged"] == 4


def test_read_only_and_audit(drive, httpx_mock, monkeypatch):
    httpx_mock.add_response(**at("POST", f"{SLIDES}/P1:batchUpdate"), json={})
    services.call("gslides_replace_text", item="P1", find="secret-a", replace="secret-b")
    log = (config.broker_home() / "audit.log").read_text(encoding="utf-8")
    assert json.loads(log.splitlines()[-1])["target"] == {"item": "P1"}
    assert "secret" not in log
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    visible = {t.name for t in services.available_tools()}
    assert "gslides_read" in visible and "gslides_add_slide" not in visible
    with pytest.raises(ReadOnlyMode):
        services.call("gslides_add_slide", item="P1", title="x")
