# Google Workspace connection: implementation plan

> Goal: add **Google Workspace / Gmail accounts** to broker365 as a second connection type next to
> Microsoft 365, with the same approach: one publisher-owned app registration, the user only
> signs in, tokens stay local, and Hermes gets tools through the same MCP server, CLI and
> dashboard.
>
> Status: plan only (2026-10-15). Facts below were checked against Google's current docs via
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
  - The default account is stored **per provider** (`default_account.microsoft`, `default_account.google`). Today's `default_account` key is migrated.
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
| G1 | `providers/` abstraction; Microsoft moved behind it with **no behaviour change**; per-provider default account + settings migration | All existing tests (201) pass unchanged |
| G2 | Google OAuth: loopback CLI login, dashboard callback, chat paste-back; encrypted storage; refresh; revoke on logout | Live sign-in on the owner's Gmail via all three paths; token survives a restart |
| G3 | `gapi.py` + Drive tools | Live smoke: list/search/upload/download/export |
| G4 | Sheets, Docs, Slides tools (native batchUpdate) | Live smoke: create sheet, write formulas, read back computed values; doc from Markdown; slides from outline |
| G5 | Gmail, Calendar, Tasks, Contacts | Live smoke (read-only on real mail; draft-only send test) |
| G6 | Dashboard provider tabs, Hermes skill + MCP instructions update, docs | Browser test of both providers; `hermes install` registers one server exposing both |

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
