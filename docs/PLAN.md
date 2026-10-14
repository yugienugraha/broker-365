# broker-365 — Implementation Plan

> Goal: let a Hermes agent user connect their Microsoft 365 / personal Microsoft account (OneDrive, Excel, Word, Outlook, Calendar, …) in **one command and one browser sign-in**, with **no Azure setup on the user's side**.

---

## 1. Problem & key insight

With Google Workspace today, every user must create their own GCP project, OAuth client, consent screen, and enable APIs. That is the friction we want to remove.

Both reference projects keep that friction for Microsoft:

| Repo | Auth model | What the user must do | Coverage |
|---|---|---|---|
| [Andrew-Girgis/microsoft-workspace-skill](https://github.com/Andrew-Girgis/microsoft-workspace-skill) | MSAL, delegated, auth-code + paste redirect URL, **client secret** | Register own Azure app, create secret, add scopes | Mail, Calendar, Contacts (no OneDrive/Excel/Word) |
| [fabiomotta0311/hermes-microsoft365-plugin](https://github.com/fabiomotta0311/hermes-microsoft365-plugin) | **Application** (app-only) mode only, client secret | Tenant admin creates app + grants app permissions | Outlook, OneDrive, SharePoint, Teams, To Do, Planner (pre-release, untested) |

**Key insight:** Microsoft identity platform supports a **multi-tenant *public client* app registration** (no client secret). A single app registration, owned by *you* (the publisher), can be shipped inside broker-365. Any user — personal Outlook.com/Hotmail or any work/school tenant — signs in with **delegated** permissions, and the tokens live **only on the user's machine/VPS**. Because it's a public client, the client ID is not a secret and is safe to embed in open-source code (same pattern as Azure CLI, GitHub CLI, rclone).

So the "broker" is not a hosted server that holds everyone's tokens — it is a **local credential broker**: one shared app identity, per-user token caches stored locally.

---

## 1a. Google Workspace APIs vs Microsoft Graph

The current Hermes `google-workspace` skill is the baseline the user knows, so this is what changes when moving to Microsoft.

### Document editing (the key difference)
| Capability | Google Workspace | Microsoft Graph | broker365 approach |
|---|---|---|---|
| **Spreadsheet** content API | ✅ **Sheets API**: values get/update/append/batchGet, `batchUpdate` (formatting, charts, pivots, named ranges, conditional formats) | ✅ **Excel Workbook API**: ranges, formulas, tables (sort/filter), charts, named items, 300+ functions, sessions | Workbook API, with openpyxl round-trip as fallback |
| Spreadsheet: personal accounts | ✅ Works for consumer Gmail | ⚠️ **Inconsistent**. Docs contradict each other: range calls list personal as supported, sessions don't, and the overview says consumer isn't supported | Per-account probe: sessionless Workbook API if it works, otherwise openpyxl |
| Files on personal accounts | ✅ Drive API | ✅ OneDrive API fully supports personal accounts | — |
| Spreadsheet: file formats | Native Google Sheets only (`.xlsx` must be converted to Sheets first) | `.xlsx` only (no `.xls`) | Convert `.xls` locally if needed |
| **Document** content API | ✅ **Docs API**: `documents.get` (structured JSON), `batchUpdate` (insertText, replaceAllText, tables, images, styles) | ❌ **None**. Files only | python-docx round-trip |
| **Presentation** content API | ✅ **Slides API**: `presentations.get`, `batchUpdate` (createSlide, insertText, replaceAllText, images, shapes, tables) | ❌ **None**. Files only | python-pptx round-trip |
| Edits on the live file | ✅ Server-side, real-time collaboration-safe | Excel (business): ✅ server-side. Word/PPT/personal Excel: download, edit, re-upload | `If-Match` eTag; on 412 report a conflict |
| Template fill | Docs/Slides `replaceAllText` | No API, done locally | `word_fill_template`, `ppt_replace_text` |
| Comments on documents | ✅ Drive Comments API (Docs/Sheets/Slides) | ❌ Not exposed for Word/PPT | Read from the downloaded file only |

### Files, auth, and the rest
| Capability | Google Workspace | Microsoft Graph | Notes |
|---|---|---|---|
| File storage API | Drive API v3 (`files`, `permissions`, `revisions`) | OneDrive/SharePoint `drive` + `driveItem` | Roughly equivalent |
| Path addressing | ❌ IDs only (search by name/parent) | ✅ `/me/drive/root:/path/to/file` | Simpler for agents on Microsoft |
| Large upload | Resumable upload | `createUploadSession` (320 KiB-multiple chunks) | Equivalent |
| Export / convert | `files.export` for native docs → pdf/docx/xlsx/pptx/txt/html (10 MB cap) | `content?format=pdf` / `html` from Office files | Both OK for PDF |
| Sharing | `permissions.create` | `createLink`, `invite` | Equivalent |
| Batch requests | Per-document `batchUpdate` (atomic) + HTTP batch | `$batch`, up to 20 requests (not atomic) | |
| Change tracking | `changes.list` + push notifications | `delta` + webhook subscriptions | Equivalent |
| Mail / Calendar / Contacts / Tasks | Gmail, Calendar, People, Tasks APIs (separate APIs) | One endpoint: `/me/messages`, `/me/events`, `/me/contacts`, `/me/todo` | Graph is one base URL and one token |
| **App registration for users** | Each user creates a GCP project, OAuth client and consent screen, and enables each API | **Publisher registers once**, users just sign in (multi-tenant public client) | Main reason this broker exists |
| Publishing a shared app publicly | `drive`/`gmail` full scopes are **restricted** and need Google verification **plus an annual CASA security assessment**; unverified apps are capped at 100 users | Publisher verification (free, needs a Partner Network ID). No security audit for delegated scopes | Much lighter on Microsoft |
| Refresh token life | **7 days** while the OAuth app is in "Testing" status; long-lived once published | ~90-day sliding window | Fewer re-logins on Microsoft |
| Headless/VPS login | Loopback or manual paste of the redirect URL (no device flow for Workspace scopes) | ✅ Device code flow | Better for the Hermes gateway on a VPS |
| Org admin control | Workspace admin can block third-party apps | Tenant can block user consent to unverified apps | Both may need admin approval at work |
| Python client | `google-api-python-client` | `msgraph-sdk` (large) or plain REST | We use plain `httpx` |

**What this means for Hermes users:** setup is easier on Microsoft, with no cloud console and device-code login. Document editing is weaker for Word and PowerPoint (no server-side API) and for Excel on personal accounts. broker365 hides the difference behind the same tool names, but the tool descriptions must say when an operation is a download, edit and re-upload.

## 2. Recommendation: CLI-first + MCP server (web UI optional later)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **CLI** (`broker365 login`) | Works on headless VPS via device code; trivial to script; same UX on Windows/macOS/Linux | Less friendly for non-technical users | ✅ **Primary** |
| **MCP server** (`broker365 mcp`) | Hermes speaks MCP natively → tools appear in the agent automatically; also works with Claude Desktop/Code, Cursor, etc. | — | ✅ **Primary agent interface** |
| Local web UI (`broker365 ui` on `127.0.0.1`) | Nice dashboard: connected accounts, scopes, revoke | Extra surface area; on VPS needs SSH tunnel | ⏳ Phase 4, optional |
| Hosted web broker (central server holding tokens) | Zero install | You become custodian of every user's refresh tokens → security, compliance, cost burden | ❌ Avoid |

**Language: Python 3.11+** — Hermes Agent is Python, MSAL Python is first-party, and both reference repos are Python, so contributors and users already have the toolchain.

### Auth flows by environment
- **Desktop (Windows / Mac mini):** `acquire_token_interactive` — opens browser, loopback redirect `http://localhost`, PKCE. No copy-paste.
- **VPS / headless / SSH:** `initiate_device_flow` + `acquire_token_by_device_flow` — prints `https://microsoft.com/devicelogin` + code; user signs in from any device (phone).
- Auto-detect: if no `DISPLAY`/browser available or `--device-code` passed → device code.
- Refresh: `acquire_token_silent` with persisted cache (`offline_access` gives refresh tokens, ~90-day sliding lifetime).

---

## 3. Azure setup (one-time, done by **you**, the publisher)

You do this once. End users never touch Azure.

### 3.1 Create the app registration
1. Sign in to <https://entra.microsoft.com> (Microsoft Entra admin center) with an account that has a tenant. A free Azure account / M365 developer tenant works.
2. **Identity → Applications → App registrations → New registration**
   - Name: `broker-365` (this is what users see on the consent screen — make it trustworthy, e.g. `Hermes 365 Connector`).
   - Supported account types: **"Accounts in any organizational directory (Any Microsoft Entra ID tenant – Multitenant) and personal Microsoft accounts (e.g. Skype, Xbox)"**. → authority `https://login.microsoftonline.com/common`.
   - Redirect URI: platform **"Public client/native (mobile & desktop)"** → `http://localhost`.
3. Copy the **Application (client) ID** → this becomes `DEFAULT_CLIENT_ID` in `src/broker365/config.py`. Tenant ID is **not** needed (we use `common`).

### 3.2 Authentication blade
- **Advanced settings → Allow public client flows: Yes** (required for device code flow).
- Ensure redirect `http://localhost` is listed under *Mobile and desktop applications*. (MSAL also accepts any port on loopback.)
- Do **not** create a client secret or certificate. A public client must not have one shipped.

### 3.3 API permissions (Microsoft Graph → Delegated)
Add these; we request them **incrementally** at runtime, so the user only consents to what a feature needs.

| Bundle | Scopes | Admin consent needed in orgs? |
|---|---|---|
| core (always) | `openid`, `profile`, `offline_access`, `User.Read` | No |
| files (OneDrive, Excel, Word, PowerPoint) | `Files.ReadWrite` (own drive). Optional `Files.ReadWrite.All` for files shared with the user | No (`.All` also No for delegated) |
| sharepoint | `Sites.ReadWrite.All` | No for delegated (tenant policy may differ) |
| mail | `Mail.ReadWrite`, `Mail.Send` | No |
| calendar | `Calendars.ReadWrite` | No |
| contacts | `Contacts.ReadWrite` | No |
| tasks (To Do) | `Tasks.ReadWrite` | No |
| onenote | `Notes.ReadWrite` | No |
| teams (work accounts only) | `Chat.ReadWrite`, `ChannelMessage.Send`, `Team.ReadBasic.All` | Some require admin |

Do **not** click "Grant admin consent" in your own tenant expecting it to apply to users — consent is per tenant. Users consent at first sign-in.

### 3.4 Branding & publisher verification (strongly recommended before public release)
- **Branding & properties:** logo, home page URL, terms of service URL, privacy statement URL (GitHub pages are fine).
- **Publisher verification:** link a **Microsoft Partner Network (MPN / Cloud Partner Program) ID** and a verified domain. Without it:
  - Consent screen shows **"unverified"**.
  - Many work/school tenants **block user consent to unverified multi-tenant apps** (default since 2020) → users see "Need admin approval".
  - Personal Microsoft accounts are unaffected.
- Document the fallback for blocked tenants (see §7): admin consent URL, or "bring your own client ID".

### 3.5 What NOT to configure
- No Application permissions (app-only) — they'd require each tenant's admin and give org-wide access; wrong model for a personal agent.
- No client secret / certificate.
- No Web/SPA redirect platforms.

---

## 4. Architecture

```
┌──────────────────────── user's machine / VPS ───────────────────────────┐
│                                                                          │
│  Hermes Agent ──MCP (stdio)──► broker365 mcp ──┐                         │
│        │                                        │                         │
│        └──(or skill shell-out)─► broker365 CLI ─┤                         │
│                                                 ▼                         │
│                                   ┌─────────────────────────┐            │
│                                   │ auth/  (MSAL Public      │            │
│                                   │  ClientApplication,      │            │
│                                   │  authority=common)       │            │
│                                   │ token cache: encrypted   │──► login.microsoftonline.com
│                                   │  (OS keychain / DPAPI /  │            │
│                                   │   file+Fernet on VPS)    │            │
│                                   └──────────┬──────────────┘            │
│                                              ▼                            │
│                                   graph/  (httpx client: retry, 429      │
│                                    Retry-After, paging, upload sessions) │──► graph.microsoft.com
│                                              ▼                            │
│                                   services/ onedrive · excel · word ·    │
│                                    mail · calendar · …                    │
└──────────────────────────────────────────────────────────────────────────┘
```

### Components
- **`broker365.auth`** — MSAL `PublicClientApplication`, `authority="https://login.microsoftonline.com/common"`. Multiple accounts (profiles: `personal`, `work`…). Incremental consent: when a tool needs a scope bundle not yet granted, raise `ConsentRequired` with a message telling the user to run `broker365 login --scopes files`.
- **Token cache** — `msal-extensions` `PersistedTokenCache`: DPAPI on Windows, Keychain on macOS, libsecret on Linux desktop; on headless Linux fall back to a file at `~/.broker365/token_cache.bin` with `chmod 600` (optionally encrypted with a passphrase from env `BROKER365_CACHE_KEY`). Never logs tokens.
- **`broker365.graph`** — thin `httpx` wrapper (not `msgraph-sdk`: it is huge and async-generated; we need ~40 endpoints). Handles `@odata.nextLink` paging, 429/503 with `Retry-After`, `$batch` later.
- **`broker365.services`** — one module per product, plain functions returning dicts. Shared by CLI and MCP.
- **`broker365.cli`** — Typer. `login`, `logout`, `status`, `accounts`, plus service subcommands (`files ls`, `excel read`, …) with `--json` output.
- **`broker365.mcp_server`** — official `mcp` Python SDK (FastMCP), stdio transport. Each service function exposed as an MCP tool with a JSON schema. Write/destructive tools are marked so Hermes can require approval.

### Config locations
Broker home (see §5a for resolution order; default `~/.broker365/`):
- `config.toml` — client_id override, default account, enabled bundles, read-only mode
- `token_cache.bin` — MSAL cache (when no OS keychain)
- `audit.log` — one line per write operation (tool, target item id, timestamp) — no content

---

## 5. Feature scope (Graph endpoints)

### Phase 1 — Files (OneDrive), the core ask
| Tool | Endpoint |
|---|---|
| `files_list(path)` | `GET /me/drive/root:/{path}:/children` |
| `files_search(q)` | `GET /me/drive/root/search(q='…')` |
| `files_get_meta(id/path)` | `GET /me/drive/items/{id}` |
| `files_download(id, dest)` | `GET /me/drive/items/{id}/content` |
| `files_upload(local, remote)` | `PUT …/content` (<4 MB) or `createUploadSession` (chunks multiple of 320 KiB, <60 MiB/request) |
| `files_mkdir`, `files_move`, `files_copy`, `files_delete` | `POST /children`, `PATCH`, `POST /copy`, `DELETE` |
| `files_share(id, type, scope)` | `POST /me/drive/items/{id}/createLink` |
| `files_convert(id, fmt)` | `GET /me/drive/items/{id}/content?format=pdf` (≤2 MB source) |

### What Graph actually offers per Office app (verified against Microsoft Learn, Oct 2026)

| | Excel | Word | PowerPoint |
|---|---|---|---|
| File ops via OneDrive `driveItem` (list, upload, download, move, copy, share, versions, thumbnails, preview, checkin/checkout) | ✅ | ✅ | ✅ |
| Convert via `content?format=pdf` / `html` | ✅ | ✅ | ✅ |
| **Cell/paragraph/slide-level API in Graph** | ✅ **Workbook API**: worksheets, ranges (values, formulas, numberFormat, sort), tables (rows, columns, sort, filter), charts (add, setData, image as base64), named items, 300+ Excel functions, sessions | ❌ none | ❌ none |
| Formats | `.xlsx` only (no `.xls`) | — | — |
| Account types | Business: ✅ fully supported. Personal: ⚠️ **inconsistent** (see note below) | — | — |
| Our strategy | Workbook API for business. Personal accounts **probe the Workbook API first** (sessionless) and fall back to an **openpyxl round-trip** | python-docx round-trip | python-pptx round-trip |

> **Personal-account Excel. Microsoft's docs contradict each other (checked Oct 2026):**
> - The [OneDrive files API](https://learn.microsoft.com/en-us/graph/api/resources/onedrive?view=graph-rest-1.0) fully supports personal OneDrive. All file operations work.
> - The [Excel overview](https://learn.microsoft.com/en-us/graph/api/resources/excel) says "Support for workbooks stored in OneDrive Consumer platform is still not available".
> - [range-get](https://learn.microsoft.com/en-us/graph/api/range-get?view=graph-rest-1.0) lists "Delegated (personal Microsoft account): Files.ReadWrite" ✅.
> - [createSession](https://learn.microsoft.com/en-us/graph/api/workbook-createsession?view=graph-rest-1.0) lists personal as "Not supported" ❌.
> - A [Microsoft Q&A thread from March 2026](https://learn.microsoft.com/en-us/answers/questions/5835926/microsoft-graph-api-workbook-paths-for-excel-store) says it's "not reliable on personal plans" and that there's no roadmap.
>
> **Decision:** don't hard-code it. On first Excel use per account, run a capability probe (`GET …/workbook/worksheets` without a session). If it succeeds, use the Workbook API **sessionless** (no `createSession` on personal accounts). If it fails, or any later call returns 4xx/5xx `NotSupported`, switch that account to the openpyxl backend. Cache the result in `config.toml`, with `broker365 excel probe --reset` to re-test. Verify against a real personal account as the first task of M3.

"Round-trip" means: `GET /content` → edit locally → `PUT /content` with `If-Match: {eTag}`, so a file someone else changed meanwhile isn't overwritten (412 → re-download and retry, or report a conflict). Comments and track-changes in Word/PowerPoint aren't exposed by Graph, but python-docx can read them from the downloaded file.

### Phase 2 — Excel (`Files.ReadWrite`, delegated)
Backend chosen per account: `WorkbookBackend` (Graph) for business, and for personal accounts when the capability probe passes (sessionless mode). `OpenpyxlBackend` (download, edit, upload) when the probe fails or a call returns `NotSupported`. Same tool signatures for both.
- `excel_create(path, sheets?)`: generate `.xlsx` with openpyxl and upload. Works for both account types.
- `excel_list_sheets`, `excel_add_sheet`, `excel_rename_sheet`, `excel_delete_sheet`
- `excel_read_range(item, sheet, address)`: `GET …/worksheets/{s}/range(address='A1:C10')` (returns values, formulas and text)
- `excel_write_range(item, sheet, address, values|formulas|numberFormat)`: `PATCH …/range(address=…)`. `null` cells are left unchanged, and `""` clears a cell.
- `excel_used_range(item, sheet)`: `GET …/worksheets/{s}/usedRange`
- `excel_table_create / add_rows / list_rows / sort / filter`
- `excel_chart_add(type, sourceData)`, `excel_chart_image` (business only)
- `excel_calc(function, args)`: `POST …/workbook/functions/{name}` (business only)
- Session handling: `createSession {persistChanges:true}` → `workbook-session-id` header. Persistent sessions expire after about 5 minutes idle, so on a 404 we recreate the session. Large ranges are split into chunks, and unbounded ranges (`A:B`) are rejected client-side.
- Worksheet and chart IDs contain `{}`, so always URL-encode them, or address worksheets by name.

### Phase 3 — Word / PowerPoint (round-trip only)
- **Word:** `word_create(path, markdown)` builds a `.docx` with python-docx (headings, lists, tables, images). `word_read(item)` returns structured text (paragraphs with styles, tables). `word_append`, `word_replace_text`, `word_fill_template({{placeholders}})`, `word_export_pdf` (`?format=pdf`).
- **PowerPoint:** `ppt_create(path, slides[])` builds from an outline with python-pptx (an optional template `.pptx` from OneDrive can supply the theme). `ppt_read(item)` returns per-slide title, text and notes. `ppt_add_slide`, `ppt_replace_text`, `ppt_export_pdf`.
- Always upload with `If-Match` eTag. Files over 4 MB use an upload session.
- Fidelity caveat: python-docx/pptx don't render, and complex features (SmartArt, embedded OLE objects, content controls) are preserved but can't be edited. Keep this in the tool descriptions so the agent doesn't over-promise.

### Phase 4 — Outlook & extras (parity with the Google setup)
Mail (list/search/read/draft/send/reply), Calendar (list/create/update/free-busy), Contacts, To Do, OneNote, SharePoint sites (`/sites/{id}/drive`). Optional local web UI.

---

## 5a. Hermes integration (verified against local install)

Observed on the dev machine: Hermes Agent **v0.21.2**, `HERMES_HOME = %LOCALAPPDATA%\hermes` (on Linux/macOS/VPS default `~/.hermes`), skills under `$HERMES_HOME/skills/<category>/<name>/SKILL.md`, the Google integration is the `google-workspace` skill (SKILL.md + `scripts/setup.py` + `scripts/google_api.py`, token files in `HERMES_HOME`). Hermes has a built-in MCP client (`native-mcp`) that reads `mcp_servers:` from `$HERMES_HOME/config.yaml` and registers tools as `mcp_<server>_<tool>`.

We ship **both** integration paths:

1. **MCP (primary)** — add to `$HERMES_HOME/config.yaml`:
   ```yaml
   mcp_servers:
     m365:
       command: "broker365"
       args: ["mcp"]
   ```
   Tools appear as `mcp_m365_files_list`, `mcp_m365_excel_write_range`, … in every Hermes platform (CLI, Telegram, Discord). `broker365 hermes install` writes this block for the user (idempotent, backs up config first).
2. **Skill (companion)** — `hermes/skill/microsoft-365/SKILL.md` installed to `$HERMES_HOME/skills/productivity/microsoft-365/`. Mirrors the google-workspace skill's style: tells the agent how to check status (`broker365 status --json`), drive login non-interactively over chat (device code: show the URL + code to the user, then poll `broker365 login --device-code --wait`), and workflow tips (Excel sessions, Word round-trip). Also lets the agent fall back to the CLI if MCP isn't enabled.

Home dir resolution: `BROKER365_HOME` → else `$HERMES_HOME/broker365` if `HERMES_HOME` is set (keeps Hermes profiles isolated, like `google_token.json`) → else `~/.broker365`.

Device code over chat: since Hermes often runs as a gateway (Telegram/Discord) on a VPS, `login --device-code --start` returns `{verification_uri, user_code, flow_id}` immediately and `login --complete <flow_id>` blocks until done. The agent relays the code to the user in chat — the same non-interactive, step-driven UX as the Google skill, but without any GCP/Azure setup.

## 6. Security model
- Delegated only → the agent can never do more than the signed-in user can.
- Tokens never leave the machine; nothing is sent to a broker-365 server (there isn't one).
- Least privilege: scope bundles opt-in; `read_only = true` config hides all write tools.
- Write tools (`send`, `delete`, `share`, overwrite) flagged as destructive in MCP annotations; `mail_send` defaults to draft unless `confirm=true`.
- `logout` removes accounts from the cache; doc link to <https://myapps.microsoft.com> / <https://account.live.com/consent/Manage> for full revocation.
- Path-traversal guard on local download/upload paths.

## 7. Risks & mitigations
| Risk | Mitigation |
|---|---|
| Org tenant blocks user consent to unverified apps | Publisher verification (§3.4); `broker365 admin-consent-url` prints `https://login.microsoftonline.com/{tenant}/adminconsent?client_id=…` for their admin; `BROKER365_CLIENT_ID` env to bring their own registration |
| Conditional Access blocks device code flow | Prefer interactive loopback on desktop; on VPS document SSH port-forward (`ssh -L 8400:localhost:8400`) + `--port 8400` |
| Refresh token expiry (90-day inactivity, password change) | `status` shows token health; tools return clear `ReauthRequired` error with exact command |
| Graph throttling (429) | Retry with `Retry-After`, exponential backoff, cap |
| Excel Workbook API support on personal OneDrive is contradictory in the docs and reported unreliable | Per-account capability probe: sessionless Workbook API when it works, openpyxl round-trip otherwise. Live test with a personal account is the first task of M3 |
| Round-trip edits race with a human editing the same file | `If-Match` eTag on upload. On 412, report a conflict instead of overwriting |
| Personal accounts lack Teams/SharePoint/Planner, and Excel Workbook API support on them is unreliable | Detect account type (`tid == 9188040d-6c67-4c5b-b112-36a304b66dad`) and hide unsupported tools |

## 8. Milestones
Status as of 2026-10-14. ✅ = implemented with mocked-Graph tests **and** smoke-tested live against a personal Microsoft account through the real app registration (`5b97899a-…`): OneDrive files, Excel, Word, PowerPoint, PDF conversion, calendar, To Do, mail (read-only) and OneNote, also through Hermes's own MCP client. Work/school-only features (SharePoint, free/busy, Teams links) are not yet tested live.

**Live findings (personal account, Oct 2026):** the Excel Workbook API works on personal OneDrive, including `createSession`, which contradicts Microsoft's docs. Sessionless reads of a just-written workbook lag 3–6 s, so every Workbook tool call opens its own persistent session and closes it at the end, falling back to sessionless if `createSession` is refused. A cross-call session cache was tried and dropped: an open session keeps the file locked against overwrite (423) for its whole ~5 min lifetime. Any workbook write locks the file for about 20 s, so uploads (`create_bytes`/`replace_bytes`) retry a 423 for up to 60 s. The openpyxl fallback remains for accounts where the probe fails. Folder creation can return transient 504s; it is retried safely because `conflictBehavior=fail` makes it idempotent.
1. ✅ **M0 Scaffold**
2. ✅ **M1 Auth:** interactive and device-code login, start/complete split for chat (`account_login_*` tools), `status`, `logout`, `accounts --default`, encrypted cache. A live sign-in is still needed.
3. ✅ **M2 OneDrive tools** in CLI and MCP, plus `broker365 hermes install`. Verified against Hermes's own MCP client (mcp 2.0.0) over stdio.
4. ✅ **M3 Excel:** dual backend with a per-account probe. Live probe against a personal account is still pending.
5. ✅ **M4 Word/PowerPoint:** python-docx/pptx round-trip with an eTag guard.
6. ✅ **M5 Outlook/Calendar/Contacts/To Do, SharePoint, OneNote**, read-only mode and the `audit.log` of write operations.
7. ⏳ **M6 Release:** ✅ Entra app created and `DEFAULT_CLIENT_ID` set. Still to do: publisher verification (needs an MPN ID; until then the consent screen says "unverified" and many work/school tenants block user consent), PyPI release, and an optional web UI.

## 9. Open questions for the owner
- App display name / publisher domain for the consent screen?
- Do you have (or want) an MPN ID for publisher verification?
- Need SharePoint/Teams in v1 or personal OneDrive only?
