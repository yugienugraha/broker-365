# Azure / Entra setup (publisher only, one time)

End users never do this. You register **one** app; its client ID is built into broker365 (or
supplied with `BROKER365_CLIENT_ID`).

## 1. Register the app
1. Open <https://entra.microsoft.com> → **Identity → Applications → App registrations → New registration**.
2. **Name**: the name users will see on the consent screen (e.g. `Hermes 365 Connector`).
3. **Supported account types**: *Accounts in any organizational directory (Any Microsoft Entra ID
   tenant – Multitenant) and personal Microsoft accounts*.
4. **Redirect URI**: platform **Public client/native (mobile & desktop)**, value `http://localhost`.
5. Click **Register** and copy the **Application (client) ID**.

## 2. Authentication blade
- **Advanced settings → Allow public client flows → Yes**. Device-code sign-in needs this.
- Check that `http://localhost` is listed under *Mobile and desktop applications*.
- **Don't** create a client secret or certificate.

## 3. API permissions → Microsoft Graph → Delegated
`User.Read`, `offline_access`, `openid`, `profile`, `Files.ReadWrite`, `Files.ReadWrite.All`,
`Sites.ReadWrite.All`, `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`,
`Contacts.ReadWrite`, `Tasks.ReadWrite`, `Notes.ReadWrite`.

broker365 asks for these incrementally, so a user only consents to the bundles they use.
Clicking "Grant admin consent" in *your* tenant doesn't affect other tenants.

## 4. Plug in the client ID
Either set `DEFAULT_CLIENT_ID` in `src/broker365/config.py` before you publish, or have users
set `BROKER365_CLIENT_ID=<id>`.

## 5. Before a public release
- **Branding & properties**: logo, home page, terms of service and privacy URLs.
- **Publisher verification**: link a Microsoft Cloud Partner Program (MPN) ID and verify a domain.
  Without it the consent screen says *unverified*, and many work/school tenants block users from
  consenting.
- When a tenant blocks user consent, its admin can approve the app for everyone through
  `broker365 admin-consent-url <tenant-domain>`.

## Don't
- Add Application (app-only) permissions.
- Add client secrets or certificates. The client ID of a public client is not a secret.
- Add Web or SPA redirect platforms.
