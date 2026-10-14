import pytest
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from broker365 import audit, auth, config, hermes
from broker365.errors import Broker365Error
from broker365.ui import server

TOKEN = "t0ken-for-tests"
BASE = "http://127.0.0.1:8365"
API = {"X-Broker365": "1"}
COOKIE = "broker365_session_8365"


@pytest.fixture
def static(tmp_path):
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>broker365</title>")
    (d / "app.js").write_text("console.log(1)")
    return d


@pytest.fixture
def client(static):
    return TestClient(server.create_app(TOKEN, 8365, static), base_url=BASE)


@pytest.fixture
def authed(client):
    client.cookies.set(COOKIE, TOKEN)
    return client


@pytest.fixture
def accounts(monkeypatch):
    accts = [
        {
            "username": "a@contoso.com",
            "account_type": "work",
            "tenant_id": "t",
            "default": True,
        }
    ]
    monkeypatch.setattr(auth, "list_accounts", lambda: [dict(a) for a in accts])
    return accts


def test_token_handshake_sets_cookie_and_redirects(client):
    r = client.get(f"/?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert f"{COOKIE}={TOKEN}" in cookie
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    r = client.get("/")
    assert r.status_code == 200 and "broker365" in r.text


def test_wrong_or_missing_token_401(client):
    assert client.get("/?token=nope").status_code == 401
    r = client.get("/")
    assert r.status_code == 401 and "broker365 ui" in r.text
    assert client.get("/static/app.js").status_code == 401


def test_api_requires_cookie_and_header(client):
    r = client.get("/api/scopes", headers=API)
    assert r.status_code == 401 and "error" in r.json()
    client.cookies.set(COOKIE, TOKEN)
    r = client.get("/api/scopes")
    assert r.status_code == 403 and "error" in r.json()
    assert client.get("/api/scopes", headers=API).status_code == 200


def test_stale_token_with_live_session_redirects(authed):
    r = authed.get("/?token=old", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_guard_protects_routes_without_api_wrapper(static):
    app = server.create_app(TOKEN, 8365, static)

    async def raw(request):
        return PlainTextResponse("secret")

    app.router.routes += [Route("/raw", raw), Route("/api/raw", raw)]
    c = TestClient(app, base_url=BASE)
    paths = [r.path for r in app.router.routes if "{" not in r.path] + ["/static/app.js", "/nope"]
    for path in paths:
        r = c.get(path, headers=API)
        assert r.status_code == 401 and "secret" not in r.text, path
        if path.startswith("/api/"):
            assert "error" in r.json()
    c.cookies.set(COOKIE, TOKEN)
    assert c.get("/raw").text == "secret"
    assert c.get("/api/raw").status_code == 403
    assert c.get("/api/raw", headers=API).text == "secret"
    assert c.post("/api/raw").status_code == 403  # checked before routing, whatever the method
    c.cookies.clear()
    assert c.post("/api/settings", headers=API, json={"read_only": True}).status_code == 401
    assert config.read_only() is False


def test_foreign_host_rejected(authed):
    r = authed.get("/api/scopes", headers={**API, "Host": "evil.example:8365"})
    assert r.status_code == 400
    assert authed.get("/", headers={"Host": "127.0.0.1:9999"}).status_code == 400
    assert authed.get("/", headers={"Host": "localhost:8365"}).status_code == 200


def test_cookie_is_per_port(static):
    assert server.cookie_name(8365) == COOKIE
    c = TestClient(server.create_app(TOKEN, 9001, static), base_url="http://127.0.0.1:9001")
    r = c.get(f"/?token={TOKEN}", follow_redirects=False)
    assert r.headers["set-cookie"].startswith(f"broker365_session_9001={TOKEN};")
    other = TestClient(server.create_app(TOKEN, 9001, static), base_url="http://127.0.0.1:9001")
    other.cookies.set(COOKIE, TOKEN)  # another port's session doesn't count here
    assert other.get("/").status_code == 401


def test_port_80_accepts_hosts_without_port(static):
    c = TestClient(server.create_app(TOKEN, 80, static), base_url="http://127.0.0.1")
    c.cookies.set("broker365_session_80", TOKEN)
    for host in ("127.0.0.1", "localhost", "[::1]", "127.0.0.1:80", "localhost:80"):
        assert c.get("/", headers={"Host": host}).status_code == 200, host
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 400
    plain = TestClient(server.create_app(TOKEN, 8365, static), base_url=BASE)
    plain.cookies.set(COOKIE, TOKEN)
    assert plain.get("/", headers={"Host": "127.0.0.1"}).status_code == 400


def test_non_loopback_host_allowed_when_explicit(static):
    c = TestClient(server.create_app(TOKEN, 8365, static, host="10.0.0.5"), base_url=BASE)
    c.cookies.set(COOKIE, TOKEN)
    assert c.get("/", headers={"Host": "10.0.0.5:8365"}).status_code == 200


def test_security_headers_everywhere(client, authed):
    for r in (client.get("/"), authed.get("/api/scopes", headers=API), authed.get("/x")):
        for key, value in server.SECURITY_HEADERS.items():
            assert r.headers[key] == value


def test_post_requires_json(authed):
    r = authed.post("/api/settings", headers=API, content="read_only=true")
    assert r.status_code == 415
    r = authed.post(
        "/api/settings", headers={**API, "Content-Type": "application/json"}, content="["
    )
    assert r.status_code == 400


def test_static_allow_list(authed):
    assert authed.get("/static/app.js").text == "console.log(1)"
    assert "javascript" in authed.get("/static/app.js").headers["content-type"]
    assert authed.get("/static/app.css").status_code == 404  # allowed but absent
    assert authed.get("/static/index.html").status_code == 404
    assert authed.get("/static/..%2Fconfig.toml").status_code == 404
    assert authed.get("/static/../config.toml").status_code == 404


def test_scopes_excludes_core(authed):
    bundles = authed.get("/api/scopes", headers=API).json()["bundles"]
    assert "core" not in bundles and bundles["mail"] == ["Mail.ReadWrite", "Mail.Send"]


def test_overview(authed, accounts, monkeypatch):
    def no_hermes(home=None):
        raise Broker365Error("Hermes config.yaml not found")

    monkeypatch.setattr(hermes, "hermes_home", no_hermes)
    r = authed.get("/api/overview", headers=API)
    data = r.json()
    assert data["signed_in"] is True and data["accounts"][0]["sign_in"] == "valid"
    assert data["client_id"].startswith("11111111") and data["read_only"] is False
    assert data["hermes"] == {"found": False, "home": None, "mcp_server": False, "skill": False}
    assert data["tools_count"] > 10 and "version" in data
    assert "test-token" not in r.text


def test_login_and_accounts(authed, accounts, monkeypatch):
    calls = []
    monkeypatch.setattr(
        auth, "device_flow_start", lambda b: calls.append(b) or {"flow_id": "F", "user_code": "C"}
    )
    monkeypatch.setattr(
        auth,
        "device_flow_complete",
        lambda f, wait_seconds: calls.append(wait_seconds) or {"status": "pending", "flow_id": f},
    )
    monkeypatch.setattr(auth, "set_default", lambda u: calls.append(u))
    monkeypatch.setattr(auth, "logout", lambda u: u)
    r = authed.post("/api/login/start", headers=API, json={"scopes": ["files", "mail"]})
    assert r.json()["flow_id"] == "F" and calls[-1] == ["files", "mail"]
    r = authed.post("/api/login/complete", headers=API, json={"flow_id": "F"})
    assert r.json() == {"status": "pending", "flow_id": "F"} and calls[-1] == 5
    authed.post("/api/login/start", headers=API, json={})
    assert calls[-1] == ["files"]
    r = authed.post("/api/accounts/default", headers=API, json={"username": "a@contoso.com"})
    assert r.json()["accounts"][0]["username"] == "a@contoso.com"
    r = authed.post("/api/accounts/logout", headers=API, json={"username": "a@contoso.com"})
    assert r.json() == {"signed_out": "a@contoso.com"}
    r = authed.post("/api/login/start", headers=API, json={"scopes": "files"})
    assert r.status_code == 400
    assert authed.post("/api/login/complete", headers=API, json={}).status_code == 400
    r = authed.post("/api/login/complete", headers=API, json={"flow_id": 3})
    assert r.status_code == 400


def test_settings_round_trip(authed, tmp_path):
    s = authed.get("/api/settings", headers=API).json()
    assert s["read_only"] is False and s["allowed_dirs"] == [] and s["excel_backend"] == {}
    assert s["read_only_env"] is False and s["workspace"] == str(config.workspace())
    d = tmp_path / "docs"
    d.mkdir()
    s = authed.post("/api/settings", headers=API, json={"read_only": True, "add_dir": str(d)})
    s = s.json()
    assert s["read_only"] is True and s["allowed_dirs"] == [str(d.resolve())]
    assert config.read_only() is True
    s = authed.post("/api/settings", headers=API, json={"remove_dir": str(d), "read_only": False})
    assert s.json()["allowed_dirs"] == [] and s.json()["read_only"] is False
    config.update_settings(excel_backend={"a@b.c": "graph"})
    s = authed.post("/api/settings", headers=API, json={"reset_excel_probe": True}).json()
    assert s["excel_backend"] == {}
    r = authed.post("/api/settings", headers=API, json={"read_only": "yes"})
    assert r.status_code == 400 and "read_only" in r.json()["error"]
    admin = [e["target"] for e in audit.read() if e["tool"] == "admin:settings"]
    assert admin == [
        {"excel_probe_reset": True},
        {"read_only": False, "dirs_removed": [str(d.resolve())]},
        {"read_only": True, "dirs_added": [str(d.resolve())]},
    ]


def test_tools_flags_hidden_in_read_only(authed, monkeypatch):
    tools = {t["name"]: t for t in authed.get("/api/tools", headers=API).json()}
    assert tools["files_delete"]["destructive"] and not tools["files_delete"]["hidden"]
    monkeypatch.setenv("BROKER365_READ_ONLY", "1")
    tools = {t["name"]: t for t in authed.get("/api/tools", headers=API).json()}
    assert tools["files_delete"]["hidden"] and not tools["files_list"]["hidden"]
    assert "\n" not in tools["files_list"]["description"]
    assert set(tools["files_list"]) == {
        "name", "bundle", "writes", "destructive", "description", "hidden"
    }  # fmt: skip


def test_activity(authed):
    assert authed.get("/api/activity", headers=API).json() == []
    for i in range(3):
        audit.record("files_delete", {"item": f"I{i}"}, result={"id": f"I{i}"})
    with (config.broker_home() / "audit.log").open("a") as f:
        f.write("not json\n")
    entries = authed.get("/api/activity?limit=2", headers=API).json()
    assert [e["target"]["item"] for e in entries] == ["I2", "I1"]
    assert authed.get("/api/activity?limit=x", headers=API).status_code == 400
    assert len(authed.get("/api/activity?limit=0", headers=API).json()) == 1


def test_hermes_status_and_install(authed, tmp_path, monkeypatch):
    hh = tmp_path / "hermes"
    hh.mkdir()
    (hh / "config.yaml").write_text("model: gpt\n")
    monkeypatch.setenv("HERMES_HOME", str(hh))
    assert authed.get("/api/hermes", headers=API).json() == {
        "found": True,
        "home": str(hh),
        "mcp_server": False,
        "skill": False,
    }
    r = authed.post("/api/hermes/install", headers=API, json={})
    assert r.json()["mcp_server"] == "added"
    assert audit.read(1)[0] | {"ts": None} == {
        "ts": None,
        "tool": "admin:hermes_install",
        "ok": True,
        "target": {"hermes_home": str(hh)},
    }
    assert hermes.status() == {"found": True, "home": str(hh), "mcp_server": True, "skill": True}


def test_hermes_status_survives_non_utf8_config(tmp_path, monkeypatch):
    hh = tmp_path / "hermes"
    hh.mkdir()
    (hh / "config.yaml").write_bytes(b"model: caf\xe9\n")  # cp1252 e-acute
    monkeypatch.setenv("HERMES_HOME", str(hh))
    assert hermes.status() == {"found": True, "home": str(hh), "mcp_server": False, "skill": False}


def test_hermes_status_unparsable_config(tmp_path, monkeypatch):
    hh = tmp_path / "hermes"
    hh.mkdir()
    (hh / "config.yaml").write_text("model: gpt\n")
    monkeypatch.setenv("HERMES_HOME", str(hh))

    def bad(text):
        raise ValueError("bad yaml")

    monkeypatch.setattr(hermes, "_has_server", bad)
    assert hermes.status()["found"] is True and hermes.status()["mcp_server"] is False


def test_broker_error_is_400_and_crash_is_opaque(authed, monkeypatch):
    def fail():
        raise Broker365Error("Not signed in. Run: broker365 login")

    monkeypatch.setattr(hermes, "status", fail)
    r = authed.get("/api/hermes", headers=API)
    assert r.status_code == 400 and r.json() == {"error": "Not signed in. Run: broker365 login"}

    def crash():
        raise RuntimeError("secret detail C:/Users/x")

    monkeypatch.setattr(hermes, "status", crash)
    r = authed.get("/api/hermes", headers=API)
    assert r.status_code == 500 and r.json() == {"error": "Internal error"}
    assert "secret" not in r.text


def test_audit_read_skips_malformed(tmp_path):
    assert audit.read() == []
    path = config.broker_home() / "audit.log"
    path.write_text('{"tool": "a"}\n[1]\n\n{"tool": "b"}\n')
    assert audit.read() == [{"tool": "b"}, {"tool": "a"}]


def test_cli_ui_prints_url_and_warns_off_loopback(monkeypatch):
    import uvicorn
    from typer.testing import CliRunner

    from broker365 import cli

    runs, opened = [], []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append(kw))
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    result = CliRunner().invoke(cli.app, ["ui", "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:9000/?token=" in result.output and "Ctrl+C" in result.output
    assert opened[0].startswith("http://127.0.0.1:9000/?token=") and "warning" not in result.output
    assert runs[0] == {
        "host": "127.0.0.1",
        "port": 9000,
        "log_level": "warning",
        "access_log": False,
    }
    result = CliRunner().invoke(cli.app, ["ui", "--host", "192.168.1.5", "--no-browser"])
    assert "SSH tunnel" in result.output and len(opened) == 1
    assert "http://192.168.1.5:8365/?token=" in result.output
    for host, shown in (("localhost", "127.0.0.1"), ("::1", "[::1]"), ("fe80::1", "[fe80::1]")):
        result = CliRunner().invoke(cli.app, ["ui", "--host", host, "--no-browser"])
        assert f"http://{shown}:8365/?token=" in result.output, result.output
