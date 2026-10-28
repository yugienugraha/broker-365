---
name: microsoft-365
description: "OneDrive, Excel, Word, PowerPoint, Outlook mail/calendar/contacts, To Do and Google Drive via broker365."
version: 0.1.0
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Microsoft, Office365, OneDrive, Excel, Word, PowerPoint, Outlook, Graph, OAuth]
    related_skills: [native-mcp, xlsx, docx]
---

# Microsoft 365 (broker365)

broker365 connects you to the user's Microsoft account through Microsoft Graph. It needs no Azure
setup on the user's side, and tokens stay on this machine.

## Which interface
- **Preferred:** the `mcp_m365_*` tools, available when the `m365` MCP server is configured.
- **Fallback (terminal):** `broker365 call <tool> '<json args>'`. List tools with `broker365 tools`.

## Sign-in (works over chat and on headless servers)
1. Check: `mcp_m365_account_status` (or `broker365 status`).
2. If not signed in, or a tool reports a missing permission, call
   `mcp_m365_account_login_start` with `scopes` set to what's needed. Bundles: `files`,
   `mail`, `calendar`, `contacts`, `tasks`, `files-shared`, `sharepoint`, `onenote`.
   If `broker365 settings` shows `app_folder: true`, ask for `files-app` instead of `files`:
   OneDrive paths then start at the app folder (`/` = `/Apps/broker365`) and nothing outside
   it is reachable.
3. Microsoft (`kind: "device_code"`): send the user the `verification_uri` and `user_code`, and
   ask them to sign in from any device.
4. Call `mcp_m365_account_login_complete` with the `flow_id`. Repeat while status is `pending`.

Terminal alternative: `broker365 login --scopes files,mail` (opens a browser, or uses a device
code automatically when there's no display).

### Connecting a Google account (Gmail / Google Workspace) over chat
1. Call `mcp_m365_account_login_start` with `provider="google"` and `scopes`, e.g.
   `["gdrive", "gsheets", "gdocs"]` (the default). Bundles: `gdrive`, `gdrive-app`, `gsheets`,
   `gdocs`, `gslides`, `gmail`, `gcalendar`, `gcontacts`, `gtasks`.
2. The result has `kind: "authorize_url"` and `needs_redirect_url: true`. Send the user the
   `authorize_url` and the `instructions`: sign in, approve (if Google says it hasn't verified
   the app, choose *Advanced → Go to broker365*), then the browser shows *"This site can't be
   reached"*. That is expected: they copy the **full address-bar URL** (it starts with
   `http://127.0.0.1:`) and paste it into the chat. The link is valid for 10 minutes.
3. Call `mcp_m365_account_login_complete` with the `flow_id` and `redirect_url` set to the
   pasted URL. Status `signed_in` means done. If it says the state doesn't match, ask for the
   URL from the latest sign-in; if it expired, start again.
Never echo the pasted URL back or store it anywhere: it contains a one-time code.
Terminal alternative: `broker365 login --provider google`.

## Files and Office documents
- Address files by absolute OneDrive path (`/Documents/Budget.xlsx`) or by item id.
- **Excel:** `excel_read_range`, `excel_write_range` (2-D `values`; strings starting with `=`
  are formulas), `excel_append_rows`, sheet tools. Work/school accounts edit the file live.
  Some personal accounts fall back to download→edit→upload, and then formulas aren't
  recalculated until the file is opened in Excel.
- **Word:** `word_create` from Markdown, `word_read`, `word_append`, `word_replace_text`,
  `word_fill_template` (`{{placeholders}}`), `word_export_pdf`.
- **PowerPoint:** `ppt_create` from a slide outline, `ppt_read`, `ppt_add_slide`,
  `ppt_replace_text`, `ppt_export_pdf`.
- Word and PowerPoint are edited by download→edit→upload. If someone changed the file meanwhile
  you get a **conflict** error, not an overwrite. Re-read the file and retry.
- Local files are only readable/writable inside the broker365 workspace (`account_status` shows
  it). Save local files there before uploading.

## Google Drive (connected Google account, scope `gdrive`)
- Tools: `gdrive_list`, `gdrive_search` (name + full text; `raw=true` takes Drive `q` syntax),
  `gdrive_get`, `gdrive_read_text`, `gdrive_download`, `gdrive_export`, `gdrive_upload`,
  `gdrive_write_text`, `gdrive_mkdir`, `gdrive_move`, `gdrive_copy`, `gdrive_trash`,
  `gdrive_share`.
- Address items by Drive file **id** or absolute path (`/Reports/q3.pdf`, `/` = My Drive).
  Drive allows duplicate names: if a path is ambiguous the error lists the ids; use one.
- Google Docs/Sheets/Slides have no file content of their own: read them with
  `gdrive_read_text` (a Sheet gives CSV of its first sheet) or save them with `gdrive_export`
  (Docs: pdf docx odt txt md html epub rtf; Sheets: pdf xlsx ods csv tsv; Slides: pdf pptx odp
  txt; 10 MB limit). `gdrive_download` is for ordinary files only.
- `gdrive_upload` with `convert=true` turns docx/md/txt/html → Docs, xlsx/csv → Sheets, pptx → Slides. Name
  conflicts: `conflict="fail"` (default), `"replace"` or `"rename"`.
- `gdrive_trash` moves to the trash (restorable); nothing is deleted permanently.
- `gdrive_share` is **destructive**: confirm with the user who gets which access first.
  `type="anyone"` means anyone with the link. An address without a Google account can only
  be shared with `notify=true` (Google emails them).

## Google Sheets, Docs and Slides (scopes `gsheets`, `gdocs`, `gslides`)
- Files are addressed like Drive items (id or path). Office files in Drive must first be
  converted (`gdrive_upload` with `convert=true`); OneDrive files use the excel/word/ppt tools.
- **Sheets:** `gsheets_create` (in a Drive folder; optional `tabs` and initial `values`),
  `gsheets_list_tabs`, `gsheets_add_tab`, `gsheets_read_range` (`render`: FORMATTED_VALUE,
  UNFORMATTED_VALUE or FORMULA), `gsheets_write_range` (strings are typed as in the UI, so
  `=SUM(A1:A3)` is a formula; `raw=true` stores text literally), `gsheets_append_rows`,
  `gsheets_clear_range` (destructive). Ranges: `A1:C10`, `Tab name!A1:B2` (quoting is done
  for you) or just a tab name.
- **Docs:** `gdocs_create` from Markdown (headings, bold, lists survive), `gdocs_read` (text
  with `#` headings, `- `/`1.` lists, tab-separated tables, plus `revisionId`), `gdocs_append`
  (plain text at the end), `gdocs_replace_text`, `gdocs_fill_template` (`{{key}}` placeholders;
  `copy_to` fills a copy and keeps the template; reports keys that matched nothing).
- **Slides:** `gslides_create` from an outline (`[{"title", "body"} or {"title", "bullets"}]`),
  `gslides_read`, `gslides_add_slide`, `gslides_replace_text`.
- Pass the `revisionId` from a read as `revision_id` to the replace tools: if someone edited
  the file meanwhile you get a conflict and nothing changes. Re-read and retry.

## Gmail, Google Calendar, Tasks and Contacts (scopes `gmail`, `gcalendar`, `gtasks`, `gcontacts`)
- **Gmail:** `gmail_list` (by label, e.g. INBOX), `gmail_search` (Gmail syntax such as
  `from:ann has:attachment newer_than:7d`), `gmail_read` (plain-text body and attachment ids),
  `gmail_download_attachment` (into the workspace), `gmail_draft`, `gmail_label` (add/remove
  labels by name, mark read, archive), `gmail_trash` (to the Trash, never a permanent delete).
- **Sending is draft-first, like Outlook:** `gmail_send` and `gmail_reply` create a draft unless
  `confirm=true`. Show the draft to the user and, only after their explicit approval, send
  exactly that draft with `gmail_send_draft(draft_id)`; if they reject it, discard it with
  `gmail_discard_draft(draft_id)` (moves it to the Trash). `gmail_reply(reply_all=true)` leaves
  the account's own address out; replying to your own sent message goes to its recipients.
- **Calendar:** `gcal_list` (start/end ISO datetimes in `time_zone`, recurring events
  expanded), `gcal_create`, `gcal_update`, `gcal_delete`, `gcal_free_busy`. Attendees are only
  emailed with `send_updates=true` (`notify=true` for a delete): confirm with the user first.
- **Tasks:** `gtasks_lists`, `gtasks_list`, `gtasks_create` (due date YYYY-MM-DD),
  `gtasks_complete`. **Contacts:** `gcontacts_list`, `gcontacts_search`, `gcontacts_create`.

## SharePoint and OneNote
- SharePoint (work/school accounts only, scope `sharepoint`): `sharepoint_sites`,
  `sharepoint_drives`, `sharepoint_list`, `sharepoint_download`, `sharepoint_upload`. Site ids
  can be `host,guid,guid` or `host:/sites/name:`. When `drive_id` is given, `site_id` is ignored.
- OneNote (scope `onenote`): `onenote_notebooks`, `onenote_sections`, `onenote_pages`,
  `onenote_read_page`, `onenote_create_page`.

## Mail / calendar safety
- `mail_send`, `mail_reply` and `mail_forward` create a **draft** by default. Show the user the
  draft, and after explicit approval send *that* draft with `mail_send_draft(draft_id)`.
  `confirm=true` sends immediately; use it only when the user already approved the exact text.
- `calendar_create` with attendees sends invitations, so confirm first.
- Never delete files, events or mail without the user asking for it.
