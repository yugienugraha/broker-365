"""Google Slides (Slides v1) for a connected Google account: the gslides_* tools.

Presentations are addressed like Drive files (id or absolute path). Slides are built from
predefined layouts with their title/body placeholders filled in; writes can be guarded with
the `revisionId` from gslides_read.
"""

from __future__ import annotations

from typing import Any, Literal

from broker365 import config
from broker365.errors import Broker365Error
from broker365.services import _gnative, gdrive, tool

BUNDLE = "gslides"
GOOGLE = config.GOOGLE
SLIDES = "https://slides.googleapis.com/v1/presentations"
DECK = _gnative.Native(
    BUNDLE,
    SLIDES,
    gdrive.SLIDES,
    "Google Slides presentation",
    "the ppt_* tools for .pptx files in OneDrive",
)
TITLE_TYPES = {"TITLE", "CENTERED_TITLE"}
BODY_TYPES = {"BODY", "SUBTITLE"}
READ_FIELDS = (
    "presentationId,title,revisionId,slides(objectId,pageElements(objectId,"
    "shape(placeholder(type),text(textElements(textRun(content))))))"
)


def _slide_spec(spec: Any) -> tuple[str, str | None, list[str] | None]:
    if not isinstance(spec, dict):
        raise Broker365Error('Each slide must be an object like {"title": ..., "bullets": [...]}')
    title, body, bullets = spec.get("title") or "", spec.get("body"), spec.get("bullets")
    if bullets is not None and (
        not isinstance(bullets, list) or not all(isinstance(b, str) for b in bullets)
    ):
        raise Broker365Error("bullets must be a list of strings")
    if body is not None and not isinstance(body, str):
        raise Broker365Error("body must be a string")
    return str(title), body, bullets


def slide_requests(
    title: str,
    body: str | None = None,
    bullets: list[str] | None = None,
    index: int | None = None,
    first: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """(new slide's objectId, requests creating it with its placeholders filled). Layout:
    TITLE_AND_BODY with a body; without one TITLE (centered) for a deck's first slide, else
    TITLE_ONLY."""
    if body and bullets:
        raise Broker365Error("Give either body or bullets for a slide, not both")
    text = "\n".join(b.strip() for b in bullets) if bullets else (body or "")
    slide_id, title_id, body_id = (_gnative.new_id(p) for p in ("slide", "title", "body"))
    if text:
        layout, title_type = "TITLE_AND_BODY", "TITLE"
    elif first:
        layout, title_type = "TITLE", "CENTERED_TITLE"
    else:
        layout, title_type = "TITLE_ONLY", "TITLE"
    mappings = [{"layoutPlaceholder": {"type": title_type, "index": 0}, "objectId": title_id}]
    if text:
        mappings.append({"layoutPlaceholder": {"type": "BODY", "index": 0}, "objectId": body_id})
    create: dict[str, Any] = {
        "objectId": slide_id,
        "slideLayoutReference": {"predefinedLayout": layout},
        "placeholderIdMappings": mappings,
    }
    if index is not None:
        create["insertionIndex"] = index
    requests: list[dict[str, Any]] = [{"createSlide": create}]
    if title:
        requests.append({"insertText": {"objectId": title_id, "insertionIndex": 0, "text": title}})
    if text:
        requests.append({"insertText": {"objectId": body_id, "insertionIndex": 0, "text": text}})
    if bullets:
        requests.append(
            {
                "createParagraphBullets": {
                    "objectId": body_id,
                    "textRange": {"type": "ALL"},
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            }
        )
    return slide_id, requests


def _shape_text(shape: dict[str, Any]) -> str:
    return _gnative.run_text((shape.get("text") or {}).get("textElements")).rstrip("\n")


# ---- tools ------------------------------------------------------------------------------


@tool(BUNDLE, GOOGLE, writes=True, audit=("title", "remote_folder"), requires=("gdrive",))
def gslides_create(
    title: str,
    slides: list[dict[str, Any]] | None = None,
    remote_folder: str = "/",
    conflict: Literal["fail", "rename"] = "fail",
) -> dict[str, Any]:
    """Create a Google Slides deck called `title` in a Drive folder (path or id; '/' = My
    Drive). `slides`: [{"title": "...", "body": "..."} or {"title": "...", "bullets": [...]}];
    a first slide without body becomes a title slide. conflict: 'fail' or 'rename'."""
    specs = [_slide_spec(s) for s in slides or []]
    # services.call has checked gslides and gdrive consent: no empty deck is left behind.
    meta = gdrive.create(gdrive.folder_ref(remote_folder), title, gdrive.SLIDES, conflict)
    out: dict[str, Any] = {**meta, "slides": []}
    if not specs:
        return out
    blank = DECK.client().get(DECK.url(meta["id"]), params={"fields": "slides.objectId"}) or {}
    requests: list[dict[str, Any]] = []
    for i, (t, body, bullets) in enumerate(specs):
        slide_id, reqs = slide_requests(t, body, bullets, first=i == 0)
        out["slides"].append(slide_id)
        requests += reqs
    requests += [{"deleteObject": {"objectId": s["objectId"]}} for s in blank.get("slides") or []]
    DECK.update(meta["id"], requests)
    return out


@tool(BUNDLE, GOOGLE)
def gslides_read(item: str) -> dict[str, Any]:
    """Read a Google Slides deck (id or path): per slide its objectId, index, title, body
    texts and every text shape. Returns revisionId for guarded writes."""
    meta = DECK.resolve(item)
    pres = DECK.client().get(DECK.url(meta["id"]), params={"fields": READ_FIELDS})
    slides = []
    for i, slide in enumerate(pres.get("slides") or []):
        shapes, title, body = [], None, []
        for el in slide.get("pageElements") or []:
            shape = el.get("shape")
            if not shape:
                continue
            kind = (shape.get("placeholder") or {}).get("type")
            text = _shape_text(shape)
            shapes.append({"objectId": el.get("objectId"), "placeholder": kind, "text": text})
            if kind in TITLE_TYPES and title is None:
                title = text
            elif kind in BODY_TYPES and text:
                body.append(text)
        slides.append(
            {
                "objectId": slide.get("objectId"),
                "index": i,
                "title": title,
                "body": body,
                "shapes": shapes,
            }
        )
    return {
        "id": pres.get("presentationId") or meta["id"],
        "title": pres.get("title"),
        "revisionId": pres.get("revisionId"),
        "slides": slides,
    }


@tool(BUNDLE, GOOGLE, writes=True, audit=("index",))
def gslides_add_slide(
    item: str,
    title: str,
    body: str | None = None,
    bullets: list[str] | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    """Add a slide with a title and either `body` text or `bullets` to a Google Slides deck,
    at `index` (0-based; default: at the end)."""
    if index is not None and index < 0:
        raise Broker365Error("index must be 0 or more")
    meta = DECK.resolve(item)
    slide_id, requests = slide_requests(title, body, bullets, index=index)
    _, revision = DECK.update(meta["id"], requests)
    return {"id": meta["id"], "slideId": slide_id, "revisionId": revision}


@tool(BUNDLE, GOOGLE, writes=True)
def gslides_replace_text(
    item: str,
    find: str,
    replace: str,
    match_case: bool = True,
    revision_id: str | None = None,
) -> dict[str, Any]:
    """Replace every occurrence of `find` on all slides. Pass `revision_id` (from
    gslides_read) to fail with a conflict instead of editing a deck changed since."""
    if not find:
        raise Broker365Error("`find` must not be empty")
    meta = DECK.resolve(item)
    counts, revision = DECK.replace_all(meta["id"], [(find, replace)], match_case, revision_id)
    return {"id": meta["id"], "occurrencesChanged": counts[0], "revisionId": revision}
