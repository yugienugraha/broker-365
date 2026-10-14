---
name: microsoft-365
description: "OneDrive, Excel, Word, PowerPoint, Outlook mail/calendar/contacts, To Do via broker365."
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
3. Send the user the `verification_uri` and `user_code`, and ask them to sign in from any device.
4. Call `mcp_m365_account_login_complete` with the `flow_id`. Repeat while status is `pending`.

Terminal alternative: `broker365 login --scopes files,mail` (opens a browser, or uses a device
code automatically when there's no display).

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
