# CLAUDE.md

## What this is
**broker365**: a local credential broker plus MCP server. It lets a Hermes AI agent (and any other MCP client) work with a user's Microsoft 365 or personal Microsoft account (OneDrive, Excel, Word, PowerPoint, Outlook, Calendar…) through Microsoft Graph. End users never touch Azure. They run `broker365 login` and sign in once.

The full design, Azure setup and milestones are in **[docs/PLAN.md](docs/PLAN.md)**. Read it before making non-trivial changes and keep it current when the design changes.

## Non-negotiable design rules
- **One multi-tenant *public client* app registration**, authority `https://login.microsoftonline.com/common`. **Never** add a client secret or certificate, and never use application (app-only) permissions. Everything is delegated.
- **Tokens stay on the user's machine.** There is no hosted backend. Never log tokens, auth codes or file contents.
- **Incremental consent**: request only the scope bundles a feature needs (`config.SCOPE_BUNDLES`). If a bundle is missing, raise `auth.ConsentRequired` and don't silently re-prompt.
- Service functions in `services/` are plain sync functions that return JSON-serialisable values, registered with `@tool(bundle, writes=..., destructive=...)`. The CLI (`broker365 call`) and the MCP server both expose the same registry, so don't put Graph logic in `cli.py` or `mcp_server.py`.
- Flags: `writes=True` for anything that changes data (hidden in read-only mode). Users can also block single tools (`blocked_tools` in config.toml): `services.available_tools` hides them and `services.call` raises `ToolBlocked`, so every new tool is blockable for free. `destructive=True` for delete, send, share and invite. In-place file edits are `writes` only, because the eTag guard already prevents silent overwrites.
- Raise `Broker365Error` subclasses with user-facing messages. The MCP wrapper turns them into `ToolError`, and any other exception text is hidden from the agent.
- Local filesystem access always goes through `config.resolve_local()`, the workspace sandbox.
- Mail send/reply/forward create a draft unless `confirm=True`. Approved drafts are sent with `mail_send_draft`, so no orphan drafts are left.
- Graph has no edit API for Word or PowerPoint. Round-trip them with python-docx/python-pptx (download, edit, upload with `If-Match` eTag).
- OneDrive file APIs fully support personal accounts. The Excel Workbook API (`.xlsx` only) works for business **and** personal accounts, including `createSession` (verified live, Oct 2026, despite the docs). Every Workbook tool call runs in its own session (create, work, close). Writes use a persistent session. Reads use a non-persistent one, because a persistent session saves recalculated NOW()/RAND() and changes the eTag. The exception is a read within 30 s of this process writing the same workbook: it uses a persistent session, since only that sees the fresh write (all measured live). Uploads wait out the ~20 s overwrite lock (423) that any workbook write leaves behind. Personal accounts still get a per-account capability probe with an openpyxl round-trip fallback behind the same tool signatures. See docs/PLAN.md §5 and §8.

## Layout
```
src/broker365/
  config.py       client ID, scope bundles, broker_home(), config.toml settings, local-path sandbox
  errors.py       Broker365Error hierarchy (ConsentRequired, ReauthRequired, GraphError, ConflictError…)
  auth.py         MSAL PublicClientApplication, encrypted token cache, interactive + device-code login
  providers/      identity-provider registry (`get`, `all`, `catalog`, `resolve`, `validate_bundles`,
                  `login_start` (default bundles), flow dispatch) + `Provider` protocol (redirect
                  sign-in via `callback_path` + `complete_callback`); callback.py = the static
                  OAuth landing page + its headers; microsoft.py adapts auth.py; flows.py stores
                  pending sign-in flows for every provider. Defaults per provider in config.toml
                  `default_accounts` (legacy `default_account` = Microsoft's, migrated on write)
                  google.py: Google OAuth (loopback / dashboard callback / paste-back, PKCE,
                  state `<flow_id>.<secret>`), encrypted google_tokens.bin, refresh, revoke
  transport.py    provider-neutral HTTP bits: pooled no-redirect client, sleep hook, retry_delay,
                  retry limits, seg, JsonVerbs (get/post/patch over request)
  graph.py        httpx Graph client: paging, Retry-After, redirects without token, upload sessions
  gapi.py         httpx Google API client: exact *.googleapis.com host allow-list, 401 refresh-once,
                  rate-limit/5xx retries, nextPageToken paging, multipart/resumable Drive uploads
  services/       tool registry (__init__) + account, files, excel, word, powerpoint, mail, calendar, contacts, tasks, sharepoint, onenote,
                  gdrive (Google Drive: @tool("gdrive", "google"), items by Drive id or path),
                  gsheets / gdocs / gslides (Sheets v4, Docs v1, Slides v1; files found and
                  created through gdrive's public helpers; _gnative.Native = per-product
                  client/url/resolve/revision-guarded update). @tool(requires=...) = other
                  bundles checked with Provider.has_consent before the tool runs;
                  gmail / gcalendar / gtasks / gcontacts (Gmail v1 raw RFC 5322, draft-first
                  sends; Calendar v3; Tasks v1; People v1); _mail.py = mail policy shared by
                  mail.py and gmail.py (recipients, 3 MB attachments, draft result)
  cli.py          Typer app, entry point `broker365`
  mcp_server.py   MCP stdio server (`broker365 mcp`)
  audit.py        JSON-lines audit.log of write tools and admin:* actions (targets only, never content;
                  core TARGET_KEYS + per-tool extras from @tool(..., audit=(...)))
  ui/             `broker365 ui` local dashboard: server.py (Starlette; all access checks in the `guard`
                  middleware: Host check, per-run token -> HttpOnly SameSite=Strict cookie, X-Broker365
                  header on /api) + static/ (vanilla JS, strict CSP, textContent only)
  hermes.py       `broker365 hermes install`: config.yaml mcp_servers entry + skill copy
  hermes_skill/   SKILL.md installed into $HERMES_HOME/skills/productivity/microsoft-365
tests/            pytest; Graph mocked with pytest-httpx; test_mcp_stdio spawns the real server
docs/PLAN.md      architecture and milestones; docs/AZURE_SETUP.md = publisher app registration
```

## Commands
```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows Git Bash; use .venv/bin/activate on Linux/macOS
pip install -e ".[dev]"
pytest
ruff check . && ruff format .
broker365 status           # paths, accounts, token health
broker365 tools            # registered tools
# releasing: docs/RELEASING.md
broker365 call files_list '{"path": "/"}'
```

## Environment
- `BROKER365_CLIENT_ID` overrides the built-in client ID (bring-your-own app registration).
- `BROKER365_GOOGLE_CLIENT_ID` / `BROKER365_GOOGLE_CLIENT_SECRET` override the built-in Google Desktop client (docs/GOOGLE_PLAN.md).
- Broker home resolution: `BROKER365_HOME` → `$HERMES_HOME/broker365` → `~/.broker365`.
- Other env vars: `BROKER365_ACCOUNT`, `BROKER365_READ_ONLY`, `BROKER365_APP_FOLDER`, `BROKER365_WORKSPACE`, `BROKER365_ALLOWED_DIRS`, `BROKER365_PLAINTEXT_CACHE` (tests/CI only).
- Hermes passes MCP servers a **filtered environment**, so `hermes install` writes `BROKER365_HOME` (and the client ID, if set) into the server's `env:` block.
- Hermes on the dev machine: v0.21.2, `HERMES_HOME=%LOCALAPPDATA%\hermes`. MCP servers are configured under `mcp_servers:` in `$HERMES_HOME/config.yaml`, and their tools register as `mcp_<server>_<tool>`. The server key we use is `m365`.

## Conventions
- Python 3.11+, type hints everywhere, ruff (line length 100).
- Cross-platform: Windows, macOS and headless Linux VPS. Use `pathlib`, no shell-specific behaviour, and device-code login must always work without a browser.
