"""`broker365` command-line entry point."""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from typing import Any

import typer

from broker365 import __version__, audit, config, providers, services
from broker365.errors import Broker365Error
from broker365.providers import flows

app = typer.Typer(
    no_args_is_help=True,
    help="Connect AI agents (Hermes, any MCP client) to Microsoft 365 and Google accounts.",
    pretty_exceptions_enable=False,
)
hermes_app = typer.Typer(help="Hermes Agent integration.", no_args_is_help=True)
app.add_typer(hermes_app, name="hermes")

ScopesOpt = typer.Option(
    None,
    "--scopes",
    "-s",
    help="Comma-separated bundles (default: "
    + "; ".join(f"{p.name} {','.join(p.default_bundles())}" for p in providers.all())
    + "). "
    + "; ".join(f"{p.name}: {', '.join(p.bundles())}" for p in providers.all()),
)
ProviderOpt = typer.Option(
    None,
    "--provider",
    "-p",
    help="Identity provider: microsoft (default) or google. With --complete: the flow's.",
)
AccountProviderOpt = typer.Option(
    None,
    "--provider",
    "-p",
    help="Identity provider (default: the one the account belongs to, else microsoft).",
)


def _out(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _run(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Broker365Error as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None


def _provider(name: str | None) -> providers.Provider:
    return _run(providers.get, name)


def _usage_error(message: str) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(2)


def _bundles(scopes: str | None, provider: providers.Provider) -> list[str]:
    bundles = [s.strip() for s in (scopes or "").split(",") if s.strip()]
    return _run(providers.choose_bundles, provider.name, bundles)


def _finish_headless(p: providers.Provider, flow: dict[str, Any]) -> None:
    """Show a login_start result by its kind and complete it from this terminal."""
    if flow.get("kind") == "authorize_url":
        typer.echo(flow.get("instructions") or "Open this URL and sign in:")
        typer.echo(flow["authorize_url"])
        redirect_url = typer.prompt("Paste the full address-bar URL you were redirected to")
        _out(_run(providers.login_complete, flow["flow_id"], redirect_url=redirect_url.strip()))
        return
    typer.echo(
        flow.get("message")
        or flow.get("instructions")
        or f"Open {flow['verification_uri']} and enter {flow['user_code']}"
    )
    _out(_run(providers.login_complete, flow["flow_id"]))


@app.command()
def version() -> None:
    """Print version."""
    typer.echo(__version__)


@app.command()
def login(
    scopes: str = ScopesOpt,
    device_code: bool = typer.Option(False, "--device-code", help="Sign in from another device."),
    start: bool = typer.Option(False, "--start", help="Start a remote sign-in: print JSON."),
    complete: str = typer.Option(None, "--complete", help="Finish sign-in flow FLOW_ID."),
    redirect_url: str = typer.Option(
        None, "--redirect-url", help="With --complete: the URL the browser was redirected to."
    ),
    port: int = typer.Option(None, "--port", help="Loopback port for browser sign-in."),
    provider: str | None = ProviderOpt,
) -> None:
    """Sign in to an account (browser, or device code on headless machines)."""
    if redirect_url is not None and not complete:
        _usage_error("--redirect-url only goes with --complete FLOW_ID.")
    if complete:
        if provider is not None:
            flow_provider, _ = _run(flows.load, complete)
            if _provider(provider).name != flow_provider:
                _usage_error(
                    f"Flow {complete} was started with provider '{flow_provider}', "
                    f"not '{provider}'. Leave out --provider."
                )
        _out(_run(providers.login_complete, complete, redirect_url=redirect_url))
        return
    p = _provider(provider)
    bundles = _bundles(scopes, p)
    if start:
        _out(_run(providers.login_start, p.name, bundles))
        return
    if device_code or not _has_browser():
        _finish_headless(p, _run(providers.login_start, p.name, bundles))
        return
    _out(_run(p.login_interactive, bundles, port))


def _has_browser() -> bool:
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False
    try:
        webbrowser.get()
        return True
    except webbrowser.Error:
        return False


@app.command()
def logout(
    account: str = typer.Option(None, "--account", "-a"),
    provider: str = AccountProviderOpt,
) -> None:
    """Remove an account's tokens from this machine."""
    p, username = _run(providers.resolve, account, provider)
    name = _run(p.logout, username)
    typer.echo(f"Signed out {name}. {p.revoke_hint}")


@app.command()
def accounts(
    default: str = typer.Option(None, "--default", help="Set the default account."),
    provider: str = AccountProviderOpt,
) -> None:
    """List signed-in accounts of every provider (optionally set a provider's default)."""
    if default:
        p, username = _run(providers.resolve, default, provider)
        _run(p.set_default, username)
    _out(_run(providers.all_accounts))


@app.command()
def status() -> None:
    """Show sign-in state, token health, and paths (JSON)."""
    from broker365.services.account import account_status

    _out({**account_status(), "client_id": config.client_id()})


@app.command()
def settings(
    read_only: bool = typer.Option(None, "--read-only/--read-write", help="Hide write tools."),
    allow_dir: list[str] = typer.Option(None, "--allow-dir", help="Allow a local directory."),
    disallow_dir: list[str] = typer.Option(None, "--disallow-dir"),
    reset_excel_probe: bool = typer.Option(
        False, "--reset-excel-probe", help="Re-test Excel API support on personal accounts."
    ),
) -> None:
    """View or change persisted settings (config.toml)."""
    changed = config.change_settings(
        read_only, allow_dir or (), disallow_dir or (), reset_excel_probe=reset_excel_probe
    )
    if changed:
        audit.record_admin("settings", changed)
    _out(config.settings_view())


@app.command("tools")
def list_tools() -> None:
    """List the tools available to agents."""
    for t in services.tool_catalog():
        if t["hidden"]:
            continue
        flag = " [destructive]" if t["destructive"] else " [write]" if t["writes"] else ""
        typer.echo(f"{t['name']:<24} {t['description']}{flag}")


@app.command()
def call(
    tool: str,
    args: str = typer.Argument("{}", help="JSON object of arguments"),
) -> None:
    """Run a tool directly, e.g. broker365 call files_list '{"path": "/Documents"}'."""
    try:
        kwargs = json.loads(args)
    except json.JSONDecodeError as exc:
        typer.echo(f"error: arguments must be a JSON object ({exc})", err=True)
        raise typer.Exit(2) from None
    if not isinstance(kwargs, dict):
        typer.echo("error: arguments must be a JSON object", err=True)
        raise typer.Exit(2)
    _out(_run(services.call, tool, **kwargs))


@app.command("admin-consent-url")
def admin_consent_url(tenant: str = typer.Argument(..., help="Tenant domain or ID")) -> None:
    """URL a tenant admin opens to approve broker365 for their whole organisation."""
    typer.echo(
        f"https://login.microsoftonline.com/{tenant}/adminconsent?client_id={config.client_id()}"
    )


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind (keep loopback)."),
    port: int = typer.Option(8365, "--port"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Only print the URL."),
) -> None:
    """Open the local web dashboard (accounts, sign-in, settings, Hermes, activity)."""
    import secrets

    import uvicorn

    from broker365.ui.server import LOOPBACK, create_app, url_host

    token = secrets.token_urlsafe(32)
    if host not in LOOPBACK:
        typer.echo(
            f"warning: binding to {host}. The dashboard controls your Microsoft account; "
            "only reach it through an SSH tunnel (ssh -L 8365:127.0.0.1:8365 ...).",
            err=True,
        )
    url = f"http://{url_host(host)}:{port}/?token={token}"
    typer.echo(f"broker365 dashboard: {url}")
    typer.echo("Press Ctrl+C to stop.")
    if not no_browser:
        webbrowser.open(url)
    uvicorn.run(
        create_app(token, port, host=host),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,  # access lines would include the ?token= query
    )


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (for Hermes / other MCP clients)."""
    from broker365 import mcp_server

    mcp_server.run()


@hermes_app.command("install")
def hermes_install(
    hermes_home: str = typer.Option(None, "--hermes-home"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Register the m365 MCP server in Hermes config.yaml and install the skill."""
    from broker365 import hermes

    _out(_run(hermes.install, hermes_home, dry_run))
