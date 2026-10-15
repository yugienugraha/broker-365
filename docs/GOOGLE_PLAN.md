# Google Workspace connection: implementation plan

> Goal: add **Google Workspace / Gmail accounts** to broker365 as a second connection type next to
> Microsoft 365, with the same approach: one publisher-owned app registration, the user only
> signs in, tokens stay local, and Hermes gets tools through the same MCP server, CLI and
> dashboard.
>
> Status: G0–G4 done (2026-10-15): sign-in, token storage/refresh and revoke, Drive, Sheets
> and Docs tools (live smoke passed; Slides awaits consent). G5 (Gmail, Calendar, Tasks,
> Contacts) implemented with mocked tests; live smoke pending.
> Facts below were checked against Google's current docs via
> Context7 (Identity OAuth 2.0, Drive, Gmail and Sheets API docs, google-auth-library-python).

---

## 1. What Google's platform allows (and how it differs from Microsoft)

| Topic | Microsoft (today) | Google (verified in docs) | Consequence for broker365 |
|---|---|---|---|
| App identity | Multi-tenant *public client*, client ID only | **Desktop app** OAuth client: client ID + client secret. Google: *"the client secret is obviously not treated as a secret"* for installed apps | Build the client ID + secret in, like `DEFAULT_CLIENT_ID`. The repo is private; Google still advises against public repos, which we comply with |
| Sign-in on desktop | Interactive (loopback) | **Loopback IP redirect** `http://127.0.0.1:<port>` + **PKCE** (still supported for Desktop clients; OOB copy/paste is deprecated) | Reuse the loopback pattern |
| Headless sign-in (VPS, chat) | Device code | Device flow allows **only `drive.file`** (+ basic identity). No Gmail, Calendar, Sheets or full Drive | Device code is not an option. See §3 for three alternatives |
| Incremental consent | Scope bundles | `include_granted_scopes=true`; granular consent (the user may untick scopes) | Same bundle model; read the *granted* scopes from the token response |
| Refresh tokens | ~90-day sliding | **Expire after 7 days while the consent screen is in "Testing"** (external user type); long-lived once "In production" | The publisher must set the app to **In production** (unverified is fine for private use) |
| Unverified app | "Unverified" label on the consent screen | Published + unverified + sensitive/restricted scopes: **warning screen and a hard cap of 100 users** | Fine for private use (owner + ≤100 people). A public release would need verification |
| Scope tiers | Delegated permissions | Non-sensitive / **sensitive** (verification) / **restricted** (verification + security assessment). `drive`, `gmail.readonly`, `gmail.modify` and `gmail.compose` are **restricted**; `drive.file`, `spreadsheets`, `calendar` are sensitive | Private use: restricted scopes OK with the warning screen. Offer `drive.file` as the lighter alternative |
| Document content APIs | Excel only; Word/PPT need download, edit, upload | **Sheets, Docs and Slides all have real APIs** (`values.update/append`, `documents.batchUpdate`, `presentations.batchUpdate`) | Google-native files are edited live, so no round-trip. Office files in Drive can be exported or converted |
| Workspace orgs | Tenant may require admin consent | A Workspace org can use **Internal** user type (no verification, no cap) with its own project; admins can also block third-party apps | Support a bring-your-own client (`BROKER365_GOOGLE_CLIENT_ID/SECRET`) |

## 2. Publisher setup in Google Cloud (one time, by the owner)

1. Go to <https://console.cloud.google.com> and create a project `broker365`.
2. **Enable APIs**: Google Drive, Sheets, Docs, Slides, Gmail, Calendar, People (contacts), Tasks.
3. **Google Auth Platform → Branding / Audience**:
   - user type **External**;
   - app name `broker365`, support email, developer contact;
   - **Publishing status: In production**. Skip verification for private use; users then see the "Google hasn't verified this app" screen, with a cap of 100 users.
   - Don't stay in *Testing*: refresh tokens would expire every 7 days.
4. **Data access (scopes)**: add the scopes from §4 so they show on the consent screen.
5. **Clients → Create client → Application type: Desktop app**. Copy the client ID and client secret. Desktop clients accept any loopback port, so no redirect URI needs to be registered.
6. Put both into `config.py` as `DEFAULT_GOOGLE_CLIENT_ID` / `DEFAULT_GOOGLE_CLIENT_SECRET`. Users can override them with env `BROKER365_GOOGLE_CLIENT_ID` / `BROKER365_GOOGLE_CLIENT_SECRET`, or bring their own Internal client for a Workspace org.

## 2a. G0 result (done 2026-10-15)
- **Google account:** `yugienugraha26@gmail.com` (the owner's other account was at its project quota). **Project:** `broker365` (number 174513608376).
- **APIs enabled:** Drive, Sheets, Docs, Slides, Gmail, Calendar, People, Tasks.
- **Consent screen:**
  - app name `broker365`, user type **External**, publishing status **In production** (unverified);
  - home <https://broker365.sultantech.id>, privacy `/privacy`, terms `/terms`;
  - authorised domain `sultantech.id`;
  - the public pages live in `site/` and are deployed on Vercel.
- **OAuth client:**
  - type **Desktop app**, named `broker365 desktop`;
  - client ID `174513608376-4ehppkiqu1pt356n4ov8o7f0tk96ei8i.apps.googleusercontent.com`;
  - the secret is stored locally in `~/.broker365-live/google_client.json` (mode 600, outside the repo) until G2 builds it in;
  - the new "Use this client for an AI-powered agent" option was left **off**.
- **Scopes:** not pre-registered under Data access. They are only needed for verification; unverified apps can request them at sign-in.

## 3. Sign-in design (the hard part)

Google only supports loopback redirects for these scopes, so the redirect must land on
`http://127.0.0.1:<port>/…` **on the machine running broker365**. All three paths use
`access_type=offline`, `prompt=consent` (to get a refresh token), `include_granted_scopes=true`,
PKCE (S256) and a random `state`.

| Where you are | Flow |
|---|---|
| **Desktop (Windows / Mac mini)** | `broker365 login --provider google --scopes gmail,gdrive` starts a one-shot loopback listener on a random port and opens the browser. The callback is checked (state), the code exchanged (with the PKCE verifier) and the listener stops |
| **Dashboard** (`broker365 ui`) | Accounts → **Connect Google**. The redirect URI is the dashboard itself: `http://127.0.0.1:8365/oauth/google/callback`. It works on a VPS too, through the SSH tunnel already documented (`ssh -L 8365:127.0.0.1:8365 vps`), because the browser on your laptop reaches the tunnel's loopback port. The callback route is the only route exempt from the session cookie, and is protected by `state` + PKCE instead |
| **Chat (Hermes over Telegram/Discord, no tunnel)** | "Paste-back" flow, like rclone/gcloud. `account_login_start(provider="google")` returns the consent URL. The user signs in on any device; the browser then fails to load `http://127.0.0.1:…/?code=…&state=…`. The user copies that **address-bar URL** and pastes it into the chat; `account_login_complete(flow_id, redirect_url=…)` validates `state`, exchanges the code with the PKCE verifier and stores the token. The code is single-use, bound to the verifier that only broker365 holds, and expires in minutes |

The flow state (verifier, state, scopes) is kept like today's device flows: `flows/<id>.json`, mode 600,
random id, cleaned after 1 h.

## 4. Scope bundles (`google:` namespace)

| Bundle | Scopes | Tier |
|---|---|---|
| core (always) | `openid`, `email`, `profile` | non-sensitive |
| `gdrive` | `https://www.googleapis.com/auth/drive` | restricted |
| `gdrive-app` (lighter alternative) | `…/auth/drive.file` (only files the app created or opened) | sensitive |
| `gsheets` | `…/auth/spreadsheets` | sensitive |
| `gdocs` | `…/auth/documents` | sensitive |
| `gslides` | `…/auth/presentations` | sensitive |
| `gmail` | `…/auth/gmail.modify` (read, label, draft, send; no permanent delete) | restricted |
| `gcalendar` | `…/auth/calendar` | sensitive |
| `gcontacts` | `…/auth/contacts` | sensitive |
| `gtasks` | `…/auth/tasks` | sensitive |

Default when the user doesn't pick: `gdrive,gsheets,gdocs`.

## 5. Architecture changes

```
src/broker365/
  providers/                      NEW: one module per identity provider
    __init__.py                   Provider registry: name, bundles, login/refresh/logout, describe
    microsoft.py                  thin adapter over today's auth.py (no behaviour change)
    google.py                     OAuth (loopback + PKCE + paste-back), token refresh, revoke
  gapi.py                         NEW: httpx client for *.googleapis.com (paging nextPageToken,
                                  Retry-After, 401→refresh once, resumable uploads, export)
  services/google/                NEW tools: gdrive, gsheets, gdocs, gslides, gmail, gcal, gtasks, gcontacts
```

- **Connections, not just accounts.** An account becomes `(provider, username)`.
  - The default account is stored **per provider** (`default_accounts.microsoft`, `default_accounts.google`). The legacy `default_account` key counts as Microsoft's and is moved on the next write (done in G1).
  - Tools pick the provider's default; an optional `account` argument selects another one.
- **Token storage.** Google refresh tokens live in an encrypted blob `google_tokens.bin`. It uses the *same* `msal_extensions` persistence: DPAPI / Keychain / libsecret, with a mode-600 fallback.
  - Each entry holds: refresh token, granted scopes, email, `sub`, expiry.
  - Access tokens are only memoized in memory, like the Microsoft `_tokens`.
- **Token egress allow-list.** `gapi` sends `Authorization` only to `https://*.googleapis.com` hosts (`www`, `sheets`, `docs`, `slides`, `gmail`, `people`, `tasks`). Drive `webContentLink`/export redirects are fetched without the token, mirroring `graph.fetch`.
- **Registry.**
  - `@tool(bundle, provider="google", writes=…, destructive=…)`.
  - Existing Microsoft tool names stay unchanged, so the Hermes skill and prompts don't break.
  - Google tools get distinct prefixes: `gdrive_*`, `gsheets_*`, `gdocs_*`, `gslides_*`, `gmail_*`, `gcal_*`, `gtasks_*`, `gcontacts_*`.
- **One MCP server** keeps serving everything: Hermes key `m365`, which will be renamed to `broker365` with a compat alias in `hermes install`. Read-only mode, `services.call`, audit (`admin:login provider=google`) and the sandbox all apply unchanged.
- **Errors.** Google `401 invalid_grant` (revoked, or expired after password change) maps to `ReauthRequired`. A `403 insufficientPermissions` for a missing scope maps to `ConsentRequired("<bundle>")`. Both are explicit, unlike Microsoft where this needed MSAL details.
- **Dashboard.**
  - Accounts page: provider tabs (Microsoft / Google), a Google "Connect" panel with bundle checkboxes and a restricted-scope note, and the paste-back box as a fallback.
  - Overview: per-provider cards.
  - `/oauth/google/callback` is handled in the `guard` middleware as an explicit, state-validated exception.

## 6. Tools (first version)

| Area | Tools | API |
|---|---|---|
| Drive | `gdrive_list`, `gdrive_search` (`q=` syntax), `gdrive_get`, `gdrive_download`, `gdrive_upload` (resumable >5 MB), `gdrive_mkdir`, `gdrive_move`, `gdrive_copy`, `gdrive_trash` (destructive), `gdrive_share` (destructive), `gdrive_export` (native → pdf/docx/xlsx/pptx) | Drive v3 |
| Sheets | `gsheets_create`, `gsheets_list_tabs`, `gsheets_add_tab`, `gsheets_read_range`, `gsheets_write_range` (`USER_ENTERED`, so formulas work), `gsheets_append_rows` (`values.append`), `gsheets_clear_range` | Sheets v4 |
| Docs | `gdocs_create` (from Markdown → batchUpdate requests), `gdocs_read` (structured text), `gdocs_append`, `gdocs_replace_text` (`replaceAllText`), `gdocs_fill_template` (`{{key}}`) | Docs v1 |
| Slides | `gslides_create` (from outline), `gslides_read`, `gslides_add_slide`, `gslides_replace_text` | Slides v1 |
| Gmail | `gmail_list`, `gmail_search` (Gmail query syntax), `gmail_read`, `gmail_draft`, `gmail_send` (draft unless `confirm=true`), `gmail_send_draft`, `gmail_reply`, `gmail_label` | Gmail v1 |
| Calendar | `gcal_list` (time-zone aware), `gcal_create` (destructive with attendees), `gcal_update`, `gcal_delete`, `gcal_free_busy` | Calendar v3 |
| Tasks / Contacts | `gtasks_lists`, `gtasks_list`, `gtasks_create`, `gtasks_complete`; `gcontacts_list`, `gcontacts_create` | Tasks v1, People v1 |

Safety rules carry over unchanged:
- mail goes draft-first;
- invites, shares, trash and send are `destructive`;
- local paths go through `config.resolve_local` (UNC-safe);
- the audit log records targets only.

## 7. Libraries

- **HTTP:** plain `httpx`, as with Graph. One small client covers token refresh (POST `https://oauth2.googleapis.com/token`) and all REST calls, and there's no 100+ MB `google-api-python-client` dependency.
- **Auth:** our own PKCE + loopback + paste-back implementation, about 150 lines. `google-auth-oauthlib`'s `InstalledAppFlow` doesn't cover the dashboard-callback and paste-back paths, so it's optional as a reference only. `google-auth` `Credentials` semantics (refresh_token + client_id/secret + token_uri) are mirrored.
- **Markdown → Docs requests:** reuse the existing Markdown parser from `services/word.py` and emit `insertText` / `updateParagraphStyle` / `createParagraphBullets` requests.

## 8. Milestones

| # | Scope | Done when |
|---|---|---|
| G0 ✅ | Publisher GCP project + Desktop client (§2) | Done 2026-10-15, see §2a |
| G1 ✅ | `providers/` abstraction; Microsoft moved behind it with **no behaviour change**; per-provider default account + settings migration | Done 2026-10-15: `providers/` registry + Microsoft adapter, `default_accounts` (legacy key migrated on next write), `provider` on accounts/flows/tools/audit/CLI/dashboard; existing 201 tests pass (assertions only gained `provider` / `default_accounts`) |
| G2 ✅ (code) | Google OAuth: loopback CLI login, dashboard callback, chat paste-back; encrypted storage; refresh; revoke on logout | Live sign-in on the owner's Gmail via all three paths; token survives a restart. Implemented 2026-10-15, see §8a; live sign-in still to be checked by the owner |
| G3 ✅ | `gapi.py` + Drive tools | Live smoke: list/search/upload/download/export. Done 2026-10-15, live-verified on a personal Gmail account, see §8b |
| G4 ✅ (code) | Sheets, Docs, Slides tools (native batchUpdate) | Live smoke: create sheet, write formulas, read back computed values; doc from Markdown; slides from outline. Implemented 2026-10-15 with mocked tests, see §8c; live smoke pending |
| G5 ✅ (code) | Gmail, Calendar, Tasks, Contacts | Live smoke (read-only on real mail; draft-only send test). Implemented 2026-10-15 with mocked tests, see §8d; live smoke pending |
| G6 | Dashboard provider tabs, Hermes skill + MCP instructions update, docs | Browser test of both providers; `hermes install` registers one server exposing both |

## 8a. G2 result (2026-10-15)
- `providers/google.py` (`GoogleProvider`), registered next to Microsoft; `providers.catalog()`
  shows Google as available when a client ID + secret are configured. The client ID and secret
  are built into `config.py` (`DEFAULT_GOOGLE_CLIENT_ID/SECRET`); env overrides
  `BROKER365_GOOGLE_CLIENT_ID/SECRET` (a bring-your-own ID must come with its own secret).
- Bundles live in `config.GOOGLE_SCOPE_BUNDLES` (core = `openid email profile`), default
  `gdrive,gsheets,gdocs` (`Provider.default_bundles()`, also used by the CLI, the chat tool and
  the dashboard's pre-ticked boxes).
- One validator for every path: `state = "<flow_id>.<secret>"` (compared with
  `secrets.compare_digest`), expiry 600 s, only `code`/`state`/`error` read. Paste-back URLs must
  be `http://` + loopback host + the flow's port and path; they are never fetched.
  `flows.new_id()` / `flows.save(..., flow_id=)` / `flows.update()` (atomic) were added so the id
  is known before the state is built.
- Dashboard: `/api/login/start` for Google uses `http://127.0.0.1:<port>/oauth/google/callback`.
  The `guard` lets exactly `GET /oauth/google/callback` through without the session cookie (Host
  check and security headers still apply). The callback exchanges the code and stores
  `{"result": …}` / `{"error": …}` in the flow file (verifier dropped first); the page polls
  `/api/login/complete` every 2 s.
- Tokens: `google_tokens.bin` through the same `msal_extensions` persistence as the Microsoft
  cache; `{"accounts": {email_lower: {email, sub, refresh_token, scopes, hd, created}}}` under a
  thread lock plus a cross-process lockfile (`google_tokens.bin.lockfile`). Re-consent without a
  new refresh token keeps the old one; the stored scopes are Google's granted `scope` (all
  current grants, via include_granted_scopes). The email must be verified, and a different
  `sub` for the same address replaces the record. Access
  tokens are memoized ≤ 300 s. `invalid_grant` → `ReauthRequired`; a missing bundle →
  `ConsentRequired(bundle, "google")` ("… login --provider google --scopes <bundle>").
- Logout revokes the refresh token at `oauth2.googleapis.com/revoke` (best effort), deletes it,
  clears the memo and the default.
- Tests: `tests/test_google.py` (Google endpoints mocked; the loopback callback is a real
  request to 127.0.0.1).

## 8b. G3 result (2026-10-15)
- `gapi.py`: `GoogleClient(bundle, username=None)`, token from the Google provider. The token
  goes only over https to the exact hosts `www`, `sheets`, `docs`, `slides`, `gmail`, `people`
  and `tasks` `.googleapis.com` (lookalikes, http, explicit ports and userinfo are refused
  before sending). It shares `transport.py` with graph.py (pooled client that never follows
  redirects, sleep hook, `retry_delay`, retry limits, `seg`, the `JsonVerbs` get/post/patch
  mixin); each keeps its own retry loop. Download redirects are fetched without the token,
  https only.
- Retries: 429 and 403 `rateLimitExceeded`/`userRateLimitExceeded` for any method;
  500/502/503/504 for GET/PUT/DELETE/HEAD or `idempotent=True`; Retry-After or exponential
  backoff, at most `transport.MAX_RETRIES` retries and `transport.MAX_TOTAL_WAIT`. 401:
  `Provider.invalidate_token` (Google: drop the memoized token; Microsoft: drop the
  account's memo and force one MSAL refresh) and one retry with a fresh token, then
  `ReauthRequired`. A missing-scope 403 gets the same single retry (the memo may predate a
  grant made in another process) before `ConsentRequired`. 403 `insufficientPermissions` /
  `ACCESS_TOKEN_SCOPE_INSUFFICIENT` becomes `ConsentRequired(bundle, "google")`. Other errors
  become `GoogleApiError` (404 "not found or no access"); it and `GraphError` share the
  `ApiError` base. No If-Match yet, so no Google conflict error (G4 if needed). The error
  code is the first `errors[].reason`, else the first `details[].reason`, else `status`.
- Uploads: multipart up to 5 MB, else a resumable session (8 MiB chunks; a 308's `Range`
  gives the next offset), read from the local file chunk by chunk. A chunk that fails (5xx or
  network) is not resent blindly: the session status is queried (`bytes */size`) and the
  upload resumes from Google's Range, with bounded backoff; 404/410 = session expired. The
  session URI must pass the allow-list. `gdrive_download` streams into a temporary file next to the sandboxed
  destination and renames it into place (size cap; nothing left behind on failure);
  `gdrive_read_text` and exports read into memory with a cap (read_text truncates).
- Path walks start at the `root` alias (no lookup); existence/ambiguity probes ask only for
  `id,name,mimeType,driveId`. Folder-scoped queries use `corpora=drive&driveId=…` for a
  shared-drive folder, else `corpora=user`; only `gdrive_search` uses `allDrives`. An
  `incompleteSearch` makes path resolution and probes fail ("use the id"); `gdrive_search`
  then returns `{"files": [...], "incompleteSearch": true, "note": …}` instead of a list.
  `conflict="rename"` reads the folder's names once (`name contains` matches tokens only).
  `convert=true` with `conflict="replace"` on an existing file is refused (rename or trash).
- Local files: `files.write_local` and `gdrive_download`/`gdrive_export` all go through
  `files.stream_local` (sandbox check, overwrite rule, temp file + `os.replace`).
- Sharing with an address that has no Google account while `notify=false` (Drive 400
  `invalidSharingRequest`) explains that Google requires `notify=true`.
- Audit: the core `audit.TARGET_KEYS` plus per-tool extras declared with
  `@tool(..., audit=(...))` (upload: remote_folder/conflict/convert; write_text: conflict;
  share: email/role/type/domain; who got access is a target, the message is not logged).
- `services/gdrive.py`: 13 tools on bundle `gdrive` (full Drive). Items by id or absolute path,
  resolved name by name (intermediate parts must be folders; an ambiguous name lists the ids).
  Every call sends `supportsAllDrives=true`; listings and searches add
  `includeItemsFromAllDrives=true&corpora=allDrives`. No `account` argument: like the Microsoft
  tools, they use the provider's default account. Trash is `files.update trashed=true`; share
  allows reader/commenter/writer only.
- **Live smoke (2026-10-15, personal Gmail, 41/41 + conversions):** mkdir (idempotent), list,
  write/read text, conflict fail/replace/rename, 12 MB resumable upload and byte-identical
  download, convert csv → Sheet and md/txt/html → Doc (Markdown keeps headings and bold), native
  download refused, Sheet export xlsx/pdf/csv/ods, Doc export docx/pdf/md/html/txt/odt/rtf/epub,
  full-text and raw search, get, copy, move+rename, not-found, sandbox refusal, trash.
  Findings: Google's text/plain export starts with a UTF-8 BOM (stripped); the tool argument
  `name` collided with `services.call(name, ...)` (now positional-only).
- Not verified live: `gdrive_share` (it shares with real people), `corpora=allDrives` on a
  Workspace account with shared drives, `replace` onto a native file.

## 8c. G4 result (2026-10-15; live smoke: Sheets and Docs 31/31, Slides awaiting consent)
- `services/gsheets.py` (Sheets v4), `gdocs.py` (Docs v1), `gslides.py` (Slides v1). Each has
  one `_gnative.Native` descriptor (bundle, API base URL, Drive mimeType, name, Microsoft
  alternative) giving `client()`, `url()`, `resolve()`, `update()` (revision-guarded
  batchUpdate → (replies, new revision)) and `replace_all()`. gdrive's public helpers
  (`folder`, `folder_ref`, `claim_name`, `put`, `create`, `copy`) do the Drive side.
- Items: a **path** is looked up in Drive (minimal fields) and must be the native type; a bare
  **id** goes straight to the product API, whose 404 / "not supported for this document" 400
  is explained with the convert-or-use-Microsoft hint (no Drive call, no gdrive needed).
- Creation goes through Drive (`files.create` with the native mimeType; `/` is the `root` alias
  without a lookup; conflict `fail`/`rename` on the title, same wording as gdrive).
  `gdocs_create` uploads the Markdown with conversion through `gdrive.put`.
  `gsheets_create` uses the Sheets API create (My Drive root) when the folder is `/` and
  `has_consent("gdrive")` is false (it then can't check for duplicate titles and says so in a
  `note`); with tabs it renames sheetId 0 and adds the others in one batchUpdate with
  `includeSpreadsheetInResponse` (no extra GETs).
- Consent pre-checks: `services.require_consent(provider, bundles)` raises ConsentRequired for
  the first bundle `Provider.has_consent` says isn't granted. `services.call` runs it for a
  tool's static `@tool(..., requires=(...))` (gdocs_create, gslides_create: gdrive); tools with
  argument-dependent needs call it themselves (gsheets_create: gsheets; gdocs_fill_template:
  gdrive only with `copy_to` or a path). `has_consent` returns False only for a missing scope;
  an unresolvable account (not signed in, no default) raises its own ReauthRequired /
  Broker365Error. The catalog and dashboard show `requires`.
- Sheets: A1 ranges are parsed with quoting: a leading quote runs to its closing quote (`''`
  escapes), and only then is `!` split (`'Q1!Sales'` = that whole tab, `'Q1!Sales'!A1` = a
  cell on it). Tab names are quoted when needed. A bare non-cell value is a tab name; a bare
  cell-like value (`Q1`, `FY24`; a lone column like `Log` doesn't count) is a tab of exactly
  that name if one exists (one titles lookup), else cells on the first tab. Values must be a
  2-D list of scalars (null → empty); `USER_ENTERED` unless `raw`; append uses `INSERT_ROWS`;
  clear is destructive.
- Docs: read (with a fields mask) flattens paragraphs (`#` headings, list items `- ` or
  `1.` from the list's glyph, numbered per list and level, tables as TSV rows) and returns
  `revisionId`. Append inserts at `endIndex - 1` (a leading newline when the doc has
  content), sets NORMAL_TEXT and deletes bullets over the inserted range (UTF-16 indexes) so
  it doesn't inherit a heading/list style, guarded by the revision just read; on a conflict
  with that internal guard it re-reads and retries (3 attempts), then reports concurrent
  editing without mentioning revisions.
  Replace/fill use `replaceAllText`; `revision_id` → `writeControl.requiredRevisionId`; a stale
  revision → `GoogleConflictError`: for requests sent with `guarded=True` only, gapi maps a
  412, or a 400 whose message matches `required revision id .* does not match` (live, Docs:
  HTTP 400 **INVALID_ARGUMENT** "The required revision ID '<id>' does not match the latest
  revision."). Other FAILED_PRECONDITION/ABORTED answers stay plain GoogleApiErrors with
  Google's text. `ConflictError` is provider-neutral (`ApiError`); `GraphConflictError` (Graph
  412) and `GoogleConflictError` derive from it and from their provider's error.
  `gdocs_fill_template` can copy the template first (title checked with
  `gdrive.claim_name`, conflict fail/rename; default folder: the template's) and trashes the
  copy if filling it fails; it reports per-key counts and unmatched keys.
- Slides: `createSlide` with predefined layouts (TITLE for a first slide without body,
  TITLE_AND_BODY with body/bullets, TITLE_ONLY otherwise), `placeholderIdMappings` for title and
  body ids, `insertText`, `createParagraphBullets`; the default blank slide is deleted when
  slides were added. Read reports title/body by placeholder type.
- Audit extras: titles, folders, ranges, tab titles, `copy_to`, slide index. Never values,
  Markdown, text, find/replace strings or template values.
- To verify live: Sheets fallback create without gdrive; formulas computed when read back with
  FORMATTED_VALUE; the exact error shape of a stale `requiredRevisionId` (Docs and Slides);
  Markdown conversion of tables; `TITLE` layout placeholder type `CENTERED_TITLE`; deleting
  the default slide; quoted tab names with non-ASCII characters; Slides consent (the live
  account has only gdrive, gsheets, gdocs).

## 8d. G5 result (2026-10-15, code; live smoke pending)
- `services/gmail.py` (Gmail v1, bundle `gmail` = gmail.modify), `gcalendar.py` (Calendar v3),
  `gtasks.py` (Tasks v1), `gcontacts.py` (People v1). Semantics, safety rules and result shapes
  mirror mail/calendar/tasks/contacts.py. Shared code: `services/_mail.py` (mail policy for
  Outlook and Gmail: `required_emails`, `read_attachment` with the 3 MB limit,
  `draft_note`/`draft_result`; each tool drafts unless confirm); `_shapes` (`html_to_text`, `to_utc`, `parse_iso`,
  `iso_date`, which refuses impossible dates such as 2026-13-45 everywhere);
  `files.local_name`, applied by `write_local`/`stream_local` to every remote default name
  (OneDrive, Drive and mail attachments); `gdrive.upload_mime` for attachment types;
  `gapi.PAGE_PARAMS` (page-size parameter and maximum per API, used by `paged`).
- Gmail: list/search = `messages.list` (fields `messages/id,nextPageToken`) then one
  `format=metadata` GET per message with a fields mask, 8 at a time (order kept; at most 100). Read walks the MIME tree: first non-attachment text/plain, else text/html
  through `html_to_text`, charset from Content-Type; attachments listed with id and part id.
  Download reads the parts tree (fields mask), looks the attachment up again by id or part
  id (Gmail may issue a new attachmentId per read), refuses parts over 35 MB before
  downloading, and saves through `files.write_local`.
- Composing: `email.message.EmailMessage` (To/Cc/Bcc, plain or HTML with a text alternative,
  sandboxed attachments ≤ 3 MB), base64url `raw`. Addresses go through `validate_emails`;
  CR/LF in the subject or extra headers is refused (EmailMessage also refuses it). Send and
  reply are draft-first (`drafts.create`, same result shape as mail_send) unless `confirm`;
  `gmail_send_draft` = `drafts.send`; `gmail_discard_draft` moves the draft's message to the
  Trash (`drafts.get?format=minimal` + `messages.trash`, never `drafts.delete`). Replies keep the thread (`threadId`, In-Reply-To,
  References, "Re:"); reply-all drops the account's own address (from the provider's stored
  record, `google.account_email`, served from the decoded-store cache); replying to your own
  message goes to its original recipients, never to yourself. `gmail_list` resolves its label
  like `gmail_label` (case-insensitive). `gmail_label` needs no lookup for system labels (INBOX,
  UNREAD, STARRED, …, CATEGORY_*); user label names (case-insensitive) come from a
  per-account `labels.list` cache (fields `labels(id,name)`), re-read once on a miss;
  mark_read and archive are label changes. `gmail_trash` uses `messages.trash`; nothing deletes permanently.
- Calendar: list converts the window to UTC (`timeMin/timeMax`), `singleEvents=true`,
  `orderBy=startTime`, results in `time_zone`, with a fields mask of what the summary uses. Create is destructive (like calendar_create);
  `sendUpdates` is `all` only with `send_updates=true` (`notify` for delete), else `none`.
  All-day events use dates with an exclusive end (end ≤ start → one day). Timed values are
  normalized to ISO 8601 and a bare date without all_day is refused. `gcal_update` reads the
  event's start/end first: it always sends both in one kind, keeps the duration when only one
  is given, and `all_day=true/false` switches kinds (both ends required; the other kind's keys
  are sent as null). Free/busy uses
  `freeBusy.query` and reports per-calendar errors.
- Tasks: `@default` list alias kept literal; open tasks only unless `include_completed`
  (`showCompleted`+`showHidden`); due dates as `YYYY-MM-DDT00:00:00.000Z`.
- Contacts: connections list sorted by first name; `searchContacts` gets one empty-query
  warm-up per account per process (Google's advice); create via `people:createContact`.
- Audit: `bcc` is counted like to/cc (also `emails`); label changes, calendar id and update
  flags are targets. Subjects, bodies, descriptions, notes are never logged.
- To verify live: metadata `metadataHeaders` repeated params; body extraction on real
  multipart mail (inline images, forwarded messages, non-UTF-8 charsets); attachmentId
  stability between read and download; draft then send_draft appearing in Sent in the right
  thread; reply threading in Gmail's UI; label names with `/`; Calendar all-day exclusive end
  and `timeZone` echo; `sendUpdates=none` really sending nothing; free/busy for a non-shared
  calendar (error reason); Tasks `@default` and `showHidden`; People search warm-up latency
  (results can lag a few seconds after create). Needs consent for gmail, gcalendar, gtasks
  and gcontacts first.

Each milestone follows the existing process: tests, then `/simplify`, `/code-review`,
`/security-review`, then commit and push to `main`.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| 7-day refresh-token expiry if the project stays in Testing | Setup step 3 sets **In production**. `account_status` warns when a Google token is <7 days old and fails with `invalid_grant` |
| "Google hasn't verified this app" screen worries users | Expected for private use; documented in the README with a screenshot note. Verification only if ever made public, which isn't planned |
| 100-user cap (unverified + sensitive/restricted) | Private distribution, well under the cap |
| Restricted scopes (`drive`, `gmail.*`) | Private use is allowed with the warning screen. `gdrive-app` (`drive.file`) is offered for people who prefer minimal access |
| Workspace org blocks third-party apps | Bring-your-own Internal client via env vars; `admin-consent` style note in the docs |
| Paste-back URL contains the auth code | Single-use, PKCE-bound (useless without our verifier), short-lived; never logged; flow file deleted after exchange |
| Loopback interception (other local app on the port) | PKCE + random `state`; the listener binds 127.0.0.1 only and stops after one callback |
| Token sent to the wrong host | `gapi` host allow-list (`*.googleapis.com`), redirects followed without the token (same tests as `graph.py`) |
| Tool-name collisions / agent confusion between providers | Distinct prefixes (`gdrive_` vs `files_`), per-provider defaults, `account` argument, MCP instructions explaining both |

## 10. Open decisions for the owner

1. App name on Google's consent screen (default `broker365`).
2. Default Drive access: full `drive` (restricted, can search all files) or `drive.file` only. **Recommended: full `drive` for private use.**
3. Rename the Hermes MCP key from `m365` to `broker365` now that it serves both providers? Tool names inside Hermes would change from `mcp_m365_*` to `mcp_broker365_*`.
4. Should the archived Hermes `google-workspace` skill be removed once this works, to avoid two Google integrations?
