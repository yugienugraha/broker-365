import json
import re

import httpx
import pytest

from broker365 import gapi, transport
from broker365.errors import (
    ApiError,
    Broker365Error,
    ConflictError,
    ConsentRequired,
    GoogleApiError,
    NetworkError,
    ReauthRequired,
)
from broker365.providers import google

DRIVE = gapi.DRIVE
FILE_URL = f"{DRIVE}/files/F1"
REAL_TOKEN = gapi.GoogleClient._token  # conftest stubs it; here the provider path runs


@pytest.fixture(autouse=True)
def gtoken(monkeypatch):
    """Tokens from the Google provider, counted; records invalidate_token calls."""
    state = {"n": 0, "forgot": []}

    def get_token(self, bundle, username=None):
        state["n"] += 1
        return f"g-token-{state['n']}"

    monkeypatch.setattr(gapi.GoogleClient, "_token", REAL_TOKEN)
    monkeypatch.setattr(google.GoogleProvider, "get_token", get_token)
    monkeypatch.setattr(
        google.GoogleProvider, "invalidate_token", lambda self, u=None: state["forgot"].append(u)
    )
    return state


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(transport, "_sleep", waited.append)
    return waited


def g():
    return gapi.client("gdrive")


def err(code, reason, message="nope", status=None):
    body = {"error": {"code": code, "message": message, "errors": [{"reason": reason}]}}
    if status:
        body["error"]["status"] = status
    return body


# ---- host allow-list --------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://www.googleapis.com/drive/v3/files",
        "https://www.googleapis.com.evil.com/drive/v3/files",
        "https://evil.com/www.googleapis.com",
        "https://googleapis.com/drive/v3/files",
        "https://storage.googleapis.com/x",
        "https://user@www.googleapis.com/drive/v3/files",
        "https://www.googleapis.com:8443/drive/v3/files",
        "https://doc-0s-1.googleusercontent.com/x",
        "ftp://www.googleapis.com/x",
    ],
)
def test_token_never_sent_outside_allow_list(url, httpx_mock):
    with pytest.raises(Broker365Error, match="non-Google-API"):
        g().get(url)
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize("host", sorted(gapi.API_HOSTS))
def test_allow_listed_hosts_get_the_token(host, httpx_mock):
    httpx_mock.add_response(url=f"https://{host}/v1/x", json={"ok": True})
    assert g().get(f"https://{host}/v1/x") == {"ok": True}
    assert httpx_mock.get_requests()[0].headers["Authorization"] == "Bearer g-token-1"


def test_relative_path_goes_to_www(httpx_mock):
    httpx_mock.add_response(url=f"{DRIVE}/about", json={})
    g().get("/drive/v3/about")


def test_redirect_not_followed_with_token(httpx_mock):
    httpx_mock.add_response(
        url=FILE_URL, status_code=302, headers={"Location": "https://x.example/"}
    )
    resp = g().request("GET", FILE_URL)
    assert resp.status_code == 302
    assert len(httpx_mock.get_requests()) == 1


def test_download_redirect_fetched_without_token(httpx_mock):
    httpx_mock.add_response(
        url=f"{FILE_URL}?alt=media",
        status_code=302,
        headers={"Location": "https://doc-0s.googleusercontent.com/dl"},
    )
    httpx_mock.add_response(url="https://doc-0s.googleusercontent.com/dl", content=b"bytes")
    assert g().download(FILE_URL, {"alt": "media"}) == (b"bytes", False)
    assert "Authorization" not in httpx_mock.get_requests()[-1].headers


def test_download_refuses_http_redirect(httpx_mock):
    httpx_mock.add_response(
        url=f"{FILE_URL}?alt=media", status_code=302, headers={"Location": "http://plain.example/"}
    )
    with pytest.raises(Broker365Error, match="non-https"):
        g().download(FILE_URL, {"alt": "media"})


def test_download_cap_truncates_or_raises(httpx_mock):
    httpx_mock.add_response(url=FILE_URL, content=b"0123456789")
    assert g().download(FILE_URL, max_bytes=4, truncate=True) == (b"0123", True)
    httpx_mock.add_response(url=FILE_URL, content=b"0123456789")
    with pytest.raises(Broker365Error, match="larger than 4 bytes"):
        g().download(FILE_URL, max_bytes=4)


# ---- retries ----------------------------------------------------------------------------


def test_429_retried_for_post_with_retry_after(httpx_mock, sleeps):
    url = f"{DRIVE}/files"
    httpx_mock.add_response(method="POST", url=url, status_code=429, headers={"Retry-After": "3"})
    httpx_mock.add_response(method="POST", url=url, json={"id": "new"})
    assert g().post(url, json={})["id"] == "new"
    assert sleeps == [3.0]


def test_403_rate_limit_retried_with_backoff(httpx_mock, sleeps):
    url = f"{DRIVE}/files"
    for _ in range(2):
        httpx_mock.add_response(
            method="POST", url=url, status_code=403, json=err(403, "userRateLimitExceeded")
        )
    httpx_mock.add_response(method="POST", url=url, json={"id": "new"})
    assert g().post(url, json={})["id"] == "new"
    assert sleeps == [1.0, 2.0]


def test_403_other_reason_not_retried(httpx_mock):
    httpx_mock.add_response(url=FILE_URL, status_code=403, json=err(403, "forbidden", "no"))
    with pytest.raises(GoogleApiError) as exc:
        g().get(FILE_URL)
    assert exc.value.status == 403 and exc.value.code == "forbidden"
    assert str(exc.value) == "Google 403 forbidden: no"


def test_503_retried_for_get_not_post(httpx_mock):
    httpx_mock.add_response(url=FILE_URL, status_code=503)
    httpx_mock.add_response(url=FILE_URL, json={"id": "F1"})
    assert g().get(FILE_URL)["id"] == "F1"
    httpx_mock.add_response(method="POST", url=f"{DRIVE}/files", status_code=503)
    with pytest.raises(GoogleApiError):
        g().post(f"{DRIVE}/files", json={})
    assert sum(r.method == "POST" for r in httpx_mock.get_requests()) == 1


def test_idempotent_post_retried_on_500(httpx_mock):
    url = f"{DRIVE}/files"
    httpx_mock.add_response(method="POST", url=url, status_code=500)
    httpx_mock.add_response(method="POST", url=url, json={"ok": 1})
    assert g().post(url, json={}, idempotent=True) == {"ok": 1}


def test_retries_give_up_after_max(httpx_mock):
    for _ in range(gapi.MAX_RETRIES + 1):
        httpx_mock.add_response(url=FILE_URL, status_code=429)
    with pytest.raises(GoogleApiError):
        g().get(FILE_URL)
    assert len(httpx_mock.get_requests()) == gapi.MAX_RETRIES + 1


def test_total_wait_cap(httpx_mock, monkeypatch):
    now = [0.0]

    def monotonic():  # every reading is 1000 s after the previous one
        now[0] += 1000.0
        return now[0]

    monkeypatch.setattr(gapi.time, "monotonic", monotonic)
    httpx_mock.add_response(url=FILE_URL, status_code=429)
    with pytest.raises(GoogleApiError):
        g().get(FILE_URL)
    assert len(httpx_mock.get_requests()) == 1


def test_network_error(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectTimeout("boom"))
    with pytest.raises(NetworkError, match="Network error talking to Google: ConnectTimeout"):
        g().get(FILE_URL)


# ---- auth errors ------------------------------------------------------------------------


def test_401_refreshes_once(httpx_mock, gtoken):
    httpx_mock.add_response(url=FILE_URL, status_code=401, json=err(401, "authError"))
    httpx_mock.add_response(url=FILE_URL, json={"id": "F1"})
    assert g().get(FILE_URL)["id"] == "F1"
    auths = [r.headers["Authorization"] for r in httpx_mock.get_requests()]
    assert auths == ["Bearer g-token-1", "Bearer g-token-2"]
    assert gtoken["forgot"] == [None]


def test_401_twice_is_reauth(httpx_mock, gtoken):
    for _ in range(2):
        httpx_mock.add_response(url=FILE_URL, status_code=401, json=err(401, "authError"))
    with pytest.raises(ReauthRequired, match="--provider google"):
        g().get(FILE_URL)
    assert len(httpx_mock.get_requests()) == 2


def test_google_forget_token_drops_memo(monkeypatch):
    monkeypatch.setattr(google, "_account", lambda u=None: {"email": "Pat@Gmail.com"})
    google._tokens["pat@gmail.com"] = ("tok", 1e12)
    google.forget_token()
    assert "pat@gmail.com" not in google._tokens


@pytest.mark.parametrize(
    "body",
    [
        err(403, "insufficientPermissions"),
        {
            "error": {
                "code": 403,
                "message": "Request had insufficient authentication scopes.",
                "status": "PERMISSION_DENIED",
                "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
            }
        },
    ],
)
def test_insufficient_scope_is_consent_required(body, httpx_mock, gtoken):
    for _ in range(2):  # one retry with a fresh token first
        httpx_mock.add_response(url=FILE_URL, status_code=403, json=body)
    with pytest.raises(ConsentRequired) as exc:
        g().get(FILE_URL)
    assert exc.value.bundle == "gdrive" and exc.value.provider == "google"
    assert "broker365 login --provider google --scopes gdrive" in str(exc.value)
    assert gtoken["forgot"] == [None]


def test_scope_403_from_stale_token_succeeds_after_refresh(httpx_mock, gtoken):
    httpx_mock.add_response(url=FILE_URL, status_code=403, json=err(403, "insufficientPermissions"))
    httpx_mock.add_response(url=FILE_URL, json={"id": "F1"})
    assert g().get(FILE_URL)["id"] == "F1"
    auths = [r.headers["Authorization"] for r in httpx_mock.get_requests()]
    assert auths == ["Bearer g-token-1", "Bearer g-token-2"]


def test_error_code_priority(httpx_mock):
    body = {
        "error": {
            "code": 403,
            "message": "m",
            "status": "PERMISSION_DENIED",
            "errors": [{"reason": "zzzFirst"}, {"reason": "aaaSecond"}],
            "details": [{"reason": "DETAIL"}],
        }
    }
    httpx_mock.add_response(url=FILE_URL, status_code=403, json=body)
    with pytest.raises(GoogleApiError) as exc:
        g().get(FILE_URL)
    assert exc.value.code == "zzzFirst"
    details_only = {"error": {"status": "FAILED_PRECONDITION", "details": [{"reason": "D1"}]}}
    httpx_mock.add_response(url=FILE_URL, status_code=400, json=details_only)
    with pytest.raises(GoogleApiError) as exc:
        g().get(FILE_URL)
    assert exc.value.code == "D1"
    httpx_mock.add_response(url=FILE_URL, status_code=400, json={"error": {"status": "ABORTED"}})
    with pytest.raises(GoogleApiError) as exc:
        g().get(FILE_URL)
    assert exc.value.code == "ABORTED"


def test_404_message_is_friendly(httpx_mock):
    httpx_mock.add_response(url=FILE_URL, status_code=404, json=err(404, "notFound", "File F1"))
    with pytest.raises(GoogleApiError, match="not found or no access"):
        g().get(FILE_URL)


def test_other_errors_are_google_api_errors(httpx_mock):
    httpx_mock.add_response(
        method="PATCH", url=FILE_URL, status_code=412, json=err(412, "conditionNotMet")
    )
    with pytest.raises(GoogleApiError) as exc:
        g().patch(FILE_URL, json={})
    assert isinstance(exc.value, ApiError) and not isinstance(exc.value, ConflictError)
    assert str(exc.value) == "Google 412 conditionNotMet: nope"


def test_non_json_error_body(httpx_mock):
    httpx_mock.add_response(url=FILE_URL, status_code=400, text="<html>bad</html>")
    with pytest.raises(GoogleApiError, match="Google 400"):
        g().get(FILE_URL)


# ---- paging -----------------------------------------------------------------------------


def test_paged_follows_next_page_token(httpx_mock):
    url = f"{DRIVE}/files"
    httpx_mock.add_response(
        url=re.compile(re.escape(url) + r"\?(?!.*pageToken).*"),
        json={"files": [{"id": "1"}, {"id": "2"}], "nextPageToken": "p2"},
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(url) + r"\?.*pageToken=p2.*"),
        json={"files": [{"id": "3"}, {"id": "4"}], "nextPageToken": "p3"},
    )
    out = g().paged(url, {"q": "x"}, "files", limit=3)
    assert [f["id"] for f in out] == ["1", "2", "3"]
    first, second = (r.url.params for r in httpx_mock.get_requests())
    assert first["pageSize"] == "3" and first["q"] == "x"
    assert second["pageSize"] == "1" and second["pageToken"] == "p2"


def test_paged_stops_without_token(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(DRIVE) + r"/files.*"), json={"files": []})
    assert g().paged(f"{DRIVE}/files") == []


# ---- uploads ----------------------------------------------------------------------------


def test_multipart_upload(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{gapi.DRIVE_UPLOAD}?uploadType=multipart&fields=id",
        json={"id": "new"},
    )
    out = g().upload(
        b"hello", {"name": "a.txt", "parents": ["P"]}, "text/plain", params={"fields": "id"}
    )
    assert out == {"id": "new"}
    req = httpx_mock.get_requests()[0]
    ctype = req.headers["Content-Type"]
    assert ctype.startswith("multipart/related; boundary=")
    boundary = ctype.split("boundary=")[1]
    parts = req.content.split(f"--{boundary}".encode())
    assert json.loads(parts[1].split(b"\r\n\r\n", 1)[1]) == {"name": "a.txt", "parents": ["P"]}
    assert parts[2] == b"\r\nContent-Type: text/plain\r\n\r\nhello\r\n"


def test_multipart_update_uses_patch(httpx_mock):
    httpx_mock.add_response(
        method="PATCH", url=f"{gapi.DRIVE_UPLOAD}/F1?uploadType=multipart", json={"id": "F1"}
    )
    assert g().upload(b"x", {}, "text/plain", file_id="F1")["id"] == "F1"


def test_resumable_upload_in_chunks(httpx_mock, monkeypatch):
    monkeypatch.setattr(gapi, "MULTIPART_MAX", 10)
    monkeypatch.setattr(gapi, "UPLOAD_CHUNK", 256 * 1024)
    data = b"a" * (256 * 1024 * 2 + 100)
    session = f"{gapi.DRIVE_UPLOAD}?uploadType=resumable&upload_id=U1"
    httpx_mock.add_response(
        method="POST",
        url=f"{gapi.DRIVE_UPLOAD}?uploadType=resumable",
        headers={"Location": session},
    )
    httpx_mock.add_response(
        method="PUT", url=session, status_code=308, headers={"Range": "bytes=0-262143"}
    )
    httpx_mock.add_response(
        method="PUT", url=session, status_code=308, headers={"Range": "bytes=0-524287"}
    )
    httpx_mock.add_response(method="PUT", url=session, json={"id": "big"})
    assert g().upload(data, {"name": "big.bin"}, "application/octet-stream")["id"] == "big"
    start, *puts = httpx_mock.get_requests()
    assert start.headers["X-Upload-Content-Length"] == str(len(data))
    assert json.loads(start.content) == {"name": "big.bin"}
    size = len(data)
    assert [r.headers["Content-Range"] for r in puts] == [
        f"bytes 0-262143/{size}",
        f"bytes 262144-524287/{size}",
        f"bytes 524288-{size - 1}/{size}",
    ]
    assert all(r.headers["Authorization"].startswith("Bearer ") for r in puts)


def test_resumable_session_uri_must_be_allow_listed(httpx_mock, monkeypatch):
    monkeypatch.setattr(gapi, "MULTIPART_MAX", 1)
    httpx_mock.add_response(
        method="POST",
        url=f"{gapi.DRIVE_UPLOAD}?uploadType=resumable",
        headers={"Location": "https://upload.evil.example/s"},
    )
    with pytest.raises(Broker365Error, match="non-Google-API"):
        g().upload(b"abc", {"name": "x"}, "text/plain")
    assert len(httpx_mock.get_requests()) == 1


def test_resumable_resends_partial_chunk(httpx_mock, monkeypatch):
    monkeypatch.setattr(gapi, "MULTIPART_MAX", 1)
    monkeypatch.setattr(gapi, "UPLOAD_CHUNK", 8)
    session = f"{gapi.DRIVE_UPLOAD}?uploadType=resumable&upload_id=U2"
    httpx_mock.add_response(
        method="POST",
        url=f"{gapi.DRIVE_UPLOAD}?uploadType=resumable",
        headers={"Location": session},
    )
    httpx_mock.add_response(
        method="PUT", url=session, status_code=308, headers={"Range": "bytes=0-3"}
    )
    httpx_mock.add_response(method="PUT", url=session, json={"id": "ok"})
    g().upload(b"0123456789", {"name": "x"}, "text/plain")
    puts = [r for r in httpx_mock.get_requests() if r.method == "PUT"]
    assert puts[1].headers["Content-Range"] == "bytes 4-9/10" and puts[1].content == b"456789"


def test_resumable_upload_reads_file_in_chunks(httpx_mock, monkeypatch, tmp_path):
    monkeypatch.setattr(gapi, "MULTIPART_MAX", 1)
    monkeypatch.setattr(gapi, "UPLOAD_CHUNK", 4)
    src = tmp_path / "big.bin"
    src.write_bytes(b"0123456789")
    session = f"{gapi.DRIVE_UPLOAD}?uploadType=resumable&upload_id=U3"
    httpx_mock.add_response(
        method="POST",
        url=f"{gapi.DRIVE_UPLOAD}?uploadType=resumable",
        headers={"Location": session},
    )
    for last in (3, 7):
        httpx_mock.add_response(
            method="PUT", url=session, status_code=308, headers={"Range": f"bytes=0-{last}"}
        )
    httpx_mock.add_response(method="PUT", url=session, json={"id": "ok"})
    assert g().upload(src, {"name": "big.bin"}, "application/octet-stream")["id"] == "ok"
    puts = [r for r in httpx_mock.get_requests() if r.method == "PUT"]
    assert [r.content for r in puts] == [b"0123", b"4567", b"89"]


def test_download_to_streams_into_file_and_enforces_cap(httpx_mock):
    import io

    httpx_mock.add_response(url=FILE_URL, content=b"0123456789")
    out = io.BytesIO()
    g().download_to(FILE_URL, out)
    assert out.getvalue() == b"0123456789"
    httpx_mock.add_response(url=FILE_URL, content=b"0123456789")
    with pytest.raises(Broker365Error, match="larger than 4 bytes"):
        g().download_to(FILE_URL, io.BytesIO(), max_bytes=4)


def _session(httpx_mock, monkeypatch, upload_id):
    monkeypatch.setattr(gapi, "MULTIPART_MAX", 1)
    monkeypatch.setattr(gapi, "UPLOAD_CHUNK", 4)
    session = f"{gapi.DRIVE_UPLOAD}?uploadType=resumable&upload_id={upload_id}"
    httpx_mock.add_response(
        method="POST",
        url=f"{gapi.DRIVE_UPLOAD}?uploadType=resumable",
        headers={"Location": session},
    )
    return session


def test_resumable_chunk_5xx_queries_status_then_resumes(httpx_mock, monkeypatch, sleeps):
    session = _session(httpx_mock, monkeypatch, "U4")
    httpx_mock.add_response(method="PUT", url=session, status_code=503)  # chunk 0-3 fails
    # status query: Google already has bytes 0-1 only
    httpx_mock.add_response(
        method="PUT", url=session, status_code=308, headers={"Range": "bytes=0-1"}
    )
    httpx_mock.add_response(
        method="PUT", url=session, status_code=308, headers={"Range": "bytes=0-5"}
    )
    httpx_mock.add_response(method="PUT", url=session, json={"id": "ok"})
    assert g().upload(b"0123456789", {"name": "x"}, "text/plain")["id"] == "ok"
    puts = [r for r in httpx_mock.get_requests() if r.method == "PUT"]
    assert [(r.headers["Content-Range"], r.content) for r in puts] == [
        ("bytes 0-3/10", b"0123"),
        ("bytes */10", b""),
        ("bytes 2-5/10", b"2345"),
        ("bytes 6-9/10", b"6789"),
    ]
    assert sleeps == [1.0]


def test_resumable_network_error_on_last_chunk_finds_upload_done(httpx_mock, monkeypatch):
    session = _session(httpx_mock, monkeypatch, "U5")
    httpx_mock.add_response(
        method="PUT", url=session, status_code=308, headers={"Range": "bytes=0-3"}
    )
    httpx_mock.add_response(
        method="PUT", url=session, status_code=308, headers={"Range": "bytes=0-7"}
    )
    httpx_mock.add_exception(httpx.ReadTimeout("lost"), method="PUT", url=session)
    httpx_mock.add_response(method="PUT", url=session, status_code=200, json={"id": "done"})
    assert g().upload(b"0123456789", {"name": "x"}, "text/plain")["id"] == "done"
    puts = [r for r in httpx_mock.get_requests() if r.method == "PUT"]
    assert puts[-1].headers["Content-Range"] == "bytes */10"
    assert len(puts) == 4  # the lost chunk was not resent blindly


@pytest.mark.parametrize("status", [404, 410])
def test_resumable_session_expired(httpx_mock, monkeypatch, status):
    session = _session(httpx_mock, monkeypatch, "U6")
    httpx_mock.add_response(method="PUT", url=session, status_code=status)
    with pytest.raises(Broker365Error, match="upload session expired"):
        g().upload(b"0123456789", {"name": "x"}, "text/plain")


def test_resumable_gives_up_after_bounded_failures(httpx_mock, monkeypatch, sleeps):
    session = _session(httpx_mock, monkeypatch, "U7")
    for _ in range(gapi.MAX_RETRIES + 1):
        httpx_mock.add_response(method="PUT", url=session, status_code=500)
    with pytest.raises(Broker365Error, match="kept failing"):
        g().upload(b"0123456789", {"name": "x"}, "text/plain")
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert len([r for r in httpx_mock.get_requests() if r.method == "PUT"]) == gapi.MAX_RETRIES + 1


@pytest.mark.parametrize(
    "status, status_name, guarded, conflict",
    [
        (412, "", True, True),
        (400, "FAILED_PRECONDITION", True, False),  # only the revision message counts
        (400, "ABORTED", True, False),
        (400, "FAILED_PRECONDITION", False, False),  # not opted in
        (400, "INVALID_ARGUMENT", True, False),
        (412, "", False, False),
    ],
)
def test_guarded_requests_classify_conflicts(httpx_mock, status, status_name, guarded, conflict):
    from broker365.errors import GoogleConflictError

    body = {"error": {"code": status, "message": "m", "status": status_name}}
    httpx_mock.add_response(method="POST", url=FILE_URL, status_code=status, json=body)
    with pytest.raises(GoogleApiError) as exc:
        g().post(FILE_URL, json={}, guarded=guarded)
    assert isinstance(exc.value, GoogleConflictError) is conflict
    assert isinstance(exc.value, ConflictError) is conflict


def test_conflict_hierarchy_is_provider_neutral():
    from broker365.errors import GoogleConflictError, GraphConflictError, GraphError

    google_conflict = GoogleConflictError(400, "FAILED_PRECONDITION", "x")
    assert isinstance(google_conflict, ConflictError) and isinstance(google_conflict, ApiError)
    assert not isinstance(google_conflict, GraphError)
    assert str(google_conflict).startswith("Google 400")
    graph_412 = GraphError.from_response(
        httpx.Response(412, json={"error": {"code": "resourceModified", "message": "changed"}})
    )
    assert isinstance(graph_412, GraphConflictError)
    assert isinstance(graph_412, ConflictError) and isinstance(graph_412, GraphError)
    assert not isinstance(graph_412, GoogleApiError)
    assert str(graph_412) == "Graph 412 resourceModified: changed"


@pytest.mark.parametrize("guarded", [True, False])
def test_live_stale_revision_shape(httpx_mock, guarded):
    """Docs' real answer to a stale requiredRevisionId (seen live, Oct 2026)."""
    from broker365.errors import GoogleConflictError

    body = {
        "error": {
            "code": 400,
            "message": "The required revision ID 'ALm37BV' does not match the latest revision.",
            "status": "INVALID_ARGUMENT",
        }
    }
    httpx_mock.add_response(method="POST", url=FILE_URL, status_code=400, json=body)
    with pytest.raises(GoogleApiError) as exc:
        g().post(FILE_URL, json={}, guarded=guarded)
    assert isinstance(exc.value, GoogleConflictError) is guarded


def test_guarded_failed_precondition_keeps_googles_text(httpx_mock):
    body = {
        "error": {
            "code": 400,
            "message": "Precondition check failed.",
            "status": "FAILED_PRECONDITION",
        }
    }
    httpx_mock.add_response(method="POST", url=FILE_URL, status_code=400, json=body)
    with pytest.raises(GoogleApiError) as exc:
        g().post(FILE_URL, json={}, guarded=True)
    assert not isinstance(exc.value, ConflictError)
    assert str(exc.value) == "Google 400 FAILED_PRECONDITION: Precondition check failed."


@pytest.mark.parametrize(
    "url, param, value",
    [
        ("https://gmail.googleapis.com/gmail/v1/users/me/messages", "maxResults", "500"),
        ("https://tasks.googleapis.com/tasks/v1/lists/@default/tasks", "maxResults", "100"),
        ("https://www.googleapis.com/calendar/v3/calendars/primary/events", "maxResults", "2500"),
        ("https://people.googleapis.com/v1/people/me/connections", "pageSize", "1000"),
        (f"{DRIVE}/files", "pageSize", "1000"),
    ],
)
def test_paged_uses_each_apis_page_parameter(url, param, value, httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(url) + r"\?.*"), json={})
    g().paged(url, None, "items", limit=5000)
    assert httpx_mock.get_requests()[0].url.params[param] == value


def test_paged_explicit_page_parameter_wins(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(FILE_URL) + r"\?.*"), json={})
    g().paged(FILE_URL, None, "x", limit=50, size_param="maxResults", page_max=10)
    assert httpx_mock.get_requests()[0].url.params["maxResults"] == "10"


@pytest.mark.parametrize("value", [".", "..", "..."])
def test_seg_refuses_dot_segments(value):
    # httpx would collapse ".../events/.." onto the parent resource (e.g. a whole calendar).
    with pytest.raises(Broker365Error, match="Invalid"):
        transport.seg(value)


def test_seg_keeps_ids_with_dots():
    assert transport.seg("a.b") == "a.b" and transport.seg(".x") == ".x"
