import json
import time

import pytest

from broker365 import auth, config
from broker365.errors import Broker365Error, ConsentRequired, ReauthRequired

REAL_GET_TOKEN = auth.get_token  # captured before conftest patches it
MSA = config.MSA_TENANT_ID


class FakeApp:
    def __init__(self, accounts=None, silent=None, device_results=None):
        self.accounts = accounts or []
        self.silent = silent
        self.device_results = list(device_results or [])
        self.removed = []

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent_with_error(self, scopes, account):
        self.last_scopes = scopes
        return self.silent

    def initiate_device_flow(self, scopes):
        return {
            "user_code": "ABCD",
            "verification_uri": "https://microsoft.com/devicelogin",
            "message": "go",
            "expires_in": 900,
            "expires_at": time.time() + 900,
            "device_code": "secret-device-code",
        }

    def acquire_token_by_device_flow(self, flow, exit_condition=None):
        return self.device_results.pop(0)

    def remove_account(self, acct):
        self.removed.append(acct)


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        app = FakeApp(**kw)
        monkeypatch.setattr(auth, "app", lambda: app)
        return app

    return install


ALICE = {"username": "alice@contoso.com", "realm": "common", "home_account_id": "o.tid-contoso"}
BOB = {"username": "bob@outlook.com", "realm": "common", "home_account_id": f"o.{MSA}"}
SIGNED_IN = {
    "access_token": "at",
    "scope": "User.Read Files.ReadWrite",
    "id_token_claims": {"preferred_username": "bob@outlook.com", "tid": MSA},
}


def test_get_token_success(fake):
    app = fake(accounts=[ALICE], silent={"access_token": "tok"})
    assert REAL_GET_TOKEN("files") == "tok"
    assert app.last_scopes == ["User.Read", "Files.ReadWrite"]


def test_get_token_consent_required(fake):
    fake(
        accounts=[ALICE],
        silent={"error": "invalid_grant", "error_description": "AADSTS65001: consent"},
    )
    with pytest.raises(ConsentRequired, match="--scopes mail"):
        REAL_GET_TOKEN("mail")


def test_get_token_not_signed_in(fake):
    fake(accounts=[])
    with pytest.raises(ReauthRequired):
        REAL_GET_TOKEN("files")


def test_multiple_accounts_need_default(fake):
    fake(accounts=[ALICE, BOB], silent={"access_token": "t"})
    with pytest.raises(Broker365Error, match="Several accounts"):
        auth.current_account()
    auth.set_default("bob@outlook.com")
    assert auth.current_account()["username"] == "bob@outlook.com"
    assert auth.is_personal() is True


def test_device_flow_pending_then_signed_in(fake):
    fake(device_results=[{"error": "authorization_pending"}, SIGNED_IN])
    started = auth.device_flow_start(["files"])
    assert "device_code" not in started
    flow_file = config.broker_home() / "flows" / f"{started['flow_id']}.json"
    assert json.loads(flow_file.read_text())["device_code"] == "secret-device-code"
    assert auth.device_flow_complete(started["flow_id"], wait_seconds=1)["status"] == "pending"
    done = auth.device_flow_complete(started["flow_id"], wait_seconds=1)
    assert done["status"] == "signed_in" and done["account_type"] == "personal"
    assert not flow_file.exists()
    assert config.load_settings()["default_account"] == "bob@outlook.com"


def test_flow_id_cannot_traverse(fake):
    fake()
    with pytest.raises(Broker365Error, match="Invalid flow id"):
        auth.device_flow_complete("../../config")


def test_logout_clears_default(fake):
    app = fake(accounts=[BOB])
    config.update_settings(default_account="bob@outlook.com")
    assert auth.logout() == "bob@outlook.com"
    assert app.removed == [BOB]
    assert "default_account" not in config.load_settings()


def test_placeholder_client_id_rejected(monkeypatch):
    monkeypatch.delenv("BROKER365_CLIENT_ID")
    monkeypatch.setattr(config, "DEFAULT_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    with pytest.raises(Broker365Error, match="No client ID"):
        auth.app()


def test_plaintext_cache_is_private(monkeypatch):
    monkeypatch.setenv("BROKER365_PLAINTEXT_CACHE", "1")
    auth._app.cache_clear()
    auth.app()
    path = config.broker_home() / "token_cache.bin"
    assert path.exists()
    if not auth.os.name == "nt":
        assert path.stat().st_mode & 0o077 == 0
    auth._app.cache_clear()


def test_token_memo_avoids_repeat_msal_calls(fake):
    app = fake(accounts=[ALICE], silent={"access_token": "tok", "expires_in": 3600})
    calls = []
    real = app.acquire_token_silent_with_error
    app.acquire_token_silent_with_error = lambda s, account: calls.append(s) or real(s, account)
    assert REAL_GET_TOKEN("files") == REAL_GET_TOKEN("files") == "tok"
    assert len(calls) == 1
    auth.logout()
    assert not auth._tokens


def test_default_account_case_insensitive(fake):
    app = fake(accounts=[ALICE, BOB])
    auth.set_default("ALICE@contoso.COM")
    assert config.load_settings()["default_account"] == "alice@contoso.com"
    assert [a["default"] for a in auth.list_accounts()] == [True, False]
    auth.logout()
    assert app.removed == [ALICE] and "default_account" not in config.load_settings()


def test_account_type_comes_from_home_account_id_not_realm(fake):
    # Real MSAL data seen live: realm is the authority segment ("common") for every account.
    fake(accounts=[ALICE, BOB])
    types = {a["username"]: (a["account_type"], a["tenant_id"]) for a in auth.list_accounts()}
    assert types == {
        "alice@contoso.com": ("work_or_school", "tid-contoso"),
        "bob@outlook.com": ("personal", MSA),
    }
    assert auth.is_personal_account(BOB) and not auth.is_personal_account(ALICE)


def test_tenant_falls_back_to_a_real_realm_but_never_common():
    assert auth._tenant({"realm": MSA}) == MSA
    assert auth._tenant({"realm": "common"}) == ""
