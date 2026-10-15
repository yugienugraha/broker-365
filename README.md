# broker365

Connect your AI agent (Hermes, or any MCP client) to Microsoft 365: OneDrive, Excel, Word,
PowerPoint, Outlook mail, Calendar, Contacts and To Do. **You don't create an Azure app. You sign
in once.**

Tokens stay on your machine, in the OS-encrypted store when one exists (DPAPI on Windows,
Keychain on macOS, libsecret on Linux), otherwise in a mode-600 file. broker365 has no server of
its own and only talks to `login.microsoftonline.com` and `graph.microsoft.com`.

## Install
broker365 is **private**: it lives only in the private GitHub repo and is not published on
PyPI. Each machine that installs it needs read access to the repo.

```bash
# one-time per machine: sign in to GitHub (opens a browser / device code; works over SSH too)
gh auth login
gh auth setup-git          # lets git/pip use that login for https://github.com URLs

pipx install git+https://github.com/yugienugraha/broker-365.git
# update later
pipx upgrade broker365     # or: pipx install --force git+https://github.com/yugienugraha/broker-365.git
```

Without the GitHub CLI, use an SSH key that has access to the repo:
`pipx install git+ssh://git@github.com/yugienugraha/broker-365.git`

For development: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`

The public client ID of the `broker365` app registration is built in, so no Azure setup is
needed. The consent screen shows the app as *unverified* until publisher verification is done.
Some work/school tenants then need an admin to approve it (`broker365 admin-consent-url <tenant>`).
To use your own registration instead, set `BROKER365_CLIENT_ID` (see [docs/AZURE_SETUP.md](docs/AZURE_SETUP.md)).

## Sign in
```bash
broker365 login --scopes files,mail,calendar   # browser; on a VPS it uses a device code automatically
broker365 status
```
Scope bundles: `files`, `files-shared`, `sharepoint`, `mail`, `calendar`, `contacts`, `tasks`,
`onenote`. Run `login` again with more bundles at any time.

### Google accounts (Gmail / Google Workspace)
```bash
broker365 login --provider google                          # default: gdrive,gsheets,gdocs
broker365 login --provider google --scopes gmail,gcalendar # add more later the same way
```
On a desktop this opens the browser and catches Google's redirect on a one-shot listener on
`127.0.0.1`. Without a browser (VPS, SSH) it prints a sign-in link instead: open it on any
device, approve, and the browser ends on a *"This site can't be reached"* page at
`http://127.0.0.1:<port>/?state=…&code=…`. That is expected. Copy the **full address-bar URL**
and paste it into the terminal. The code in it is single-use, short-lived and useless without
the PKCE verifier that only broker365 holds.

Google bundles: `gdrive` (all of Drive), `gdrive-app` (only files broker365 created or opened),
`gsheets`, `gdocs`, `gslides`, `gmail` (read, label, draft, send; no permanent delete),
`gcalendar`, `gcontacts`, `gtasks`.

**"Google hasn't verified this app".** broker365's Google client is in production but not
verified (private use, at most 100 users). Google shows this warning, especially for the
*restricted* scopes `gdrive` and `gmail`: choose **Advanced → Go to broker365 (unsafe)** to
continue. Prefer `gdrive-app` if you don't want to grant access to your whole Drive.
Sign out with `broker365 logout --provider google -a you@gmail.com` (this also revokes the
token at Google); remove the app entirely at <https://myaccount.google.com/connections>.
A Workspace org that blocks third-party apps can bring its own *Internal* client:
`BROKER365_GOOGLE_CLIENT_ID` / `BROKER365_GOOGLE_CLIENT_SECRET`.

## Dashboard
```bash
broker365 ui                 # opens http://127.0.0.1:8365 in your browser
broker365 ui --no-browser    # just print the link (e.g. on a VPS)
```
The dashboard lets you sign in to Microsoft accounts (device code) and Google accounts (Google
redirects back to the dashboard, so it finishes by itself), pick the default account,
switch read-only mode, manage allowed folders, connect Hermes, and review activity.
It listens only on `127.0.0.1` and uses a one-time link with a secret token. On a VPS, reach it
through an SSH tunnel: `ssh -L 8365:127.0.0.1:8365 you@vps`, then open the printed link locally.

## Use with Hermes
```bash
broker365 hermes install      # adds the `m365` MCP server to $HERMES_HOME/config.yaml + installs the skill
```
Restart Hermes, then ask it something like *"Create /Budget.xlsx with these numbers and share a
view link"*. Hermes can also run the sign-in itself over chat, with no terminal needed: a
device code for Microsoft, and for Google a link plus "paste back the address-bar URL".

## Use from the terminal
```bash
broker365 tools
broker365 call files_list '{"path": "/Documents"}'
broker365 call gdrive_list '{"path": "/"}'
broker365 call excel_write_range '{"item": "/Budget.xlsx", "sheet": "Sheet1", "address": "A1:B2", "values": [["Item","Cost"],["Coffee",3]]}'
broker365 call word_create '{"remote_path": "/Notes.docx", "markdown": "# Hello\n- one\n- two"}'
```

## Safety
- Everything is delegated: the agent can do only what the signed-in user can.
- `broker365 settings --read-only` hides every write tool.
- Local file access is sandboxed to `$BROKER365_HOME/files`. Add more with `--allow-dir`. On Windows, UNC, device and drive-relative paths are rejected before they are touched, so a prompt-injected `\\host\share` path cannot leak NTLM credentials.
- Every write operation is recorded in `$BROKER365_HOME/audit.log` (tool, time, outcome, target ids/paths; never content).
- Mail send/reply/forward create drafts by default. The agent sends the approved draft with `mail_send_draft`.
- Word and PowerPoint edits are eTag-guarded, so concurrent changes produce a conflict instead
  of being overwritten.

## What works where
| | Work/school | Personal |
|---|---|---|
| OneDrive files | ✅ | ✅ |
| Excel cell API | ✅ live | ✅ live (verified Oct 2026); download→edit→upload fallback if an account refuses it |
| Word / PowerPoint | download→edit→upload | download→edit→upload |
| Mail, calendar, contacts, To Do | ✅ | ✅ (free/busy is work/school only) |
| OneNote | ✅ | ✅ |
| SharePoint sites | ✅ | — |

### Google tools
| Area | Tools | Notes |
|---|---|---|
| Google Sheets (`gsheets`) | `gsheets_create`, `gsheets_list_tabs`, `gsheets_add_tab`, `gsheets_read_range`, `gsheets_write_range`, `gsheets_append_rows`, `gsheets_clear_range` | Live edits through the Sheets API; formulas work (`USER_ENTERED`); clearing is destructive |
| Google Docs (`gdocs`) | `gdocs_create`, `gdocs_read`, `gdocs_append`, `gdocs_replace_text`, `gdocs_fill_template` | Create from Markdown; `{{key}}` templates; revision-guarded replace |
| Google Slides (`gslides`) | `gslides_create`, `gslides_read`, `gslides_add_slide`, `gslides_replace_text` | Decks from an outline (title + body or bullets) |
| Google Drive (`gdrive`) | `gdrive_list`, `gdrive_search`, `gdrive_get`, `gdrive_read_text`, `gdrive_download`, `gdrive_export`, `gdrive_upload`, `gdrive_write_text`, `gdrive_mkdir`, `gdrive_move`, `gdrive_copy`, `gdrive_trash`, `gdrive_share` | Items by file id or path (`/Reports/q3.pdf`). Docs/Sheets/Slides are read with `gdrive_read_text` or exported (pdf, docx, xlsx, pptx, …). Trash only, never a permanent delete; `gdrive_share` and `gdrive_trash` are destructive |

The design rationale is in [docs/PLAN.md](docs/PLAN.md).
