// broker365 local dashboard. Vanilla JS, no dependencies.
// All server-provided text is rendered through textContent (via h()), never innerHTML.

'use strict';

/* ------------------------------------------------------------------ *
 * DOM helpers
 * ------------------------------------------------------------------ */

const BOOL_PROPS = new Set(['disabled', 'checked', 'hidden', 'required', 'readOnly', 'open']);

/** h('button', {class: 'btn', onClick: fn}, 'Label', child, ...) */
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key.startsWith('on') && typeof value === 'function') {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (BOOL_PROPS.has(key)) el[key] = Boolean(value);
      else if (key === 'value') el.value = value;
      else el.setAttribute(key, value === true ? '' : String(value));
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

function replace(el, ...children) {
  return append(clear(el), children);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Only allow http(s) URLs into href attributes. */
function safeUrl(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
  } catch {
    return null;
  }
}

function extLink(url, label) {
  const href = safeUrl(url);
  if (!href) return h('span', { class: 'break' }, label || String(url ?? ''));
  return h('a', { href, target: '_blank', rel: 'noopener noreferrer', class: 'break' }, label || href);
}

function fmtTime(ts) {
  if (ts === undefined || ts === null || ts === '') return { text: '—', iso: null };
  let date;
  if (typeof ts === 'number') date = new Date(ts < 1e12 ? ts * 1000 : ts);
  else date = new Date(ts);
  if (Number.isNaN(date.getTime())) return { text: String(ts), iso: null };
  return {
    text: date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' }),
    iso: date.toISOString(),
  };
}

function timeEl(ts) {
  const t = fmtTime(ts);
  return h('time', t.iso ? { datetime: t.iso } : null, t.text);
}

function fmtValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function badge(text, kind) {
  return h('span', { class: `badge${kind ? ' ' + kind : ''}` }, text);
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many || one + 's'}`;
}

/* ------------------------------------------------------------------ *
 * API
 * ------------------------------------------------------------------ */

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

let sessionGone = false;

async function api(path, { method = 'GET', body } = {}) {
  if (sessionGone) throw new ApiError('Session expired', 401);
  const opts = {
    method,
    credentials: 'same-origin',
    headers: { 'X-Broker365': '1', Accept: 'application/json' },
  };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError('Could not reach broker365. Check that `broker365 ui` is still running.', 0);
  }
  if (res.status === 401) {
    showSessionExpired();
    throw new ApiError('Session expired', 401);
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    const msg = data && typeof data.error === 'string' && data.error
      ? data.error
      : `Request failed (HTTP ${res.status}).`;
    throw new ApiError(msg, res.status);
  }
  return data;
}

/** GET that shares one pending request per path (avoids duplicate fetches on first load). */
const inflight = new Map();
function apiGet(path) {
  let pending = inflight.get(path);
  if (!pending) {
    pending = api(path).finally(() => inflight.delete(path));
    inflight.set(path, pending);
  }
  return pending;
}

/**
 * Load `path` for a page section. Returns the data ({} for an empty body), or null when
 * the request failed (an error box with `retry` replaces `host`, after optional `head`)
 * or navigation moved on.
 */
async function load(host, ctx, path, retry, head = null) {
  try {
    const data = await apiGet(path);
    return ctx.alive() ? data ?? {} : null;
  } catch (err) {
    if (ctx.alive()) replace(host, head, errorBox(err, retry));
    return null;
  }
}

function showSessionExpired() {
  if (sessionGone) return;
  sessionGone = true;
  cancelActiveFlow();
  replace(
    document.body,
    h('div', { class: 'session-gone' },
      h('div', { class: 'panel', role: 'alert' },
        h('h1', null, 'Session expired'),
        h('p', null,
          'Session expired — run ', h('code', null, 'broker365 ui'),
          ' again and use the printed link.'),
      ),
    ),
  );
  document.title = 'Session expired – broker365';
}

/** Disable a button while an async action runs. */
async function withBusy(button, busyLabel, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  if (busyLabel) button.textContent = busyLabel;
  try {
    return await fn();
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.removeAttribute('aria-busy');
      if (busyLabel) button.textContent = label;
    }
  }
}

/** Run a button action: busy state, then a success toast (okMsg) or an error toast. */
function onAction(button, busyLabel, fn, okMsg) {
  return withBusy(button, busyLabel, async () => {
    try {
      await fn();
      if (okMsg) toast(okMsg);
    } catch (err) {
      toastError(err);
    }
  });
}

/* ------------------------------------------------------------------ *
 * Toasts and dialogs
 * ------------------------------------------------------------------ */

function toast(message, kind = 'ok') {
  if (sessionGone) return;
  const host = document.getElementById('toasts');
  if (!host) return;
  const close = h('button', { type: 'button', 'aria-label': 'Dismiss' }, '×');
  const el = h('div', { class: `toast${kind === 'ok' ? '' : ` ${kind}`}` }, h('span', null, message), close);
  const remove = () => el.remove();
  close.addEventListener('click', remove);
  host.append(el);
  setTimeout(remove, kind === 'ok' ? 4500 : 9000);
}

function toastError(err) {
  if (err && err.status === 401) return;
  toast(err && err.message ? err.message : String(err), 'bad');
}

function confirmDialog({ title, message, confirmLabel, danger = false }) {
  return new Promise((resolve) => {
    const cancelBtn = h('button', { type: 'button', class: 'btn' }, 'Cancel');
    const okBtn = h('button', { type: 'button', class: `btn ${danger ? 'danger solid' : 'primary'}` }, confirmLabel);
    const titleId = `dlg-${Date.now()}`;
    const dialog = h('dialog', { 'aria-labelledby': titleId },
      h('h2', { id: titleId }, title),
      h('p', null, message),
      h('div', { class: 'row end' }, cancelBtn, okBtn),
    );
    let result = false;
    cancelBtn.addEventListener('click', () => dialog.close());
    okBtn.addEventListener('click', () => { result = true; dialog.close(); });
    dialog.addEventListener('close', () => { dialog.remove(); resolve(result); });
    document.body.append(dialog);
    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
      cancelBtn.focus();
    } else {
      dialog.remove();
      resolve(window.confirm(`${title}\n\n${message}`));
    }
  });
}

/* ------------------------------------------------------------------ *
 * Shared vocabulary
 * ------------------------------------------------------------------ */

const BUNDLES = {
  files: { label: 'OneDrive files', desc: 'Your OneDrive, including Excel, Word and PowerPoint files' },
  'files-shared': { label: 'Files shared with me', desc: 'Files and folders other people shared with you' },
  sharepoint: { label: 'SharePoint', desc: 'Team sites and document libraries (work or school)' },
  mail: { label: 'Mail', desc: 'Read mail and write drafts' },
  calendar: { label: 'Calendar', desc: 'Events and availability' },
  contacts: { label: 'Contacts', desc: 'Your Outlook address book' },
  tasks: { label: 'To Do', desc: 'Microsoft To Do lists and tasks' },
  onenote: { label: 'OneNote', desc: 'Notebooks, sections and pages' },
};
const BUNDLE_ORDER = Object.keys(BUNDLES);

function bundleLabel(key) {
  return BUNDLES[key] ? BUNDLES[key].label : key || 'Other';
}

function accountTypeBadge(type) {
  if (type === 'personal') return badge('Personal', 'slate');
  if (type === 'work_or_school') return badge('Work or school', 'slate');
  return badge(type ? String(type) : 'Unknown');
}

function signInBadge(signIn) {
  if (signIn === 'valid') return badge('Signed in', 'ok');
  return badge('Needs sign-in', 'bad');
}

function hermesConnected(hermes) {
  return Boolean(hermes && hermes.found && hermes.mcp_server && hermes.skill);
}

function pageHead(title, lede, ...extra) {
  return h('div', { class: 'page-head' },
    h('div', null, h('h1', null, title), lede ? h('p', { class: 'lede' }, lede) : null),
    extra.length ? h('div', { class: 'actions' }, extra) : null,
  );
}

function errorBox(err, retry) {
  return h('div', { class: 'error-box', role: 'alert' },
    h('p', null, h('strong', null, 'Could not load this page. '), err && err.message ? err.message : String(err)),
    retry ? h('p', { class: 'row' }, h('button', { type: 'button', class: 'btn small', onClick: retry }, 'Try again')) : null,
  );
}

/** Empty state: first line is the title (bold when a hint follows), the rest are muted hints. */
function emptyPanel(...lines) {
  return h('div', { class: 'panel empty-panel' }, lines.map((line, i) => (i
    ? h('p', { class: 'muted' }, line)
    : h('p', null, lines.length > 1 ? h('strong', null, line) : line))));
}

/**
 * Responsive table: columns are labels or {label, class, hideLabel}; cellFn(row) returns
 * one cell content per column. Each td gets its column's data-label for the stacked layout.
 */
function dataTable(columns, rows, cellFn, { rowClass, wrapClass } = {}) {
  const cols = columns.map((c) => (typeof c === 'string' ? { label: c } : c));
  return h('div', { class: wrapClass ? `table-wrap ${wrapClass}` : 'table-wrap' },
    h('table', { class: 'stack' },
      h('thead', null, h('tr', null, cols.map((c) => h('th', { scope: 'col' },
        c.hideLabel ? h('span', { class: 'visually-hidden' }, c.label) : c.label)))),
      h('tbody', null, rows.map((row) => {
        const cells = cellFn(row);
        return h('tr', { class: rowClass ? rowClass(row) : null },
          cols.map((c, i) => h('td', { 'data-label': c.label, class: c.class }, cells[i])));
      }))));
}

/** Case-insensitive search: true when q is empty or any field contains it. */
function matches(q, ...fields) {
  return !q || fields.some((v) => v && String(v).toLowerCase().includes(q));
}

/* ------------------------------------------------------------------ *
 * Header: default account indicator
 * ------------------------------------------------------------------ */

function updateWhoami(accounts) {
  const el = document.getElementById('whoami-value');
  if (!el) return;
  const list = Array.isArray(accounts) ? accounts : [];
  const def = list.find((a) => a.default) || null;
  el.classList.toggle('none', !def);
  if (def) {
    el.textContent = def.username || 'Unnamed account';
    el.title = def.username || '';
  } else {
    el.textContent = list.length ? 'No default set' : 'Not signed in';
    el.title = '';
  }
}

async function refreshWhoami() {
  try {
    // Shares the request when the current page is already loading the overview.
    const ov = await apiGet('/api/overview');
    updateWhoami(ov && ov.accounts);
  } catch {
    /* header stays as-is; pages show their own errors */
  }
}

/* ------------------------------------------------------------------ *
 * Page: Overview
 * ------------------------------------------------------------------ */

async function renderOverview(view, ctx) {
  const [ovRes, actRes] = await Promise.allSettled([
    apiGet('/api/overview'),
    apiGet('/api/activity?limit=5'),
  ]);
  if (!ctx.alive()) return;
  if (ovRes.status === 'rejected') {
    replace(view, pageHead('Overview'), errorBox(ovRes.reason, ctx.reload));
    return;
  }
  const ov = ovRes.value || {};
  const accounts = Array.isArray(ov.accounts) ? ov.accounts : [];
  updateWhoami(accounts);

  const def = accounts.find((a) => a.default);
  const unhealthy = accounts.filter((a) => a.sign_in !== 'valid');

  const accountsCell = h('section', { class: 'board-cell', 'aria-labelledby': 'ov-acc' },
    h('h2', { id: 'ov-acc' }, 'Accounts'),
    h('div', { class: 'board-value' }, accounts.length ? plural(accounts.length, 'account') : 'None connected'),
    def ? h('p', { class: 'break' }, h('span', { class: 'star', 'aria-hidden': 'true' }, '★ '), def.username) : null,
    accounts.length
      ? h('p', null, unhealthy.length
        ? badge(`${unhealthy.length} need${unhealthy.length === 1 ? 's' : ''} sign-in`, 'bad')
        : badge('All signed in', 'ok'))
      : h('p', { class: 'muted' }, 'Connect a Microsoft account so Hermes can reach your files.'),
    h('div', { class: 'actions' }, h('a', { class: 'btn small', href: '#/accounts' }, accounts.length ? 'Manage accounts' : 'Connect an account')),
  );

  const modeCell = h('section', { class: 'board-cell', 'aria-labelledby': 'ov-mode' },
    h('h2', { id: 'ov-mode' }, 'Mode'),
    h('div', { class: 'board-value' }, ov.read_only ? 'Read-only' : 'Read and write'),
    h('p', { class: 'muted' }, ov.read_only
      ? 'Hermes can look but not change anything.'
      : 'Hermes can create and edit files, drafts and events.'),
    h('div', { class: 'actions' }, h('a', { class: 'btn small', href: '#/settings' }, 'Change mode')),
  );

  const hermes = ov.hermes || {};
  const connected = hermesConnected(hermes);
  const hermesCell = h('section', { class: 'board-cell', 'aria-labelledby': 'ov-hermes' },
    h('h2', { id: 'ov-hermes' }, 'Hermes integration'),
    h('div', { class: 'board-value' }, connected ? 'Connected' : hermes.found ? 'Not set up' : 'Hermes not found'),
    h('p', null, connected ? badge('MCP server and skill installed', 'ok') : badge('Action needed', 'brass')),
    h('div', { class: 'actions' }, h('a', { class: `btn small${connected ? '' : ' primary'}`, href: '#/hermes' },
      connected ? 'View integration' : 'Set up Hermes')),
  );

  const toolsCell = h('section', { class: 'board-cell', 'aria-labelledby': 'ov-tools' },
    h('h2', { id: 'ov-tools' }, 'Tools'),
    h('div', { class: 'board-value' }, typeof ov.tools_count === 'number' ? plural(ov.tools_count, 'tool') : '—'),
    h('p', { class: 'muted' }, 'Available to Hermes with the current accounts and mode.'),
    h('div', { class: 'actions' }, h('a', { class: 'btn small', href: '#/tools' }, 'Browse tools')),
  );

  let recent;
  if (actRes.status === 'rejected') {
    recent = h('p', { class: 'muted' }, `Activity could not be loaded: ${actRes.reason.message}`);
  } else {
    const entries = Array.isArray(actRes.value) ? actRes.value : [];
    recent = entries.length
      ? h('ul', { class: 'activity-mini' }, entries.map((e) => h('li', null,
        timeEl(e.ts),
        h('span', { class: 'mono break' }, e.tool || '—'),
        e.ok ? badge('OK', 'ok') : badge('Failed', 'bad'),
      )))
      : h('p', { class: 'muted' }, 'Nothing yet. Changes Hermes makes in Microsoft 365 will be listed here.');
  }

  replace(view,
    pageHead('Overview', null),
    h('div', { class: 'meta-line', 'aria-label': 'Version details' },
      h('span', null, `Version ${ov.version || 'unknown'}`),
      h('span', null, 'Client ID ', h('code', null, ov.client_id || '—')),
      ov.home ? h('span', null, 'Data folder ', h('code', null, ov.home)) : null,
    ),
    h('div', { class: 'section' }, h('div', { class: 'board' }, accountsCell, modeCell, hermesCell, toolsCell)),
    h('section', { class: 'section', 'aria-labelledby': 'ov-recent' },
      h('div', { class: 'row section-title' }, h('h2', { id: 'ov-recent' }, 'Recent activity'),
        h('a', { href: '#/activity', class: 'small' }, 'See all activity')),
      h('div', { class: 'panel' }, recent),
    ),
  );
}

/* ------------------------------------------------------------------ *
 * Page: Accounts
 * ------------------------------------------------------------------ */

let activeFlow = null;

/** Stop a sign-in flow's polling and countdown. */
function stopFlow(flow) {
  flow.cancelled = true;
  clearInterval(flow.timer);
  if (activeFlow === flow) activeFlow = null;
}

function cancelActiveFlow() {
  if (activeFlow) stopFlow(activeFlow);
}

async function renderAccounts(view, ctx) {
  const tableHost = h('div', null, h('p', { class: 'loading' }, 'Loading accounts…'));
  const connectHost = h('div', { class: 'panel' }, h('p', { class: 'loading' }, 'Loading permissions…'));

  replace(view,
    pageHead('Accounts', 'Microsoft accounts broker365 can act for. Tokens stay on this machine.'),
    h('section', { 'aria-labelledby': 'acc-list' },
      h('h2', { id: 'acc-list', class: 'visually-hidden' }, 'Connected accounts'),
      tableHost),
    h('section', { class: 'section', 'aria-labelledby': 'acc-connect' },
      h('h2', { id: 'acc-connect' }, 'Connect an account'),
      h('p', { class: 'hint' },
        'Choose what Hermes may use, then sign in with a code. To add permissions to an account you already connected, sign in to it again with more boxes ticked.'),
      connectHost),
  );

  const reloadTable = () => loadAccountsTable(tableHost, ctx);
  await Promise.all([reloadTable(), loadConnectPanel(connectHost, ctx, reloadTable)]);
}

async function loadAccountsTable(host, ctx) {
  const ov = await load(host, ctx, '/api/overview', () => loadAccountsTable(host, ctx));
  if (!ov) return;
  const accounts = Array.isArray(ov.accounts) ? ov.accounts : [];
  updateWhoami(accounts);
  renderAccountsTable(host, accounts, ctx);
}

function renderAccountsTable(host, accounts, ctx) {
  if (!accounts.length) {
    replace(host, emptyPanel('No accounts connected yet.', 'Pick what Hermes may use below and start sign-in.'));
    return;
  }

  const columns = ['Account', 'Type', 'Sign-in', 'Default', { label: 'Actions', hideLabel: true }];
  replace(host, dataTable(columns, accounts, (acc) => {
    const makeDefault = acc.default
      ? h('span', { class: 'badge brass' }, h('span', { 'aria-hidden': 'true' }, '★'), 'Default')
      : h('button', { type: 'button', class: 'btn small' }, 'Make default');
    if (!acc.default) {
      makeDefault.addEventListener('click', () => onAction(makeDefault, 'Saving…', async () => {
        const res = await api('/api/accounts/default', { method: 'POST', body: { username: acc.username } });
        const list = Array.isArray(res && res.accounts) ? res.accounts : null;
        if (list && ctx.alive()) { updateWhoami(list); renderAccountsTable(host, list, ctx); }
        else await loadAccountsTable(host, ctx);
      }, `${acc.username} is now the default account`));
    }

    const signOut = h('button', { type: 'button', class: 'btn small danger' }, 'Sign out');
    signOut.addEventListener('click', async () => {
      const ok = await confirmDialog({
        title: 'Sign out of this account?',
        message: `broker365 will delete the saved tokens for ${acc.username}. Hermes loses access to it until you sign in again.`,
        confirmLabel: 'Sign out',
        danger: true,
      });
      if (!ok) return;
      await onAction(signOut, 'Signing out…', async () => {
        await api('/api/accounts/logout', { method: 'POST', body: { username: acc.username } });
        await loadAccountsTable(host, ctx);
      }, `Signed out of ${acc.username}`);
    });

    return [
      [h('strong', { class: 'break' }, acc.username || 'Unnamed account'),
        acc.tenant_id ? h('span', { class: 'sub' }, 'Tenant ', h('code', null, acc.tenant_id)) : null],
      accountTypeBadge(acc.account_type),
      [signInBadge(acc.sign_in),
        acc.sign_in === 'valid' ? null : h('span', { class: 'sub' }, String(acc.sign_in || 'Unknown error'))],
      makeDefault,
      signOut,
    ];
  }, { rowClass: (acc) => (acc.default ? 'is-default' : null) }));
}

async function loadConnectPanel(host, ctx, reloadTable) {
  const scopes = await load(host, ctx, '/api/scopes', () => loadConnectPanel(host, ctx, reloadTable));
  if (!scopes) return;

  const available = Object.keys(scopes.bundles || {});
  const keys = [
    ...BUNDLE_ORDER.filter((k) => available.includes(k)),
    ...available.filter((k) => !BUNDLE_ORDER.includes(k)),
  ];
  if (!keys.length) keys.push(...BUNDLE_ORDER);

  const boxes = keys.map((key) => {
    const input = h('input', { type: 'checkbox', name: 'bundle', value: key, checked: key === 'files' });
    const perms = scopes.bundles && Array.isArray(scopes.bundles[key]) ? scopes.bundles[key] : [];
    return h('label', { class: 'bundle', title: perms.length ? `Permissions: ${perms.join(', ')}` : null },
      input,
      h('span', null,
        h('span', { class: 'b-name' }, bundleLabel(key)),
        h('span', { class: 'b-key' }, BUNDLES[key] ? BUNDLES[key].desc : key)));
  });

  const startBtn = h('button', { type: 'submit', class: 'btn primary' }, 'Start sign-in');
  const flowHost = h('div', { 'aria-live': 'polite' });
  const form = h('form', null,
    h('fieldset', { class: 'bundles' }, h('legend', null, 'Permissions'), boxes),
    h('div', { class: 'row' }, startBtn,
      h('span', { class: 'muted small' }, 'Works for personal and work or school accounts.')),
  );

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const chosen = [...form.querySelectorAll('input[name="bundle"]:checked')].map((i) => i.value);
    if (!chosen.length) {
      toast('Tick at least one permission to sign in.', 'bad');
      return;
    }
    cancelActiveFlow();
    let flow = null;
    await onAction(startBtn, 'Starting…', async () => {
      flow = await api('/api/login/start', { method: 'POST', body: { scopes: chosen } });
    });
    // Start the flow only after onAction has re-enabled the button, so the flow can keep it
    // disabled while a code is on screen.
    if (flow && ctx.alive()) runSignInFlow(flowHost, flow, ctx, reloadTable, startBtn);
  });

  replace(host, form, flowHost);
}

function codeTiles(code) {
  return h('div', { class: 'code' },
    h('span', { class: 'visually-hidden' }, `Code: ${code}`),
    code.split('').map((ch) => (ch === '-' || ch === ' '
      ? h('span', { class: 'gap', 'aria-hidden': 'true' }, ch === '-' ? '-' : '')
      : h('span', { 'aria-hidden': 'true' }, ch))));
}

function runSignInFlow(host, flow, ctx, reloadTable, startBtn) {
  const code = String(flow.user_code || '');
  const total = Math.max(1, Number(flow.expires_in) || 900);
  const state = { cancelled: false, timer: null, deadline: Date.now() + total * 1000 };
  activeFlow = state;

  const copyBtn = h('button', { type: 'button', class: 'btn' }, 'Copy code');
  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(code);
      toast('Code copied');
    } catch {
      toast('Copy failed. Select the code and copy it manually.', 'bad');
    }
  });

  const cancelBtn = h('button', { type: 'button', class: 'btn' }, 'Cancel');
  const progress = h('progress', { max: String(total), value: String(total), 'aria-label': 'Time left to enter the code' });
  const remaining = h('span', null);
  const statusText = h('span', { class: 'row' }, h('span', { class: 'pulse', 'aria-hidden': 'true' }), 'Waiting for you to approve in the browser');

  const card = h('div', { class: 'keytag' },
    h('h3', null, 'Finish signing in'),
    h('ol', null,
      h('li', null, 'Open ', extLink(flow.verification_uri, flow.verification_uri)),
      h('li', null, 'Enter this code and pick your Microsoft account.'),
      h('li', null, 'Approve the permissions. This page updates by itself.')),
    h('div', { class: 'code-row' }, codeTiles(code), copyBtn),
    progress,
    h('div', { class: 'status-line' }, statusText, remaining),
    h('div', { class: 'row' }, cancelBtn),
  );
  const link = card.querySelector('a');
  if (link) link.classList.add('verify');
  replace(host, card);
  startBtn.disabled = true;

  const tick = () => {
    const left = Math.max(0, Math.round((state.deadline - Date.now()) / 1000));
    progress.value = left;
    const m = Math.floor(left / 60);
    const s = String(left % 60).padStart(2, '0');
    remaining.textContent = `Code expires in ${m}:${s}`;
  };
  tick();
  state.timer = setInterval(tick, 1000);

  const finish = (ok, title, detail) => {
    stopFlow(state);
    if (startBtn.isConnected) startBtn.disabled = false;
    if (!host.isConnected) return;
    replace(host, h('div', { class: `keytag ${ok ? 'done' : 'failed'}`, role: ok ? 'status' : 'alert' },
      h('h3', null, title),
      detail ? h('p', { class: 'break' }, detail) : null,
      ok ? null : h('p', { class: 'muted small' }, 'Pick your permissions above and start sign-in again.')));
  };

  cancelBtn.addEventListener('click', () => {
    finish(false, 'Sign-in cancelled', 'The code was not used. Nothing changed.');
  });

  (async () => {
    while (!state.cancelled) {
      if (Date.now() > state.deadline) {
        finish(false, 'The code expired', 'Codes are valid for a limited time.');
        toast('Sign-in code expired', 'bad');
        return;
      }
      let res;
      try {
        res = await api('/api/login/complete', { method: 'POST', body: { flow_id: flow.flow_id } });
      } catch (err) {
        if (state.cancelled || err.status === 401) return;
        finish(false, 'Sign-in failed', err.message);
        toastError(err);
        return;
      }
      if (state.cancelled) return;
      const status = res && res.status;
      if (status === 'signed_in') {
        const who = res.username || 'your account';
        finish(true, `Signed in as ${who}`, 'Hermes can use this account now.');
        toast(`Signed in as ${who}`);
        if (ctx.alive()) await reloadTable();
        return;
      }
      if (status !== 'pending') {
        const msg = (res && (res.error || res.message)) || `Sign-in stopped (${status || 'unknown status'}).`;
        finish(false, 'Sign-in failed', msg);
        toast(msg, 'bad');
        return;
      }
      await sleep(1000);
    }
  })();
}

/* ------------------------------------------------------------------ *
 * Page: Settings
 * ------------------------------------------------------------------ */

async function renderSettings(view, ctx) {
  const settings = await load(view, ctx, '/api/settings', ctx.reload, pageHead('Settings'));
  if (!settings) return;
  const body = h('div');
  replace(view, pageHead('Settings', 'What Hermes may change, and which local folders it can touch.'), body);
  renderSettingsBody(body, settings, ctx);
}

function renderSettingsBody(body, s, ctx) {
  /** POST a settings patch from `control`, then redraw with the returned settings.
   *  `okMsg` is a string, or a function of the returned settings giving [message, kind]. */
  const save = (control, busyLabel, patch, okMsg, onError) => onAction(control, busyLabel, async () => {
    const next = await api('/api/settings', { method: 'POST', body: patch })
      .catch((err) => { if (onError) onError(); throw err; });
    if (typeof okMsg === 'function' && next) toast(...okMsg(next));
    if (ctx.alive() && next) renderSettingsBody(body, next, ctx);
  }, typeof okMsg === 'function' ? null : okMsg);

  // Read-only mode (locked when BROKER365_READ_ONLY is set in the environment)
  const envLocked = Boolean(s.read_only_env);
  const toggle = h('input', {
    type: 'checkbox', role: 'switch', class: 'switch', id: 'ro-toggle',
    checked: Boolean(s.read_only), disabled: envLocked,
    'aria-describedby': envLocked ? 'ro-help ro-env' : 'ro-help',
  });
  toggle.addEventListener('change', () => {
    if (envLocked) { toggle.checked = Boolean(s.read_only); return; }
    const want = toggle.checked;
    const was = Boolean(s.read_only);
    save(toggle, null, { read_only: want }, (next) => {
      const now = Boolean(next.read_only);
      if (now !== was) return [now ? 'Read-only mode on' : 'Read-only mode off', 'ok'];
      return [`Read-only mode is still ${now ? 'on' : 'off'}${next.read_only_env ? ' (set by BROKER365_READ_ONLY in the environment)' : ''}`, 'warn'];
    }, () => { toggle.checked = !want; });
  });
  const readOnly = h('section', { class: 'panel', 'aria-labelledby': 'ro-title' },
    h('div', { class: 'switch-row' },
      h('div', null,
        h('h2', { id: 'ro-title' }, h('label', { for: 'ro-toggle' }, 'Read-only mode')),
        h('p', { id: 'ro-help', class: 'muted' },
          'When on, every tool that writes, sends, moves or deletes is hidden from Hermes. It can still search and read. Restart Hermes after changing this.'),
        envLocked ? h('p', { id: 'ro-env', class: 'small muted' }, 'Set by BROKER365_READ_ONLY in the environment') : null),
      toggle));

  // Workspace
  const workspace = h('section', { class: 'panel', 'aria-labelledby': 'ws-title' },
    h('h2', { id: 'ws-title' }, 'Workspace'),
    h('p', { class: 'muted' }, 'Files Hermes downloads or prepares locally are kept here.'),
    h('p', { class: 'section-gap' }, h('code', { class: 'break' }, s.workspace || '—')));

  // Allowed folders
  const dirs = Array.isArray(s.allowed_dirs) ? s.allowed_dirs : [];
  const dirList = dirs.length
    ? h('ul', { class: 'dir-list' }, dirs.map((dir) => {
      const rm = h('button', { type: 'button', class: 'btn small danger', 'aria-label': `Remove ${dir}` }, 'Remove');
      rm.addEventListener('click', () => save(rm, 'Removing…', { remove_dir: dir }, 'Folder removed'));
      return h('li', null, h('span', { class: 'path' }, dir), rm);
    }))
    : h('p', { class: 'muted dir-empty' }, 'No extra folders. Hermes can only use the workspace.');

  const dirInput = h('input', { type: 'text', id: 'dir-input', name: 'dir', placeholder: 'C:\\Users\\you\\Documents\\Reports', autocomplete: 'off', spellcheck: 'false' });
  const addBtn = h('button', { type: 'submit', class: 'btn' }, 'Add folder');
  const addForm = h('form', { class: 'field' },
    h('label', { for: 'dir-input' }, 'Add a folder'),
    h('div', { class: 'row' }, h('div', { class: 'grow-input' }, dirInput), addBtn),
    h('span', { class: 'hint' }, 'Use a full local path. Network shares are refused.'));
  addForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const value = dirInput.value.trim();
    if (!value) { dirInput.focus(); return; }
    await save(addBtn, 'Adding…', { add_dir: value }, 'Folder added');
  });

  const folders = h('section', { class: 'panel', 'aria-labelledby': 'dirs-title' },
    h('h2', { id: 'dirs-title' }, 'Allowed folders'),
    h('p', { class: 'muted' }, 'Local folders Hermes may read from or save into, besides the workspace.'),
    h('div', { class: 'section-gap' }, dirList),
    addForm);

  // Excel backend
  const backends = s.excel_backend && typeof s.excel_backend === 'object' ? Object.entries(s.excel_backend) : [];
  const resetBtn = h('button', { type: 'button', class: 'btn' }, 'Re-test Excel support');
  resetBtn.addEventListener('click', () => save(resetBtn, 'Resetting…', { reset_excel_probe: true },
    'Excel support will be re-tested on the next Excel request'));
  const methodBadge = (mode) => (mode === 'workbook' ? badge('Live cell editing', 'ok')
    : mode === 'openpyxl' ? badge('Download, edit, upload', 'brass') : badge(String(mode)));
  const excel = h('section', { class: 'panel', 'aria-labelledby': 'xl-title' },
    h('h2', { id: 'xl-title' }, 'Excel editing'),
    h('p', { class: 'muted' },
      'broker365 checks once per account whether Excel can be edited live in the cloud. If not, it downloads the workbook, edits it and uploads it again.'),
    backends.length
      ? dataTable([{ label: 'Account', class: 'break' }, 'Method'], backends,
        ([user, mode]) => [user, methodBadge(mode)], { wrapClass: 'section-gap' })
      : h('p', { class: 'section-gap' }, 'Not tested yet. The check runs on the first Excel request.'),
    h('div', { class: 'row section-gap' }, resetBtn));

  replace(body, readOnly, workspace, folders, excel);
}

/* ------------------------------------------------------------------ *
 * Page: Hermes
 * ------------------------------------------------------------------ */

async function renderHermes(view, ctx) {
  const statusHost = h('div', { class: 'panel' }, h('p', { class: 'loading' }, 'Checking Hermes…'));
  const resultHost = h('div', { 'aria-live': 'polite' });
  const installBtn = h('button', { type: 'button', class: 'btn primary' }, 'Install / repair integration');

  replace(view,
    pageHead('Hermes', 'Hermes reaches Microsoft 365 through the m365 MCP server and the broker365 skill.'),
    h('section', { 'aria-labelledby': 'hm-status' },
      h('h2', { id: 'hm-status', class: 'visually-hidden' }, 'Integration status'),
      statusHost),
    h('section', { class: 'section', 'aria-labelledby': 'hm-install' },
      h('h2', { id: 'hm-install' }, 'Install or repair'),
      h('p', { class: 'hint' }, 'Adds the m365 server to your Hermes config and installs the skill. Your existing config is backed up first. Safe to run again.'),
      h('div', { class: 'row' }, installBtn),
      resultHost),
  );

  const loadStatus = async () => {
    const st = await load(statusHost, ctx, '/api/hermes', loadStatus);
    if (st) renderHermesStatus(statusHost, st);
  };

  installBtn.addEventListener('click', () => onAction(installBtn, 'Installing…', async () => {
    const res = await api('/api/hermes/install', { method: 'POST', body: {} }).catch((err) => {
      if (ctx.alive()) replace(resultHost, h('div', { class: 'error-box section-gap', role: 'alert' },
        h('p', null, h('strong', null, 'Install failed. '), err.message)));
      throw err;
    });
    if (!ctx.alive()) return;
    toast('Hermes integration installed');
    renderInstallResult(resultHost, res || {});
    await loadStatus();
  }));

  await loadStatus();
}

function checkItem(ok, title, detail) {
  return h('li', null,
    h('span', { class: `check ${ok ? 'yes' : 'no'}`, 'aria-hidden': 'true' }, ok ? '✓' : '!'),
    h('div', null,
      h('strong', null, title),
      h('span', { class: 'visually-hidden' }, ok ? ' (done)' : ' (missing)'),
      detail ? h('p', { class: 'muted small break' }, detail) : null));
}

function renderHermesStatus(host, st) {
  const found = Boolean(st.found);
  replace(host,
    h('ul', { class: 'checklist' },
      checkItem(found,
        found ? 'Hermes found' : 'Hermes not found',
        found ? h('span', null, 'at ', h('code', null, st.home || '—'))
          : 'Install Hermes first, or set HERMES_HOME to its folder.'),
      checkItem(Boolean(st.mcp_server), 'MCP server registered',
        st.mcp_server ? 'The m365 server is in config.yaml.'
          : 'The m365 server is not in your Hermes config yet.'),
      checkItem(Boolean(st.skill), 'Skill installed',
        st.skill ? 'Hermes knows how to use the broker365 tools.'
          : 'The broker365 skill is not installed yet.')),
    hermesConnected(st) ? h('p', { class: 'callout section-gap' }, 'Everything is in place. Ask Hermes to list your OneDrive files to try it.') : null,
  );
}

function renderInstallResult(host, res) {
  const rows = [
    ['Hermes folder', res.hermes_home],
    ['Config file', res.config],
    ['MCP server', res.mcp_server],
    ['Skill', res.skill],
  ];
  if (res.backup) rows.push(['Backup of old config', res.backup]);
  const next = Array.isArray(res.next) ? res.next : res.next ? [res.next] : [];
  replace(host, h('div', { class: 'panel section-gap', role: 'status' },
    h('h3', null, 'Integration installed'),
    h('dl', { class: 'kv section-gap' }, rows.flatMap(([k, v]) => [h('dt', null, k), h('dd', null, fmtValue(v))])),
    next.length ? h('div', { class: 'section-gap' }, h('p', null, h('strong', null, 'Next')),
      h('ul', null, next.map((n) => h('li', { class: 'break' }, fmtValue(n))))) : null,
    h('p', { class: 'callout section-gap' }, h('strong', null, 'Restart Hermes'), ' so it loads the m365 server and the skill.')));
}

/* ------------------------------------------------------------------ *
 * Page: Activity
 * ------------------------------------------------------------------ */

function targetSummary(target) {
  if (!target || typeof target !== 'object') return target ? String(target) : '';
  return Object.entries(target)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join(', ');
}

function resultCell(result) {
  if (!result || typeof result !== 'object') return h('span', { class: 'muted' }, '—');
  const url = result.web_url || result.webUrl || result.url || result.link;
  const id = result.id || result.item_id || result.message_id || result.event_id;
  const parts = [];
  if (id) parts.push(h('code', { class: 'break' }, String(id)));
  if (url) parts.push(h('span', { class: 'sub' }, extLink(String(url), 'Open')));
  if (!parts.length) {
    const summary = targetSummary(result);
    parts.push(summary ? h('span', { class: 'break small' }, summary) : h('span', { class: 'muted' }, '—'));
  }
  return parts;
}

async function renderActivity(view, ctx) {
  const listHost = h('div', null, h('p', { class: 'loading' }, 'Loading activity…'));
  const countEl = h('p', { class: 'muted small', 'aria-live': 'polite' });
  const filter = h('input', { type: 'search', id: 'act-filter', placeholder: 'Filter by tool, target or error', autocomplete: 'off' });
  const refreshBtn = h('button', { type: 'button', class: 'btn' }, 'Refresh');
  let entries = [];

  const draw = () => {
    const q = filter.value.trim().toLowerCase();
    const shown = entries.filter((e) => matches(q, e.tool, e.error, targetSummary(e.target), targetSummary(e.result)));
    countEl.textContent = entries.length
      ? (q ? `Showing ${shown.length} of ${plural(entries.length, 'entry', 'entries')}` : plural(entries.length, 'entry', 'entries'))
      : '';
    if (!entries.length) {
      replace(listHost, emptyPanel('No activity yet.',
        'Each time Hermes changes something in Microsoft 365, broker365 records the tool, time and target here. File contents are never recorded.'));
      return;
    }
    if (!shown.length) {
      replace(listHost, emptyPanel('No entries match this filter.'));
      return;
    }
    const columns = [{ label: 'Time', class: 'nowrap' }, 'Tool', 'Outcome', { label: 'Target', class: 'break' }, 'Result'];
    replace(listHost, dataTable(columns, shown, (e) => [
      timeEl(e.ts),
      h('code', null, e.tool || '—'),
      [e.ok ? badge('OK', 'ok') : badge('Failed', 'bad'),
        !e.ok && e.error ? h('span', { class: 'sub' }, String(e.error)) : null],
      targetSummary(e.target) || h('span', { class: 'muted' }, '—'),
      resultCell(e.result),
    ]));
  };

  const fetchEntries = async () => {
    const data = await load(listHost, ctx, '/api/activity?limit=200',
      () => withBusy(refreshBtn, 'Refreshing…', fetchEntries));
    if (!data) return;
    entries = Array.isArray(data) ? data : [];
    draw();
  };

  filter.addEventListener('input', draw);
  refreshBtn.addEventListener('click', () => withBusy(refreshBtn, 'Refreshing…', async () => {
    await fetchEntries();
    if (ctx.alive()) toast('Activity refreshed');
  }));

  replace(view,
    pageHead('Activity', 'Changes made through broker365, newest first: what Hermes wrote, plus sign-ins and settings changes (admin:…).'),
    h('div', { class: 'toolbar' },
      h('label', { for: 'act-filter', class: 'visually-hidden' }, 'Filter activity'),
      h('div', { class: 'grow' }, filter), refreshBtn),
    countEl,
    listHost);

  await withBusy(refreshBtn, null, fetchEntries);
}

/* ------------------------------------------------------------------ *
 * Page: Tools
 * ------------------------------------------------------------------ */

async function renderTools(view, ctx) {
  const data = await load(view, ctx, '/api/tools', ctx.reload, pageHead('Tools'));
  if (!data) return;
  const tools = Array.isArray(data) ? data : [];

  const visible = tools.filter((t) => !t.hidden).length;
  const search = h('input', { type: 'search', id: 'tool-search', placeholder: 'Search tools', autocomplete: 'off' });
  const listHost = h('div', { 'aria-live': 'polite' });

  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const shown = tools.filter((t) => matches(q, t.name, t.description, t.bundle, bundleLabel(t.bundle)));
    if (!tools.length) {
      replace(listHost, emptyPanel('No tools reported by the server.'));
      return;
    }
    if (!shown.length) {
      replace(listHost, emptyPanel('No tools match this search.'));
      return;
    }
    const groups = new Map();
    for (const t of shown) {
      const key = t.bundle || 'other';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(t);
    }
    const ordered = [...groups.keys()].sort((a, b) => {
      const ia = BUNDLE_ORDER.indexOf(a); const ib = BUNDLE_ORDER.indexOf(b);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
    });
    replace(listHost, ordered.map((key) => {
      const items = groups.get(key).slice().sort((a, b) => String(a.name).localeCompare(String(b.name)));
      const id = `tg-${key.replace(/[^a-z0-9-]/gi, '')}`;
      return h('section', { class: 'tool-group', 'aria-labelledby': id },
        h('h2', { id }, bundleLabel(key), h('span', { class: 'count' }, plural(items.length, 'tool'))),
        h('ul', { class: 'tools' }, items.map((t) => h('li', { class: t.hidden ? 'is-hidden' : null },
          h('div', { class: 't-head' },
            h('span', { class: 't-name' }, t.name || '—'),
            t.writes ? badge('Writes', 'brass') : badge('Read only'),
            t.destructive ? badge('Destructive', 'bad') : null,
            t.hidden ? h('span', { class: 'badge slate', title: 'Hermes cannot see this tool with the current mode or permissions' }, 'Hidden from Hermes') : null),
          t.description ? h('p', { class: 't-desc' }, t.description) : null))));
    }));
  };

  search.addEventListener('input', draw);

  replace(view,
    pageHead('Tools', `${plural(tools.length, 'tool')} in total, ${visible} visible to Hermes right now.`),
    h('div', { class: 'toolbar' },
      h('label', { for: 'tool-search', class: 'visually-hidden' }, 'Search tools'),
      h('div', { class: 'grow' }, search)),
    listHost);
  draw();
}

/* ------------------------------------------------------------------ *
 * Router
 * ------------------------------------------------------------------ */

const ROUTES = {
  overview: { title: 'Overview', render: renderOverview },
  accounts: { title: 'Accounts', render: renderAccounts },
  settings: { title: 'Settings', render: renderSettings },
  hermes: { title: 'Hermes', render: renderHermes },
  activity: { title: 'Activity', render: renderActivity },
  tools: { title: 'Tools', render: renderTools },
};

let renderSeq = 0;
let firstRender = true;

function currentRoute() {
  const name = (location.hash || '').replace(/^#\/?/, '').split(/[/?]/)[0];
  return ROUTES[name] ? name : null;
}

async function route() {
  if (sessionGone) return;
  const name = currentRoute();
  if (!name) {
    location.replace('#/overview');
    return;
  }
  const page = ROUTES[name];
  const seq = ++renderSeq;
  cancelActiveFlow();

  for (const a of document.querySelectorAll('#nav a')) {
    if (a.dataset.route === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  document.title = `${page.title} – broker365`;

  const main = document.getElementById('main');
  const view = h('div', { class: 'view' }, h('p', { class: 'loading' }, 'Loading…'));
  replace(main, view);
  const ctx = {
    alive: () => seq === renderSeq && !sessionGone,
    reload: () => route(),
  };
  if (!firstRender) main.focus({ preventScroll: true });
  firstRender = false;

  try {
    await page.render(view, ctx);
  } catch (err) {
    if (ctx.alive()) replace(view, pageHead(page.title), errorBox(err, ctx.reload));
  }
}

window.addEventListener('hashchange', route);

function start() {
  if (!currentRoute()) history.replaceState(null, '', '#/overview');
  route();
  refreshWhoami();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
else start();
