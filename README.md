# broker365

Connect your AI agent (Hermes, or any MCP client) to Microsoft 365: OneDrive, Excel, Word,
PowerPoint, Outlook mail, Calendar, Contacts and To Do. **You don't create an Azure app. You sign
in once.**

Tokens stay on your machine, in the OS-encrypted store when one exists (DPAPI on Windows,
Keychain on macOS, libsecret on Linux), otherwise in a mode-600 file. broker365 has no server of
its own and only talks to `login.microsoftonline.com` and `graph.microsoft.com`.

## Install
```bash
pipx install git+https://github.com/yugienugraha/broker-365.git
# or, for development:
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

> **Publisher note:** until the public client ID is built in, set `BROKER365_CLIENT_ID` to your
> app registration. See [docs/AZURE_SETUP.md](docs/AZURE_SETUP.md).

## Sign in
```bash
broker365 login --scopes files,mail,calendar   # browser; on a VPS it uses a device code automatically
broker365 status
```
Scope bundles: `files`, `files-shared`, `sharepoint`, `mail`, `calendar`, `contacts`, `tasks`,
`onenote`. Run `login` again with more bundles at any time.

## Use with Hermes
```bash
broker365 hermes install      # adds the `m365` MCP server to $HERMES_HOME/config.yaml + installs the skill
```
Restart Hermes, then ask it something like *"Create /Budget.xlsx with these numbers and share a
view link"*. Hermes can also run the sign-in itself over chat (device code), with no terminal
needed.

## Use from the terminal
```bash
broker365 tools
broker365 call files_list '{"path": "/Documents"}'
broker365 call excel_write_range '{"item": "/Budget.xlsx", "sheet": "Sheet1", "address": "A1:B2", "values": [["Item","Cost"],["Coffee",3]]}'
broker365 call word_create '{"remote_path": "/Notes.docx", "markdown": "# Hello\n- one\n- two"}'
```

## Safety
- Everything is delegated: the agent can do only what the signed-in user can.
- `broker365 settings --read-only` hides every write tool.
- Local file access is sandboxed to `$BROKER365_HOME/files`. Add more with `--allow-dir`.
- Mail send/reply/forward create drafts by default. The agent sends the approved draft with `mail_send_draft`.
- Word and PowerPoint edits are eTag-guarded, so concurrent changes produce a conflict instead
  of being overwritten.

## What works where
| | Work/school | Personal |
|---|---|---|
| OneDrive files | ✅ | ✅ |
| Excel cell API | ✅ live | ⚠️ probed per account; falls back to download→edit→upload |
| Word / PowerPoint | download→edit→upload | download→edit→upload |
| Mail, calendar, contacts, To Do | ✅ | ✅ (free/busy is work/school only) |

The design rationale is in [docs/PLAN.md](docs/PLAN.md).
