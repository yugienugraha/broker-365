import json
import os
import stat
import time

import pytest
from typer.testing import CliRunner

from broker365 import auth, cli, config, providers, services
from broker365.cli import app as cli_app
from broker365.errors import Broker365Error, ReauthRequired
from broker365.providers import flows
from broker365.services import account
from tests.test_auth import ALICE, BOB, SIGNED_IN, FakeApp

runner = CliRunner()


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        app = FakeApp(**kw)
        monkeypatch.setattr(auth, "app", lambda: app)
        return app

    return install


def test_registry():
    ms = providers.get("microsoft")
    assert providers.get() is ms and providers.get(" Microsoft ") is ms
    gp = providers.get("google")
    assert providers.DEFAULT == "microsoft" and providers.all() == [ms, gp]
    assert (ms.name, ms.label) == ("microsoft", "Microsoft 365")
    assert ms.name == config.MICROSOFT == auth.PROVIDER == providers.DEFAULT
    assert "account.live.com/consent/Manage" in ms.revoke_hint
    assert ms.bundles() == {b: config.SCOPE_BUNDLES[b] for b in config.user_bundles()}
    assert "core" not in ms.bundles()
    assert providers.catalog() == [
        {"name": "microsoft", "label": "Microsoft 365", "available": True},
        {"name": "google", "label": "Google Workspace", "available": True},
    ]
    assert ms.default_bundles() == ["files"]


def test_unknown_provider():
    assert providers.get(" Google ").name == "google"
    with pytest.raises(Broker365Error, match="Unknown provider 'yahoo'. Known: microsoft, google"):
        providers.get("yahoo")


def test_microsoft_accounts_carry_provider(fake):
    fake(accounts=[ALICE, BOB])
    accts = providers.all_accounts()
    assert [a["provider"] for a in accts] == ["microsoft", "microsoft"]
    assert [a["username"] for a in accts] == ["alice@contoso.com", "bob@outlook.com"]


def test_legacy_default_account_is_read_then_migrated_on_write(fake):
    fake(accounts=[ALICE, BOB])
    config.update_settings(default_account="ALICE@contoso.com", read_only=True)
    assert config.default_account("microsoft") == "ALICE@contoso.com"
    assert config.default_account("google") is None
    # still legacy on disk; matching stays case-insensitive
    assert [a["default"] for a in auth.list_accounts()] == [True, False]
    assert auth.current_account()["username"] == "alice@contoso.com"
    assert "default_accounts" not in config.load_settings()

    auth.set_default("bob@outlook.com")
    assert config.load_settings() == {
        "read_only": True,
        "default_accounts": {"microsoft": "bob@outlook.com"},
    }
    assert [a["default"] for a in auth.list_accounts()] == [False, True]


def test_new_key_wins_over_legacy_and_clearing_removes_both():
    config.update_settings(
        default_account="old@x.com", default_accounts={"microsoft": "new@x.com", "google": "g@x"}
    )
    assert config.default_account("microsoft") == "new@x.com"
    config.set_default_account("microsoft", None)
    assert config.load_settings() == {"default_accounts": {"google": "g@x"}}
    config.set_default_account("google", None)
    assert config.load_settings() == {}


def test_first_login_default_only_if_unset(fake):
    config.update_settings(default_account="alice@contoso.com")
    fake(device_results=[SIGNED_IN])
    flow = auth.device_flow_start(["files"])
    auth.device_flow_complete(flow["flow_id"], wait_seconds=1)
    assert config.default_account("microsoft") == "alice@contoso.com"


def test_flow_file_records_provider_and_dispatch(fake, monkeypatch):
    fake(device_results=[SIGNED_IN])
    started = account.account_login_start(["files"])
    assert started["provider"] == "microsoft" and "device_code" not in started
    path = config.broker_home() / "flows" / f"{started['flow_id']}.json"
    assert json.loads(path.read_text())["provider"] == "microsoft"
    assert flows.load(started["flow_id"])[0] == "microsoft"
    done = account.account_login_complete(started["flow_id"], wait_seconds=1)
    assert done["status"] == "signed_in" and done["username"] == "bob@outlook.com"
    assert not path.exists()


def test_legacy_flow_file_without_provider_is_microsoft(fake):
    app = fake(device_results=[SIGNED_IN])
    flow_id = "legacyflow123"
    path = config.broker_home() / "flows" / f"{flow_id}.json"
    path.parent.mkdir(exist_ok=True)
    flow = app.initiate_device_flow(scopes=[])
    path.write_text(json.dumps(flow))
    assert flows.load(flow_id) == ("microsoft", flow)
    assert providers.login_complete(flow_id, wait_seconds=1)["status"] == "signed_in"


def test_flow_dispatch_to_google(fake):
    fake()
    flow_id = "googleflow123"
    path = config.broker_home() / "flows" / f"{flow_id}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"provider": "google", "data": {}}))
    with pytest.raises(Broker365Error, match="Sign-in expired, start again"):
        account.account_login_complete(flow_id)
    assert not path.exists()
    with pytest.raises(Broker365Error, match="Invalid flow id"):
        account.account_login_complete("../../config")


def test_microsoft_rejects_redirect_url(fake):
    fake()
    flow_id = account.account_login_start(["files"])["flow_id"]
    with pytest.raises(Broker365Error, match="only for Google"):
        account.account_login_complete(flow_id, redirect_url="http://127.0.0.1/?code=x")
    assert flows.load(flow_id)[0] == "microsoft"  # still usable


def test_account_login_start_provider_param(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "device_flow_start", lambda b: calls.append(b) or {"flow_id": "F"})
    started = account.account_login_start()
    assert started["flow_id"] == "F" and started["provider"] == "microsoft"
    account.account_login_start(["mail"], provider="microsoft")
    account.account_login_start(["calendar"], provider=None)
    assert calls == [["files"], ["mail"], ["calendar"]]
    with pytest.raises(Broker365Error, match="Unknown scope bundle.s.: gmail. Known: files"):
        account.account_login_start(["gmail"])
    started = account.account_login_start(provider="google")
    assert started["kind"] == "authorize_url" and started["needs_redirect_url"] is True
    assert flows.load(started["flow_id"])[0] == "google"
    assert "spreadsheets" in started["authorize_url"]  # a default bundle
    with pytest.raises(Broker365Error, match="Unknown provider"):
        services.call("account_login_start", provider="nope")


def test_account_status_lists_provider(fake):
    fake(accounts=[BOB])
    status = account.account_status()
    assert status["signed_in"] is True and "error" not in status
    assert status["accounts"] == [
        {
            "username": "bob@outlook.com",
            "account_type": "personal",
            "tenant_id": config.MSA_TENANT_ID,
            "default": False,
            "provider": "microsoft",
            "provider_label": "Microsoft 365",
            "tenant_label": "Tenant",
            "sign_in": "valid",
        }
    ]


def test_account_status_reports_provider_error(monkeypatch):
    def broken():
        raise Broker365Error("No client ID configured.")

    monkeypatch.setattr(auth, "app", broken)
    status = account.account_status()
    assert status["signed_in"] is False and status["accounts"] == []
    assert status["error"] == "No client ID configured."


def test_cli_provider_option(fake):
    fake(accounts=[ALICE, BOB])
    for args in (
        ["login", "--provider", "nope"],
        ["logout", "--provider", "nope"],
        ["accounts", "--default", "x@y.com", "--provider", "nope"],
    ):
        result = runner.invoke(cli_app, args)
        assert result.exit_code == 1
        assert "Unknown provider 'nope'. Known: microsoft, google" in result.output
    result = runner.invoke(cli_app, ["login", "--scopes", "nope", "--start"])
    assert result.exit_code == 1 and "Unknown scope bundle(s): nope" in result.output

    result = runner.invoke(cli_app, ["accounts", "--default", "BOB@outlook.com"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert [(a["provider"], a["default"]) for a in out] == [
        ("microsoft", False),
        ("microsoft", True),
    ]
    status = json.loads(runner.invoke(cli_app, ["status"]).output)
    assert {a["provider"] for a in status["accounts"]} == {"microsoft"}


def test_cli_login_start_and_complete(fake):
    fake(device_results=[SIGNED_IN])
    result = runner.invoke(cli_app, ["login", "--start", "--provider", "microsoft"])
    assert result.exit_code == 0, result.output
    started = json.loads(result.output)
    assert started["provider"] == "microsoft" and started["user_code"] == "ABCD"
    result = runner.invoke(cli_app, ["login", "--complete", started["flow_id"], "-p", "Microsoft"])
    assert json.loads(result.output)["status"] == "signed_in"


def test_tool_catalog_provider_field():
    catalog = services.tool_catalog()
    assert catalog and all(
        t["provider"]
        == (
            "google"
            if t["name"].startswith(("gdrive_", "gsheets_", "gdocs_", "gslides_"))
            else "microsoft"
        )
        for t in catalog
    )
    assert services.load_all()["account_status"].provider == "microsoft"


# ---- flow store -----------------------------------------------------------------------


def test_flows_save_load_consume():
    flow_id = flows.save("microsoft", {"device_code": "dc", "expires_at": 1})
    path = config.broker_home() / "flows" / f"{flow_id}.json"
    assert json.loads(path.read_text()) == {
        "provider": "microsoft",
        "data": {"device_code": "dc", "expires_at": 1},
    }
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert flows.load(flow_id) == ("microsoft", {"device_code": "dc", "expires_at": 1})
    flows.consume(flow_id)
    assert not path.exists()
    flows.consume(flow_id)  # idempotent
    with pytest.raises(Broker365Error, match="Unknown or already-used flow id"):
        flows.load(flow_id)


def test_flows_legacy_file_is_microsoft():
    path = config.broker_home() / "flows" / "legacyflow999.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"user_code": "X", "device_code": "d"}))
    assert flows.load("legacyflow999") == ("microsoft", {"user_code": "X", "device_code": "d"})
    # a file with the provider mixed into MSAL's dict
    path.write_text(json.dumps({"user_code": "X", "provider": "microsoft"}))
    assert flows.load("legacyflow999") == ("microsoft", {"user_code": "X"})


@pytest.mark.parametrize("bad", ["../../config", "..\\..\\config", "short", "a/b/c/d/e/f", ""])
def test_flows_reject_bad_ids(bad):
    for fn in (flows.load, flows.consume):
        with pytest.raises(Broker365Error, match="Invalid flow id"):
            fn(bad)


def test_flows_cleanup_drops_old_files():
    old = flows.save("microsoft", {})
    path = config.broker_home() / "flows" / f"{old}.json"
    stale = time.time() - flows.MAX_AGE - 10
    os.utime(path, (stale, stale))
    fresh = flows.save("microsoft", {})  # save() cleans up first
    assert not path.exists()
    assert flows.load(fresh)[0] == "microsoft"


# ---- login contract -------------------------------------------------------------------


def test_microsoft_login_start_kind_contract(fake):
    fake()
    started = providers.get().login_start(["files"])
    assert started["kind"] == "device_code" and started["needs_redirect_url"] is False
    assert started["provider"] == "microsoft"
    assert {"flow_id", "verification_uri", "user_code", "expires_in", "message"} <= set(started)
    assert "https://microsoft.com/devicelogin" in started["instructions"]
    assert "ABCD" in started["instructions"]
    assert "device_code" not in started


class FakeProvider:
    """A provider with an authorize_url (paste-back) flow, registered by `fake_provider`."""

    name = "fakeprov"
    label = "Fake Cloud"
    revoke_hint = "Revoke at https://fake.example/apps."
    available = True
    callback_path = None
    restricted_bundles = frozenset()

    def __init__(self, accounts=(), token_error=None, list_error=None):
        self.accounts = list(accounts)
        self.token_error = token_error
        self.list_error = list_error
        self.calls = []

    def bundles(self):
        return {"fdrive": ["drive.scope"], "fmail": ["mail.scope"]}

    def list_accounts(self):
        if self.list_error:
            raise Broker365Error(self.list_error)
        return [
            {"username": u, "default": False, "provider": self.name, "provider_label": self.label}
            for u in self.accounts
        ]

    def get_token(self, bundle, username=None):
        return "t"

    def check_token(self, username):
        if self.token_error:
            raise Broker365Error(self.token_error)

    def login_start(self, bundles, redirect_uri=None):
        flow_id = flows.save(self.name, {"bundles": bundles, "state": "s1"})
        return {
            "flow_id": flow_id,
            "provider": self.name,
            "kind": "authorize_url",
            "needs_redirect_url": True,
            "instructions": "Open the link, sign in, then paste the address-bar URL.",
            "authorize_url": "https://fake.example/auth?state=s1",
            "expires_in": 600,
        }

    def login_complete(self, flow_id, data, wait_seconds=None, redirect_url=None):
        if not redirect_url or "state=s1" not in redirect_url:
            raise Broker365Error("redirect_url is required")
        flows.consume(flow_id)
        self.calls.append(("complete", redirect_url, data["bundles"]))
        return {"status": "signed_in", "username": "pat@fake.example"}

    def login_interactive(self, bundles, port=None):
        raise AssertionError("not used")

    def logout(self, username=None):
        self.calls.append(("logout", username))
        return username

    def set_default(self, username):
        self.calls.append(("set_default", username))


@pytest.fixture
def fake_provider(monkeypatch):
    def install(**kw):
        p = FakeProvider(**kw)
        monkeypatch.setitem(providers._PROVIDERS, p.name, p)
        return p

    return install


def test_cli_headless_authorize_url_flow(fake_provider, monkeypatch):
    p = fake_provider()
    monkeypatch.setattr(cli, "_has_browser", lambda: False)
    result = runner.invoke(
        cli_app,
        ["login", "--provider", "fakeprov", "--scopes", "fmail"],
        input="http://127.0.0.1:1/?code=c&state=s1\n",
    )
    assert result.exit_code == 0, result.output
    assert "https://fake.example/auth?state=s1" in result.output
    assert "paste the address-bar URL" in result.output
    assert p.calls == [("complete", "http://127.0.0.1:1/?code=c&state=s1", ["fmail"])]
    assert '"status": "signed_in"' in result.output
    result = runner.invoke(cli_app, ["login", "--provider", "fakeprov", "--scopes", "files"])
    assert result.exit_code == 1 and "Known: fdrive, fmail" in result.output


def test_cli_login_complete_with_redirect_url(fake_provider):
    p = fake_provider()
    started = p.login_start(["fdrive"])
    result = runner.invoke(
        cli_app,
        ["login", "--complete", started["flow_id"], "--redirect-url", "http://x/?state=s1"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "signed_in"
    assert p.calls == [("complete", "http://x/?state=s1", ["fdrive"])]
    # the chat tools dispatch the same way
    started = account.account_login_start(["fmail"], provider="fakeprov")
    assert started["kind"] == "authorize_url" and started["needs_redirect_url"] is True
    done = account.account_login_complete(started["flow_id"], redirect_url="http://x/?state=s1")
    assert done["status"] == "signed_in"


def test_cli_scopes_help_lists_microsoft_bundles():
    assert "microsoft: files, files-shared, sharepoint" in cli.ScopesOpt.help


# ---- resolve / validate / check_token ---------------------------------------------------


def test_resolve(fake, fake_provider):
    fake(accounts=[ALICE, BOB])
    fp = fake_provider(accounts=["Pat@Fake.example", "bob@outlook.com"])
    ms = providers.get()
    assert providers.resolve() == (ms, None)
    assert providers.resolve(None, "fakeprov") == (fp, None)
    assert providers.resolve("x@y.com", "microsoft") == (ms, "x@y.com")  # explicit wins
    assert providers.resolve("ALICE@contoso.com") == (ms, "alice@contoso.com")
    assert providers.resolve("pat@fake.EXAMPLE") == (fp, "Pat@Fake.example")
    with pytest.raises(Broker365Error, match=r"several providers \(Microsoft 365, Fake Cloud\)"):
        providers.resolve("bob@outlook.com")
    with pytest.raises(Broker365Error, match="Account nobody@x.com is not signed in"):
        providers.resolve("nobody@x.com")
    with pytest.raises(Broker365Error, match="Unknown provider"):
        providers.resolve("alice@contoso.com", "nope")


def test_cli_logout_and_default_resolve_by_username(fake, fake_provider):
    app = fake(accounts=[ALICE, BOB])
    fp = fake_provider(accounts=["pat@fake.example"])
    result = runner.invoke(cli_app, ["logout", "-a", "pat@fake.example"])
    assert result.exit_code == 0, result.output
    assert "Signed out pat@fake.example. Revoke at https://fake.example/apps." in result.output
    result = runner.invoke(cli_app, ["accounts", "--default", "PAT@fake.example"])
    assert result.exit_code == 0, result.output
    assert fp.calls == [("logout", "pat@fake.example"), ("set_default", "pat@fake.example")]

    result = runner.invoke(cli_app, ["logout", "-a", "alice@contoso.com"])
    assert result.exit_code == 0, result.output
    assert app.removed == [ALICE]
    assert (
        "Signed out alice@contoso.com. To revoke the app entirely: https://myapps.microsoft.com "
        "(work) or https://account.live.com/consent/Manage (personal)."
    ) in " ".join(result.output.split())
    result = runner.invoke(cli_app, ["logout", "-a", "ghost@x.com"])
    assert result.exit_code == 1 and "Account ghost@x.com is not signed in" in result.output


def test_validate_bundles(fake_provider):
    fake_provider()
    assert providers.validate_bundles("microsoft", ["files", "mail"]) == ["files", "mail"]
    assert providers.validate_bundles(None, []) == []
    with pytest.raises(Broker365Error, match=r"Unknown scope bundle\(s\): nope. Known: files, "):
        providers.validate_bundles("microsoft", ["files", "nope"])
    with pytest.raises(Broker365Error, match=r"bundle\(s\): files. Known: fdrive, fmail$"):
        providers.validate_bundles("fakeprov", ["files"])
    assert providers.validate_bundles("google", ["gmail", "core"]) == ["gmail", "core"]
    with pytest.raises(Broker365Error, match=r"bundle\(s\): files. Known: gdrive, gdrive-app, "):
        providers.validate_bundles("google", ["files"])


def test_account_status_uses_check_token(fake, fake_provider, monkeypatch):
    fake(accounts=[BOB])
    fake_provider(accounts=["pat@fake.example"], token_error="Sign-in expired. Run: login")
    seen = []
    monkeypatch.setattr(auth, "get_token", lambda b, u=None: seen.append((b, u)) or "t")
    status = account.account_status()
    assert seen == [("core", "bob@outlook.com")]
    assert [(a["provider"], a["sign_in"]) for a in status["accounts"]] == [
        ("microsoft", "valid"),
        ("fakeprov", "Sign-in expired. Run: login"),
    ]


# ---- settings ---------------------------------------------------------------------------


@pytest.fixture
def settings_writes(monkeypatch):
    writes = []
    real_save = config.save_settings
    monkeypatch.setattr(config, "save_settings", lambda s: writes.append(s) or real_save(s))
    return writes


def test_set_default_account_unchanged_writes_nothing(settings_writes):
    config.update_settings(default_account="alice@contoso.com")
    settings_writes.clear()
    config.set_default_account("microsoft", "bob@x.com", if_unset=True)
    config.set_default_account("microsoft", "alice@contoso.com")
    config.set_default_account("google", None)
    assert settings_writes == []
    assert config.load_settings() == {"default_account": "alice@contoso.com"}  # not migrated
    config.set_default_account("google", "g@x")
    assert settings_writes == [
        {"default_accounts": {"microsoft": "alice@contoso.com", "google": "g@x"}}
    ]


def test_repeat_login_does_not_rewrite_settings(fake, settings_writes):
    fake(device_results=[SIGNED_IN, SIGNED_IN])
    for _ in range(2):
        flow = auth.device_flow_start(["files"])
        auth.device_flow_complete(flow["flow_id"], wait_seconds=1)
    assert settings_writes == [{"default_accounts": {"microsoft": "bob@outlook.com"}}]


def test_malformed_default_accounts_is_ignored_then_rewritten(fake):
    fake(accounts=[ALICE, BOB])
    for bad in ("alice@contoso.com", ["x"], {"microsoft": 5, "google": "g@x"}):
        config.save_settings({"default_accounts": bad, "default_account": "bob@outlook.com"})
        want = {"google": "g@x"} if isinstance(bad, dict) else {}
        assert config._default_accounts(config.load_settings()) == {
            **want,
            "microsoft": "bob@outlook.com",
        }
        assert config.default_account("microsoft") == "bob@outlook.com"  # legacy fallback
        assert [a["default"] for a in auth.list_accounts()] == [False, True]
        auth.set_default("alice@contoso.com")
        assert config.load_settings() == {
            "default_accounts": {**want, "microsoft": "alice@contoso.com"}
        }
    # a no-op change still replaces a malformed table
    config.save_settings({"default_accounts": "junk", "default_account": 7})
    assert config.default_account("microsoft") is None
    config.set_default_account("google", None)
    assert config.load_settings() == {}


# ---- review fixes -----------------------------------------------------------------------


def test_core_bundle_is_accepted(fake, monkeypatch):
    fake()
    assert providers.validate_bundles("microsoft", ["core"]) == ["core"]
    assert providers.validate_bundles("microsoft", ["core", "mail"]) == ["core", "mail"]
    calls = []
    monkeypatch.setattr(auth, "device_flow_start", lambda b: calls.append(b) or {"flow_id": "F"})
    account.account_login_start(scopes=["core"])
    result = runner.invoke(cli_app, ["login", "-s", "core", "--start"])
    assert result.exit_code == 0, result.output
    assert calls == [["core"], ["core"]]


def test_all_accounts_and_resolve_skip_broken_provider(fake, fake_provider):
    fake(accounts=[ALICE])
    fake_provider(list_error="Fake Cloud is misconfigured")
    assert [a["username"] for a in providers.all_accounts()] == ["alice@contoso.com"]
    assert providers.resolve("ALICE@contoso.com") == (providers.get(), "alice@contoso.com")
    result = runner.invoke(cli_app, ["accounts", "--default", "alice@contoso.com"])
    assert result.exit_code == 0, result.output
    assert [a["default"] for a in json.loads(result.output)] == [True]


def test_resolve_unknown_account_is_reauth_required(fake, fake_provider):
    fake(accounts=[ALICE])
    fake_provider()
    with pytest.raises(ReauthRequired, match="Account ghost@x.com is not signed in. Run: "):
        providers.resolve("ghost@x.com")


def test_resolve_skips_lookup_when_provider_is_known(fake, monkeypatch):
    fake(accounts=[ALICE])
    listed = []
    real = auth.list_accounts
    monkeypatch.setattr(auth, "list_accounts", lambda: listed.append(1) or real())
    ms = providers.get()
    assert providers.resolve("x@y.com", "microsoft") == (ms, "x@y.com")
    assert listed == []
    # without a provider, the account is looked up and the provider's spelling returned
    assert providers.resolve("ALICE@contoso.com") == (ms, "alice@contoso.com")
    assert listed == [1]
    with pytest.raises(ReauthRequired, match="Account ghost@x.com is not signed in"):
        ms.logout("ghost@x.com")


def test_login_complete_reads_flow_once(fake, monkeypatch):
    fake(device_results=[{"error": "authorization_pending"}, SIGNED_IN])
    flow_id = account.account_login_start(["files"])["flow_id"]
    loads = []
    real = flows.load
    monkeypatch.setattr(flows, "load", lambda f: loads.append(f) or real(f))
    assert account.account_login_complete(flow_id, wait_seconds=1)["status"] == "pending"
    assert loads == [flow_id]
    assert account.account_login_complete(flow_id, wait_seconds=1)["status"] == "signed_in"
    assert loads == [flow_id, flow_id]


def test_flows_save_is_atomic(monkeypatch):
    def broken_dump(obj, f):
        f.write('{"provider": "micro')
        raise OSError("disk full")

    monkeypatch.setattr(flows.json, "dump", broken_dump)
    with pytest.raises(OSError, match="disk full"):
        flows.save("microsoft", {"device_code": "dc"})
    assert list((config.broker_home() / "flows").iterdir()) == []


def test_flows_cleanup_drops_stale_temp_files():
    d = config.broker_home() / "flows"
    d.mkdir(exist_ok=True)
    tmp = d / ".abcdefgh.x.tmp"
    tmp.write_text("{")
    stale = time.time() - flows.MAX_AGE - 10
    os.utime(tmp, (stale, stale))
    flows.cleanup()
    assert not tmp.exists()


def test_cli_login_option_combinations(fake, fake_provider):
    fake()
    fake_provider()
    result = runner.invoke(cli_app, ["login", "--redirect-url", "http://x/?state=s1"])
    assert result.exit_code == 2
    assert "--redirect-url only goes with --complete" in result.output
    flow_id = account.account_login_start(["files"])["flow_id"]
    result = runner.invoke(cli_app, ["login", "--complete", flow_id, "--provider", "fakeprov"])
    assert result.exit_code == 2
    assert "started with provider 'microsoft', not 'fakeprov'" in result.output
    assert flows.load(flow_id)[0] == "microsoft"  # untouched
    result = runner.invoke(cli_app, ["login", "--complete", "nosuchflow1", "-p", "microsoft"])
    assert result.exit_code == 1 and "Unknown or already-used flow id" in result.output
