"""Google provider (docs/GOOGLE_PLAN.md §3): PKCE sign-in by paste-back, dashboard callback and
loopback listener; encrypted token store; refresh; revoke. Google's endpoints are mocked with
pytest-httpx; the loopback callback is a real request to 127.0.0.1 through urllib."""

import base64
import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from hashlib import sha256
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from broker365 import audit, cli, config, providers
from broker365.cli import app as cli_app
from broker365.errors import Broker365Error, ConsentRequired, NetworkError, ReauthRequired
from broker365.providers import flows, google
from broker365.services import account
from broker365.ui import server
from tests.conftest import API, BASE, PORT, TOKEN

TOKEN_URL = google.TOKEN_URL
REVOKE_URL = google.REVOKE_URL
CLIENT_SECRET = config.DEFAULT_GOOGLE_CLIENT_SECRET
G = "https://www.googleapis.com/auth/"
CORE_GRANTED = f"openid {G}userinfo.email {G}userinfo.profile"
GP = providers.get("google")
runner = CliRunner()


def jwt(claims: dict) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"


def token_body(
    email="Pat@Gmail.com",
    refresh="rt-secret-1",
    scopes=(f"{G}drive",),
    hd=None,
    access="at-1",
    sub="sub-1",
    verified=True,
):
    claims = {"aud": config.DEFAULT_GOOGLE_CLIENT_ID, "email": email, "sub": sub}
    claims |= {"email_verified": verified, "name": "Pat"}
    if hd:
        claims["hd"] = hd
    body = {
        "access_token": access,
        "expires_in": 3599,
        "scope": " ".join([CORE_GRANTED, *scopes]),
        "token_type": "Bearer",
        "id_token": jwt(claims),
    }
    if refresh:
        body["refresh_token"] = refresh
    return body


def redirect_for(started: dict, code="auth-code-1", **override) -> str:
    """The address-bar URL Google redirects the browser to for a started flow."""
    q = parse_qs(urlsplit(started["authorize_url"]).query)
    params = {"state": q["state"][0], "code": code, "scope": "openid"} | override
    return f"{q['redirect_uri'][0]}?{urlencode({k: v for k, v in params.items() if v})}"


def sign_in(httpx_mock, bundles=("gdrive",), **body):
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=token_body(**body))
    started = GP.login_start(list(bundles))
    return account.account_login_complete(started["flow_id"], redirect_url=redirect_for(started))


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def stored() -> dict:
    return google._load()["accounts"]


# ---- login_start ------------------------------------------------------------------------


def test_authorize_url_parameters():
    started = GP.login_start(["gdrive", "gmail"])
    assert started["provider"] == "google" and started["kind"] == "authorize_url"
    assert started["needs_redirect_url"] is True and started["expires_in"] == 600
    assert "can't be reached" in started["instructions"]
    assert "address bar" in started["instructions"] and "Advanced" in started["instructions"]
    url = urlsplit(started["authorize_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == google.AUTH_URL
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["client_id"] == config.DEFAULT_GOOGLE_CLIENT_ID
    assert q["response_type"] == "code" and q["access_type"] == "offline"
    assert q["prompt"] == "consent" and q["include_granted_scopes"] == "true"
    assert q["scope"] == f"openid email profile {G}drive {G}gmail.modify"
    assert q["code_challenge_method"] == "S256"
    assert "client_secret" not in q and CLIENT_SECRET not in started["authorize_url"]
    redirect = urlsplit(q["redirect_uri"])
    assert (redirect.scheme, redirect.hostname, redirect.path) == ("http", "127.0.0.1", "/")
    assert redirect.port and redirect.port > 0
    flow_id, _, secret = q["state"].partition(".")
    assert flow_id == started["flow_id"] and len(secret) >= 32
    provider, data = flows.load(flow_id)
    assert provider == "google" and data["state_secret"] == secret
    assert data["redirect_uri"] == q["redirect_uri"]
    assert set(data) == {"verifier", "state_secret", "redirect_uri", "expires_at"}
    assert len(data["verifier"]) >= 43
    challenge = base64.urlsafe_b64encode(sha256(data["verifier"].encode()).digest()).rstrip(b"=")
    assert q["code_challenge"] == challenge.decode()
    assert data["expires_at"] - time.time() == pytest.approx(600, abs=5)


def test_dashboard_redirect_uri_needs_no_paste():
    uri = "http://127.0.0.1:8365/oauth/google/callback"
    started = GP.login_start(["gsheets"], redirect_uri=uri)
    assert started["needs_redirect_url"] is False
    assert parse_qs(urlsplit(started["authorize_url"]).query)["redirect_uri"] == [uri]
    assert "updates by itself" in started["instructions"]


def test_unknown_bundle_and_defaults():
    with pytest.raises(Broker365Error, match="Unknown scope bundle.s.: files. Known: gdrive"):
        GP.login_start(["files"])
    assert GP.default_bundles() == ["gdrive", "gsheets", "gdocs"]
    assert "core" not in GP.bundles() and GP.bundles()["gdrive-app"] == [f"{G}drive.file"]


def test_byo_client_id_requires_its_own_secret(monkeypatch):
    monkeypatch.setenv("BROKER365_GOOGLE_CLIENT_ID", "mine.apps.googleusercontent.com")
    assert GP.available is False
    assert providers.catalog()[1] == {
        "name": "google",
        "label": "Google Workspace",
        "available": False,
    }
    with pytest.raises(Broker365Error, match="No Google OAuth client configured"):
        GP.login_start(["gdrive"])
    monkeypatch.setenv("BROKER365_GOOGLE_CLIENT_SECRET", "my-secret")
    assert GP.available is True
    q = parse_qs(urlsplit(GP.login_start(["gdrive"])["authorize_url"]).query)
    assert q["client_id"] == ["mine.apps.googleusercontent.com"]


# ---- paste-back ---------------------------------------------------------------------------


def test_paste_back_happy_path(httpx_mock):
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=token_body())
    started = account.account_login_start(["gdrive"], provider="google")
    pending = account.account_login_complete(started["flow_id"])
    assert pending["status"] == "pending" and "redirect_url" in pending["message"]
    done = account.account_login_complete(
        started["flow_id"], redirect_url=redirect_for(started, code="auth-code-1")
    )
    assert done == {
        "status": "signed_in",
        "username": "Pat@Gmail.com",
        "account_type": "google",
        "tenant_id": None,
        "name": "Pat",
        "scopes": [*CORE_GRANTED.split(), f"{G}drive"],
        "provider": "google",
        "provider_label": "Google Workspace",
    }
    sent = form(httpx_mock.get_requests()[0])
    data_redirect = parse_qs(urlsplit(started["authorize_url"]).query)["redirect_uri"][0]
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "auth-code-1"
    assert sent["redirect_uri"] == data_redirect and len(sent["code_verifier"]) >= 43
    assert sent["client_id"] == config.DEFAULT_GOOGLE_CLIENT_ID
    assert sent["client_secret"] == CLIENT_SECRET
    # flow consumed, account stored, default set, audited without secrets
    with pytest.raises(Broker365Error, match="already-used"):
        flows.load(started["flow_id"])
    rec = stored()["pat@gmail.com"]
    assert rec["email"] == "Pat@Gmail.com" and rec["refresh_token"] == "rt-secret-1"
    assert rec["sub"] == "sub-1" and rec["hd"] is None and rec["created"]
    assert config.default_account("google") == "Pat@Gmail.com"
    assert GP.list_accounts() == [
        {
            "username": "Pat@Gmail.com",
            "account_type": "google",
            "tenant_id": None,
            "default": True,
            "provider": "google",
            "provider_label": "Google Workspace",
            "tenant_label": "Domain",
        }
    ]
    entry = audit.read()[0]
    assert entry["tool"] == "admin:login"
    assert entry["target"] == {  # same keys as Microsoft's login entry (auth._finish_login)
        "provider": "google",
        "username": "Pat@Gmail.com",
        "scopes": ["gdrive"],
    }
    log = (config.broker_home() / "audit.log").read_text()
    for secret in ("rt-secret-1", "auth-code-1", "at-1", CLIENT_SECRET, sent["code_verifier"]):
        assert secret not in log


def test_token_store_is_encrypted_or_private(httpx_mock):
    sign_in(httpx_mock)
    path = config.broker_home() / google.STORE_FILE
    raw = path.read_bytes()
    if b"rt-secret-1" in raw:  # plaintext fallback (no keyring backend): owner-only file
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    else:
        assert b"pat@gmail.com" not in raw.lower()  # encrypted (DPAPI / Keychain / libsecret)


def test_plaintext_cache_env(httpx_mock, monkeypatch):
    monkeypatch.setenv("BROKER365_PLAINTEXT_CACHE", "1")
    sign_in(httpx_mock)
    path = config.broker_home() / google.STORE_FILE
    data = json.loads(path.read_text())
    assert data["accounts"]["pat@gmail.com"]["refresh_token"] == "rt-secret-1"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_workspace_account_and_second_account_keeps_default(httpx_mock):
    sign_in(httpx_mock)
    done = sign_in(httpx_mock, email="lee@corp.example", hd="corp.example", refresh="rt-2")
    assert done["account_type"] == "workspace" and done["tenant_id"] == "corp.example"
    assert config.default_account("google") == "Pat@Gmail.com"
    assert [a["default"] for a in GP.list_accounts()] == [True, False]
    GP.set_default("LEE@corp.example")
    assert config.default_account("google") == "lee@corp.example"
    assert audit.read()[0]["tool"] == "admin:set_default"
    with pytest.raises(ReauthRequired, match="ghost@x.com is not signed in"):
        GP.set_default("ghost@x.com")


def test_reconsent_without_refresh_token_keeps_old_one_and_trusts_granted_scopes(httpx_mock):
    sign_in(httpx_mock, scopes=(f"{G}drive",))
    # include_granted_scopes: Google lists every current grant
    both = (f"{G}drive", f"{G}gmail.modify")
    sign_in(httpx_mock, bundles=("gmail",), refresh=None, scopes=both)
    rec = stored()["pat@gmail.com"]
    assert rec["refresh_token"] == "rt-secret-1"
    assert f"{G}drive" in rec["scopes"] and f"{G}gmail.modify" in rec["scopes"]
    assert audit.read()[0]["target"]["scopes"] == ["gdrive", "gmail"]
    # a grant the user revoked meanwhile is gone, not kept from the old record
    sign_in(httpx_mock, bundles=("gmail",), refresh=None, scopes=(f"{G}gmail.modify",))
    assert f"{G}drive" not in stored()["pat@gmail.com"]["scopes"]


def test_response_without_scope_keeps_stored_scopes(httpx_mock):
    sign_in(httpx_mock, scopes=(f"{G}drive",))
    body = token_body(refresh=None)
    del body["scope"]
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=body)
    started = GP.login_start(["gdrive"])
    account.account_login_complete(started["flow_id"], redirect_url=redirect_for(started))
    assert f"{G}drive" in stored()["pat@gmail.com"]["scopes"]


def test_unverified_email_is_refused(httpx_mock):
    with pytest.raises(Broker365Error, match="Google account email is not verified"):
        sign_in(httpx_mock, verified=False)
    assert stored() == {}


def test_same_email_other_google_account_replaces_record(httpx_mock):
    sign_in(httpx_mock, scopes=(f"{G}drive",))
    # a different account (sub) now owns the address: no refresh token or scopes carry over
    with pytest.raises(Broker365Error, match="did not return a refresh token"):
        sign_in(httpx_mock, sub="sub-2", refresh=None, scopes=(f"{G}gmail.modify",))
    sign_in(httpx_mock, sub="sub-2", refresh="rt-2", scopes=(f"{G}gmail.modify",))
    rec = stored()["pat@gmail.com"]
    assert rec["sub"] == "sub-2" and rec["refresh_token"] == "rt-2"
    assert f"{G}drive" not in rec["scopes"]


def test_store_writes_hold_the_cross_process_lock_and_reread(httpx_mock):
    sign_in(httpx_mock)
    path = config.broker_home() / google.STORE_FILE
    lockfile = config.broker_home() / f"{google.STORE_FILE}.lockfile"
    seen = []
    google._modify(lambda data: seen.append(lockfile.exists()))
    assert seen == [True] and not lockfile.exists()
    google._load()  # cached
    # another process adds an account behind our back; our next write must keep it
    data = json.loads(json.dumps(google._load()))
    data["accounts"]["b@x.com"] = {"email": "b@x.com", "refresh_token": "r", "scopes": []}
    google._store().save(json.dumps(data))
    google._modify(lambda d: d["accounts"]["pat@gmail.com"].update(hd=None))
    assert set(stored()) == {"pat@gmail.com", "b@x.com"} and path.exists()


def test_first_login_without_refresh_token_fails(httpx_mock):
    with pytest.raises(Broker365Error, match="did not return a refresh token"):
        sign_in(httpx_mock, refresh=None)
    assert stored() == {}


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda u: u.replace("state=", "state=x"), "state mismatch"),
        (lambda u: u.replace("http://127.0.0.1", "http://evil.example"), "not the broker365"),
        (lambda u: u.replace("http://", "https://"), "not the broker365"),
        (lambda u: u.replace("127.0.0.1:", "127.0.0.1:1"), "not the broker365"),
        (lambda u: u.replace("/?", "/x?"), "not the broker365"),
        (lambda u: u.replace("http://127.0.0.1:", "http://user@127.0.0.2:"), "not the broker365"),
        (lambda u: "not a url", "not the broker365"),
        (lambda u: u.replace("code=", "nocode="), "no authorization code"),
        (lambda u: u + "&state=again", "state mismatch"),
    ],
)
def test_paste_back_rejections_keep_flow(httpx_mock, mutate, message):
    started = GP.login_start(["gdrive"])
    bad = mutate(redirect_for(started))
    with pytest.raises(Broker365Error, match=message) as err:
        account.account_login_complete(started["flow_id"], redirect_url=bad)
    assert "auth-code-1" not in str(err.value)
    assert flows.load(started["flow_id"])[0] == "google"  # a wrong paste can be retried
    assert httpx_mock.get_requests() == []  # nothing exchanged, the URL never fetched


def test_paste_back_localhost_alias_is_accepted(httpx_mock):
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=token_body())
    started = GP.login_start(["gdrive"])
    url = redirect_for(started).replace("127.0.0.1", "localhost")
    assert account.account_login_complete(started["flow_id"], redirect_url=url)["status"] == (
        "signed_in"
    )


def test_access_denied_consumes_flow(httpx_mock):
    started = GP.login_start(["gdrive"])
    url = redirect_for(started, code=None, error="access_denied")
    with pytest.raises(Broker365Error, match="Sign-in was cancelled or permission denied"):
        account.account_login_complete(started["flow_id"], redirect_url=url)
    with pytest.raises(Broker365Error, match="already-used"):
        flows.load(started["flow_id"])
    started = GP.login_start(["gdrive"])
    url = redirect_for(started, code=None, error="<script>alert(1)</script>")
    with pytest.raises(Broker365Error, match=r"Google sign-in failed \(error\)"):
        account.account_login_complete(started["flow_id"], redirect_url=url)


def test_expired_and_reused_flow(httpx_mock):
    started = GP.login_start(["gdrive"])
    _, data = flows.load(started["flow_id"])
    flows.update(started["flow_id"], "google", {**data, "expires_at": time.time() - 1})
    with pytest.raises(Broker365Error, match="Sign-in expired, start again"):
        account.account_login_complete(started["flow_id"], redirect_url=redirect_for(started))
    with pytest.raises(Broker365Error, match="already-used"):
        account.account_login_complete(started["flow_id"])

    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=token_body())
    started = GP.login_start(["gdrive"])
    url = redirect_for(started)
    account.account_login_complete(started["flow_id"], redirect_url=url)
    with pytest.raises(Broker365Error, match="Unknown or already-used flow id"):
        account.account_login_complete(started["flow_id"], redirect_url=url)


def test_exchange_error_surfaces_message_without_secrets(httpx_mock):
    httpx_mock.add_response(
        url=TOKEN_URL,
        method="POST",
        status_code=400,
        json={"error": "invalid_grant", "error_description": "Malformed auth code."},
    )
    started = GP.login_start(["gdrive"])
    with pytest.raises(Broker365Error, match="Google sign-in failed: Malformed auth code.") as e:
        account.account_login_complete(started["flow_id"], redirect_url=redirect_for(started))
    assert "auth-code-1" not in str(e.value) and CLIENT_SECRET not in str(e.value)
    assert stored() == {}


def test_exchange_rejects_id_token_for_other_app(httpx_mock):
    body = token_body()
    body["id_token"] = jwt({"aud": "other-app", "email": "pat@gmail.com"})
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=body)
    started = GP.login_start(["gdrive"])
    with pytest.raises(Broker365Error, match="ID token for another app"):
        account.account_login_complete(started["flow_id"], redirect_url=redirect_for(started))


def test_network_error_is_generic(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom " + CLIENT_SECRET), url=TOKEN_URL)
    started = GP.login_start(["gdrive"])
    with pytest.raises(NetworkError, match=r"Could not reach Google \(ConnectError\)") as e:
        account.account_login_complete(started["flow_id"], redirect_url=redirect_for(started))
    assert CLIENT_SECRET not in str(e.value)


# ---- tokens -------------------------------------------------------------------------------


def test_exchange_token_is_memoized_then_refreshed(httpx_mock):
    sign_in(httpx_mock)
    assert GP.get_token("gdrive") == "at-1"  # from the exchange, no request
    google._tokens.clear()
    httpx_mock.add_response(
        url=TOKEN_URL, method="POST", json={"access_token": "at-2", "expires_in": 3599}
    )
    assert GP.get_token("gdrive", "PAT@gmail.com") == "at-2"
    assert GP.get_token("core") == "at-2"  # memoized: no second refresh request
    refresh = form(httpx_mock.get_requests()[-1])
    assert refresh["grant_type"] == "refresh_token"
    assert refresh["refresh_token"] == "rt-secret-1"
    assert refresh["client_secret"] == CLIENT_SECRET
    GP.check_token("pat@gmail.com")
    assert len(httpx_mock.get_requests()) == 2


def test_store_is_decoded_once_until_the_file_changes(httpx_mock, monkeypatch):
    sign_in(httpx_mock)
    assert GP.list_accounts()  # decoded and cached
    read = google._read
    monkeypatch.setattr(google, "_read", lambda: pytest.fail("store decoded again"))
    assert GP.get_token("gdrive") == "at-1" and len(GP.list_accounts()) == 1
    monkeypatch.setattr(google, "_read", read)
    other = {"accounts": {"b@x.com": {"email": "b@x.com", "refresh_token": "r"}}}
    google._store().save(json.dumps(other))  # another process wrote the store
    assert [a["username"] for a in GP.list_accounts()] == ["b@x.com"]


def test_concurrent_refreshes_are_single_flight(httpx_mock):
    sign_in(httpx_mock)
    google._tokens.clear()
    httpx_mock.add_response(
        url=TOKEN_URL, method="POST", json={"access_token": "at-2", "expires_in": 3599}
    )
    got: list[str] = []
    threads = [threading.Thread(target=lambda: got.append(GP.get_token("gdrive"))) for _ in "1234"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert got == ["at-2"] * 4 and len(httpx_mock.get_requests()) == 2  # exchange + 1 refresh


@pytest.mark.parametrize("expires_in", [None, "soon", -5, "nan", [1]])
def test_invalid_expires_in_defaults(httpx_mock, expires_in):
    sign_in(httpx_mock)
    google._tokens.clear()
    body = {"access_token": "at-2"}
    if expires_in is not None:
        body["expires_in"] = expires_in
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=body)
    assert GP.get_token("gdrive") == "at-2"
    assert google._tokens["pat@gmail.com"][1] > time.time() + 200  # memoized (3600 s assumed)


def test_non_string_access_token_is_a_broker_error(httpx_mock):
    sign_in(httpx_mock)
    google._tokens.clear()
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json={"access_token": 5})
    with pytest.raises(Broker365Error, match="Google token refresh failed"):
        GP.get_token("gdrive")


def test_memo_expires(httpx_mock, monkeypatch):
    sign_in(httpx_mock)
    google._tokens["pat@gmail.com"] = ("old", time.time() - 1)
    httpx_mock.add_response(
        url=TOKEN_URL, method="POST", json={"access_token": "fresh", "expires_in": 100}
    )
    assert GP.get_token("gdrive") == "fresh"
    assert "pat@gmail.com" not in google._tokens  # expires_in - 120 < 0: not memoized


def test_consent_required_mentions_provider(httpx_mock):
    sign_in(httpx_mock)
    with pytest.raises(ConsentRequired) as err:
        GP.get_token("gmail")
    assert str(err.value) == (
        "Permission 'gmail' not granted yet. Run: broker365 login --provider google --scopes gmail"
    )
    assert str(ConsentRequired("mail")) == (
        "Permission 'mail' not granted yet. Run: broker365 login --scopes mail"
    )
    with pytest.raises(Broker365Error, match="Unknown Google scope bundle 'files'"):
        GP.get_token("files")


def test_invalid_grant_is_reauth_required(httpx_mock):
    sign_in(httpx_mock)
    google._tokens.clear()
    httpx_mock.add_response(
        url=TOKEN_URL,
        method="POST",
        status_code=400,
        json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
        is_reusable=True,
    )
    with pytest.raises(ReauthRequired, match="Run: broker365 login --provider google"):
        GP.get_token("gdrive")
    assert google._tokens == {}
    status = account.account_status()
    google_acct = [a for a in status["accounts"] if a["provider"] == "google"]
    assert google_acct and "expired or was revoked" in google_acct[0]["sign_in"]


def test_account_resolution():
    with pytest.raises(ReauthRequired, match="Not signed in to Google. Run: broker365 login --pr"):
        GP.get_token("core")
    data = {
        "accounts": {
            k: {"email": k, "refresh_token": "r", "scopes": []} for k in ("a@x.com", "b@x.com")
        }
    }
    google._store().save(json.dumps(data))
    with pytest.raises(Broker365Error, match=r"Several Google accounts signed in \(a@x.com, b@x"):
        GP.get_token("core")
    google._tokens["b@x.com"] = ("tok-b", time.time() + 60)
    assert GP.get_token("core", "B@x.com") == "tok-b"
    config.set_default_account("google", "B@X.COM")
    assert GP.get_token("core") == "tok-b"
    with pytest.raises(ReauthRequired, match="Google account c@x.com is not signed in"):
        GP.get_token("core", "c@x.com")


def test_corrupt_store_is_an_error(monkeypatch):
    monkeypatch.setenv("BROKER365_PLAINTEXT_CACHE", "1")
    (config.broker_home() / google.STORE_FILE).write_text("[1, 2]")
    with pytest.raises(Broker365Error, match="Google token store .* is unreadable"):
        GP.list_accounts()
    status = account.account_status()
    assert "unreadable" in status["error"]


def test_providers_resolve_finds_google_account(httpx_mock):
    sign_in(httpx_mock)
    p, name = providers.resolve("PAT@gmail.com")
    assert p is GP and name == "Pat@Gmail.com"


# ---- logout -------------------------------------------------------------------------------


def test_logout_revokes_deletes_and_clears_default(httpx_mock):
    sign_in(httpx_mock)
    httpx_mock.add_response(url=REVOKE_URL, method="POST")
    assert GP.logout("pat@GMAIL.com") == "Pat@Gmail.com"
    assert form(httpx_mock.get_requests()[-1]) == {"token": "rt-secret-1"}
    assert stored() == {} and google._tokens == {}
    assert config.default_account("google") is None
    entry = audit.read()[0]
    assert entry["tool"] == "admin:logout"
    assert entry["target"] == {"provider": "google", "username": "Pat@Gmail.com"}
    assert "rt-secret-1" not in (config.broker_home() / "audit.log").read_text()
    with pytest.raises(ReauthRequired):
        GP.logout()


def test_logout_survives_network_error_and_keeps_other_default(httpx_mock):
    sign_in(httpx_mock)
    sign_in(httpx_mock, email="lee@x.com", refresh="rt-2")
    httpx_mock.add_exception(httpx.ConnectError("down"), url=REVOKE_URL)
    assert GP.logout("lee@x.com") == "lee@x.com"
    assert list(stored()) == ["pat@gmail.com"]
    assert config.default_account("google") == "Pat@Gmail.com"


def test_cli_logout_prints_revoke_hint(httpx_mock):
    sign_in(httpx_mock)
    httpx_mock.add_response(url=REVOKE_URL, method="POST")
    result = runner.invoke(cli_app, ["logout", "-a", "pat@gmail.com", "-p", "google"])
    assert result.exit_code == 0, result.output
    assert "https://myaccount.google.com/connections" in result.output


# ---- dashboard callback -------------------------------------------------------------------


@pytest.fixture
def dashboard(authed):
    """(the dashboard page with its session, Google's redirect without one), same app."""
    return authed, TestClient(authed.app, base_url=BASE)


def callback_path(started: dict, **override) -> str:
    url = urlsplit(redirect_for(started, **override))
    return f"{url.path}?{url.query}"


def test_dashboard_callback_flow(httpx_mock, dashboard):
    authed, browser = dashboard
    httpx_mock.add_response(
        url=TOKEN_URL, method="POST", json=token_body(email="<b>pat</b>@gmail.com")
    )
    r = authed.post("/api/login/start", headers=API, json={"provider": "google"})
    started = r.json()
    assert started["needs_redirect_url"] is False
    q = parse_qs(urlsplit(started["authorize_url"]).query)
    assert q["redirect_uri"] == ["http://127.0.0.1:8365/oauth/google/callback"]
    assert q["scope"] == [f"openid email profile {G}drive {G}spreadsheets {G}documents"]
    poll = {"flow_id": started["flow_id"]}
    assert authed.post("/api/login/complete", headers=API, json=poll).json()["status"] == (
        "pending"
    )

    # a foreign Host is still refused on the cookie-less route
    r = browser.get(callback_path(started), headers={"Host": "evil.example:8365"})
    assert r.status_code == 400 and r.text == "Invalid Host header\n"
    # a wrong state is refused and leaves the flow pending
    r = browser.get(callback_path(started, state="x" + started["flow_id"] + ".nope"))
    assert r.status_code == 400 and "did not complete" in r.text
    r = browser.get(callback_path(started, state=started["flow_id"] + ".wrong"))
    assert r.status_code == 400 and "did not complete" in r.text
    assert "auth-code-1" not in r.text
    assert authed.post("/api/login/complete", headers=API, json=poll).json()["status"] == (
        "pending"
    )

    r = browser.get(callback_path(started))  # no session cookie: allowed
    assert r.status_code == 200
    assert "Signed in to Google Workspace as &lt;b&gt;pat&lt;/b&gt;@gmail.com" in r.text
    assert "<b>" not in r.text and "<script" not in r.text and 'href="/"' in r.text
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"
    # a replay of the same redirect does nothing and leaves the stored result intact
    r = browser.get(callback_path(started))
    assert r.status_code == 400
    assert set(flows.load(started["flow_id"])[1]) == {"expires_at", "result"}

    done = authed.post("/api/login/complete", headers=API, json=poll).json()
    assert done["status"] == "signed_in" and done["username"] == "<b>pat</b>@gmail.com"
    with pytest.raises(Broker365Error, match="already-used"):
        flows.load(started["flow_id"])
    assert len(httpx_mock.get_requests()) == 1


def test_dashboard_callback_denied_and_exchange_error(httpx_mock, dashboard):
    authed, browser = dashboard
    started = authed.post("/api/login/start", headers=API, json={"provider": "google"}).json()
    r = browser.get(callback_path(started, code=None, error="access_denied"))
    assert r.status_code == 400 and "did not complete" in r.text
    r = authed.post("/api/login/complete", headers=API, json={"flow_id": started["flow_id"]})
    assert r.status_code == 400 and "cancelled or permission denied" in r.json()["error"]

    httpx_mock.add_response(
        url=TOKEN_URL, method="POST", status_code=400, json={"error": "invalid_grant"}
    )
    started = authed.post("/api/login/start", headers=API, json={"provider": "google"}).json()
    assert browser.get(callback_path(started)).status_code == 400
    r = authed.post("/api/login/complete", headers=API, json={"flow_id": started["flow_id"]})
    assert r.status_code == 400 and "Google sign-in failed: invalid_grant" in r.json()["error"]


@pytest.mark.parametrize(
    "query",
    ["", "?state=", "?state=..%2F..%2Fconfig.x&code=c", "?code=c", "?state=nosuchflow1.x&code=c"],
)
def test_dashboard_callback_garbage(dashboard, query):
    _, browser = dashboard
    r = browser.get(f"/oauth/google/callback{query}")
    assert r.status_code == 400 and "did not complete" in r.text


def test_dashboard_callback_refuses_microsoft_flow(dashboard):
    _, browser = dashboard
    flow_id = flows.save("microsoft", {"device_code": "d"})
    r = browser.get(f"/oauth/google/callback?state={flow_id}.x&code=c")
    assert r.status_code == 400
    assert flows.load(flow_id) == ("microsoft", {"device_code": "d"})


def test_dashboard_callback_records_errors_after_state_matched(dashboard):
    authed, browser = dashboard
    started = authed.post("/api/login/start", headers=API, json={"provider": "google"}).json()
    r = browser.get(callback_path(started, code=None))  # right state, no code
    assert r.status_code == 400
    r = authed.post("/api/login/complete", headers=API, json={"flow_id": started["flow_id"]})
    assert r.status_code == 400 and "no authorization code" in r.json()["error"]  # polling ends


@pytest.mark.parametrize(
    "host, redirect",
    [
        ("localhost", "http://127.0.0.1:8365/oauth/google/callback"),
        ("::1", "http://[::1]:8365/oauth/google/callback"),
        ("0.0.0.0", None),  # noqa: S104 - a non-loopback bind
        ("192.168.1.5", None),
    ],
)
def test_dashboard_redirect_only_on_loopback_bind(static, host, redirect):
    c = TestClient(server.create_app(TOKEN, PORT, static, host=host), base_url=BASE)
    c.cookies.set(server.cookie_name(PORT), TOKEN)
    started = c.post("/api/login/start", headers=API, json={"provider": "google"}).json()
    uri = parse_qs(urlsplit(started["authorize_url"]).query)["redirect_uri"][0]
    if redirect:
        assert uri == redirect and started["needs_redirect_url"] is False
    else:  # paste-back to a loopback URL instead of the (unreachable) dashboard
        assert uri.startswith("http://127.0.0.1:") and uri.endswith("/")
        assert started["needs_redirect_url"] is True


def test_ipv6_loopback_redirect_is_accepted(httpx_mock):
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=token_body())
    started = GP.login_start(["gdrive"], redirect_uri="http://[::1]:8365/oauth/google/callback")
    url = redirect_for(started)
    assert url.startswith("http://[::1]:8365/oauth/google/callback?")
    done = account.account_login_complete(started["flow_id"], redirect_url=url)
    assert done["status"] == "signed_in"


def test_callback_post_still_needs_session(dashboard):
    _, browser = dashboard
    assert browser.post("/oauth/google/callback").status_code == 401


# ---- loopback listener (CLI) ---------------------------------------------------------------


def fake_browser(monkeypatch, requests):
    """Replace the browser: on open, send `requests(url)` (a list of query strings) to the
    loopback redirect with urllib (not httpx, so pytest-httpx doesn't intercept it)."""
    seen: list = []
    threads: list[threading.Thread] = []

    def visit(url):
        q = parse_qs(urlsplit(url).query)
        redirect, state = q["redirect_uri"][0], q["state"][0]

        def run():
            for path in requests(state):
                try:
                    with urllib.request.urlopen(redirect.rstrip("/") + path, timeout=5) as r:  # noqa: S310 - http loopback
                        seen.append((r.status, r.read().decode(), dict(r.headers)))
                except urllib.error.HTTPError as exc:
                    seen.append((exc.code, exc.read().decode(), dict(exc.headers)))

        thread = threading.Thread(target=run, daemon=True)
        threads.append(thread)
        thread.start()
        return True

    monkeypatch.setattr(google.webbrowser, "open", visit)

    def done():
        for t in threads:
            t.join(5)
        return seen

    return done


def test_login_interactive(httpx_mock, monkeypatch):
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=token_body())
    seen = fake_browser(
        monkeypatch,
        lambda state: [
            "/favicon.ico",
            f"/?state={state}x&code=evil",
            f"/?state={state}&code=auth-code-1&scope=openid",
        ],
    )
    done = GP.login_interactive(["gdrive"])
    seen = seen()
    assert done["username"] == "Pat@Gmail.com" and done["provider"] == "google"
    sent = form(httpx_mock.get_requests()[0])
    assert sent["code"] == "auth-code-1" and sent["redirect_uri"].startswith("http://127.0.0.1:")
    assert [s[0] for s in seen] == [404, 400, 200]
    assert "Signed in. You can close this tab." in seen[2][1]
    assert seen[2][2]["Content-Security-Policy"].startswith("default-src 'none'")
    assert list((config.broker_home() / "flows").glob("*.json")) == []


def test_login_interactive_survives_idle_connection(httpx_mock, monkeypatch):
    """A connection that never sends a request (a browser preconnect) is dropped after
    READ_TIMEOUT, so the real redirect behind it is still served before the deadline."""
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=token_body())
    monkeypatch.setattr(google, "READ_TIMEOUT", 0.3)
    idle: list[socket.socket] = []

    def visit(url):
        q = parse_qs(urlsplit(url).query)
        redirect, state = q["redirect_uri"][0], q["state"][0]
        idle.append(socket.create_connection(("127.0.0.1", urlsplit(redirect).port)))

        def run():
            time.sleep(0.1)
            target = f"{redirect}?state={state}&code=auth-code-1"
            urllib.request.urlopen(target, timeout=5).close()  # noqa: S310 - http loopback

        threading.Thread(target=run, daemon=True).start()
        return True

    monkeypatch.setattr(google.webbrowser, "open", visit)
    try:
        assert GP.login_interactive(["gdrive"])["username"] == "Pat@Gmail.com"
    finally:
        for sock in idle:
            sock.close()


def test_login_interactive_denied(monkeypatch):
    seen = fake_browser(monkeypatch, lambda state: [f"/?state={state}&error=access_denied"])
    with pytest.raises(Broker365Error, match="cancelled or permission denied"):
        GP.login_interactive(["gdrive"])
    assert seen()[0][0] == 400


def test_login_interactive_times_out(monkeypatch):
    monkeypatch.setattr(google, "INTERACTIVE_TIMEOUT", 0.6)
    with pytest.raises(Broker365Error, match="timed out"):
        GP.login_interactive(["gdrive"])


# ---- CLI ----------------------------------------------------------------------------------


def test_cli_headless_paste_flow(httpx_mock, monkeypatch):
    httpx_mock.add_response(url=TOKEN_URL, method="POST", json=token_body())
    monkeypatch.setattr(cli, "_has_browser", lambda: False)
    prompts = []

    def paste(text):
        prompts.append(text)
        (flow_file,) = (config.broker_home() / "flows").glob("*.json")
        flow_id = flow_file.stem
        data = flows.load(flow_id)[1]
        return f"{data['redirect_uri']}?state={flow_id}.{data['state_secret']}&code=auth-code-1\n"

    monkeypatch.setattr(cli.typer, "prompt", paste)
    result = runner.invoke(cli_app, ["login", "--provider", "google"])
    assert result.exit_code == 0, result.output
    assert "https://accounts.google.com/o/oauth2/v2/auth?" in result.output
    assert "can't be reached" in result.output and prompts
    out = json.loads(result.output[result.output.index("{") :])
    assert out["status"] == "signed_in" and out["username"] == "Pat@Gmail.com"
    assert audit.read()[0]["target"]["scopes"] == ["gdrive"]  # what Google granted
    q = parse_qs(urlsplit(httpx_mock.get_requests()[0].url.__str__()).query)
    assert q == {}
    assert CLIENT_SECRET not in result.output and "rt-secret-1" not in result.output


def test_cli_default_scopes_and_browser_path(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_has_browser", lambda: True)
    monkeypatch.setattr(
        type(GP), "login_interactive", lambda self, b, port=None: calls.append((b, port)) or {}
    )
    assert runner.invoke(cli_app, ["login", "-p", "google"]).exit_code == 0
    assert (
        runner.invoke(cli_app, ["login", "-p", "google", "-s", "gmail", "--port", "1"]).exit_code
        == 0
    )
    assert calls == [(["gdrive", "gsheets", "gdocs"], None), (["gmail"], 1)]
    result = runner.invoke(cli_app, ["login", "-p", "google", "--start"])
    started = json.loads(result.output)
    assert started["kind"] == "authorize_url" and started["needs_redirect_url"] is True
    scope = parse_qs(urlsplit(started["authorize_url"]).query)["scope"][0]
    assert scope == f"openid email profile {G}drive {G}spreadsheets {G}documents"


REAL_HAS_CONSENT = google.GoogleProvider.__dict__["has_consent"]  # conftest stubs it


def test_has_consent_reads_granted_bundles_without_network(httpx_mock):
    with pytest.raises(ReauthRequired):  # not signed in: its own error, not "no consent"
        REAL_HAS_CONSENT(GP, "gdrive")
    sign_in(httpx_mock)
    requests_before = len(httpx_mock.get_requests())
    assert REAL_HAS_CONSENT(GP, "gdrive") and REAL_HAS_CONSENT(GP, "core")
    assert not REAL_HAS_CONSENT(GP, "gmail")
    assert not REAL_HAS_CONSENT(GP, "files")  # a Microsoft bundle
    assert len(httpx_mock.get_requests()) == requests_before
    with pytest.raises(ReauthRequired, match="someone@else.com"):
        REAL_HAS_CONSENT(GP, "gdrive", "someone@else.com")


def test_tool_precheck_reports_sign_in_not_consent(monkeypatch):
    from broker365 import services

    monkeypatch.setattr(google.GoogleProvider, "has_consent", REAL_HAS_CONSENT)
    with pytest.raises(ReauthRequired):
        services.call("gdocs_create", title="T")


def test_account_email_does_not_decode_the_store_again(httpx_mock, monkeypatch):
    sign_in(httpx_mock)
    assert google.account_email() == "Pat@Gmail.com"  # decoded and cached
    monkeypatch.setattr(google, "_read", lambda: pytest.fail("store decoded again"))
    assert google.account_email() == "Pat@Gmail.com"
    assert google.account_email("PAT@gmail.com") == "Pat@Gmail.com"
