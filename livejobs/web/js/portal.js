import { $, ago, api, ApiError, chrome, copyButton, fmtDate, fmtDateTime, h, safeUrl, session, toast, VENDOR_LABEL } from '/static/js/common.js';

const user = await chrome('portal');
if (!user) location.replace('/login?next=' + encodeURIComponent('/portal' + location.hash));

const cfg = await api('/public/config', { auth: false }).catch(() => ({ mcp_url: '', vendors: [] }));
$('#who').textContent = user.email;

const TABS = [
  ['connect', 'Connect your agent'], ['preferences', 'Preferences'], ['saved', 'Saved jobs'],
  ['applications', 'Applications'], ['account', 'Account'],
];
// Admin-only tab. Hiding it is a courtesy: every /api/admin/* endpoint checks the role on the server.
if (user && user.role === 'admin') TABS.push(['boards', 'Job boards']);
const counts = {};
const panel = $('#panel');
const nav = $('#side-nav');

function drawNav(active) {
  nav.replaceChildren(...TABS.map(([key, label]) =>
    h('a', { href: '#' + key, 'aria-current': key === active ? 'page' : false },
      h('span', { text: label }), counts[key] != null ? h('span', { class: 'count', text: String(counts[key]) }) : null)));
}

const views = { connect, preferences, saved, applications, account, ...(user && user.role === 'admin' ? { boards: adminBoards } : {}) };
async function route() {
  const key = views[location.hash.slice(1)] ? location.hash.slice(1) : 'connect';
  drawNav(key);
  panel.replaceChildren(h('p', { class: 'muted ui', text: 'Loading' }));
  try { panel.replaceChildren(await views[key]()); }
  catch (ex) { panel.replaceChildren(h('p', { class: 'form-error', text: ex.message })); }
  document.title = TABS.find(([k]) => k === key)[1] + ' | livejobs';
  drawNav(key);
}
addEventListener('hashchange', route);

const head = (title, lede) => [h('h1', { text: title }), lede ? h('p', { class: 'lede', text: lede }) : null];

// ───────────────── connect ─────────────────
let freshToken = '';
async function connect() {
  const tokens = await api('/me/mcp-tokens');
  const wrap = h('div');
  wrap.append(...head('Connect your agent',
    'Your agent gets a personal token that opens your matches and your application record. It never needs your résumé or contact details to talk to livejobs.'));

  // step 1: token
  const tokenArea = h('div');
  const nameInput = h('input', { class: 'input', id: 'tok-name', value: 'My agent', maxlength: '100' });
  const create = h('button', { class: 'btn btn-stamp', type: 'button', text: 'Create token' });
  create.addEventListener('click', async () => {
    create.disabled = true;
    try {
      const t = await api('/me/mcp-tokens', { method: 'POST', body: { name: nameInput.value.trim() || 'My agent' } });
      freshToken = t.token;
      await route();
    } catch (ex) { toast(ex.message, true); create.disabled = false; }
  });
  tokenArea.append(
    h('div', { class: 'field' }, h('label', { for: 'tok-name', text: 'Token name' }), nameInput,
      h('p', { class: 'hint', text: 'A label so you can tell your agents apart, such as “Claude Code on my laptop”.' })),
    create);
  if (freshToken) {
    tokenArea.append(h('div', { class: 'token-box', role: 'status' },
      h('strong', { class: 'ui', text: 'Copy your token now. It’s shown only once.' }),
      h('code', { text: freshToken }), h('div', {}, copyButton(() => freshToken, 'Copy token'))));
  }
  wrap.append(h('section', { class: 'block' }, h('h2', { text: '1. Create a token' }), tokenArea));

  // step 2: client snippets
  const url = cfg.mcp_url || (location.origin + '/mcp/');
  const T = freshToken || 'YOUR_TOKEN';
  const clients = {
    'Claude Code': { note: 'Run in your terminal:', text: `claude mcp add --transport http livejobs ${url} --header "Authorization: Bearer ${T}"` },
    'Claude Desktop': { note: 'Add to claude_desktop_config.json (needs Node.js, which runs the mcp-remote bridge):',
      text: JSON.stringify({ mcpServers: { livejobs: { command: 'npx', args: ['-y', 'mcp-remote', url, '--header', `Authorization: Bearer ${T}`] } } }, null, 2) },
    'Codex': { note: 'Add to ~/.codex/config.toml, and keep the token in an environment variable:',
      text: `[mcp_servers.livejobs]\nurl = "${url}"\nbearer_token_env_var = "LIVEJOBS_TOKEN"\n\n# in your shell profile:\nexport LIVEJOBS_TOKEN="${T}"` },
    'Gemini CLI': { note: 'Add to ~/.gemini/settings.json:',
      text: JSON.stringify({ mcpServers: { livejobs: { httpUrl: url, headers: { Authorization: `Bearer ${T}` } } } }, null, 2) },
    'Other': { note: 'Any MCP client that supports remote servers with a custom header:', text: `URL:     ${url}\nHeader:  Authorization: Bearer ${T}\nTransport: streamable HTTP` },
  };
  const snippetBox = h('div');
  const tabs = h('div', { class: 'tabs', role: 'tablist' });
  const show = (name) => {
    [...tabs.children].forEach((b) => b.setAttribute('aria-selected', String(b.textContent === name)));
    const c = clients[name];
    snippetBox.replaceChildren(
      h('p', { class: 'hint snip-note', text: c.note }),
      h('div', { class: 'codeblock' }, h('pre', { text: c.text }), copyButton(() => c.text)));
  };
  for (const name of Object.keys(clients)) tabs.append(h('button', { type: 'button', role: 'tab', text: name, onclick: () => show(name) }));
  show('Claude Code');
  wrap.append(h('section', { class: 'block' }, h('h2', { text: '2. Add livejobs to your agent' }),
    freshToken ? null : h('p', { class: 'hint', text: 'Create a token above and it will be filled into these commands. Client settings change over time; if one doesn’t work, look up how your client adds a remote MCP server with a bearer header.' }),
    tabs, snippetBox));

  // step 3: what to say
  const ask = 'Use the livejobs tools to find jobs that match my preferences and apply to up to 5 of them. Use my résumé at <path to your résumé> and the contact details I’ve given you. Don’t send my résumé or contact details to livejobs. Read each form before you submit it. If it asks for anything I haven’t given you, such as visa sponsorship, disability, veteran status, race, gender or any custom question, ask me and wait for my answer; never guess, and offer me “decline to answer” on the self-identification questions. If I can’t be reached, skip that job and tell me which question stopped you.';
  wrap.append(h('section', { class: 'block' }, h('h2', { text: '3. Ask your agent to apply' }),
    h('p', { text: 'Keep your résumé somewhere your agent can read it, such as a file on your computer or in its project. livejobs doesn’t see it. Then say something like:' }),
    h('div', { class: 'codeblock' }, h('pre', { text: ask }), copyButton(() => ask)),
    h('p', { class: 'hint', text: 'Your agent can also search any job by title or keyword, save jobs for you and change your preferences, all through the same connection.' })));

  // existing tokens
  const tbody = h('tbody');
  for (const t of tokens) {
    const revoke = h('button', { class: 'btn btn-danger btn-sm', type: 'button', text: 'Revoke' });
    revoke.addEventListener('click', async () => {
      if (!confirm(`Revoke “${t.name || t.prefix}”? Any agent using it stops working immediately.`)) return;
      try { await api(`/me/mcp-tokens/${t.id}`, { method: 'DELETE' }); freshToken = ''; toast('Token revoked'); route(); }
      catch (ex) { toast(ex.message, true); }
    });
    tbody.append(h('tr', {}, h('td', { text: t.name || '(unnamed)' }), h('td', { class: 'muted', text: t.prefix + '…' }),
      h('td', { class: 'muted', text: t.last_used_at ? 'Used ' + ago(t.last_used_at) : 'Never used' }),
      h('td', {}, h('div', { class: 'row-actions' }, revoke))));
  }
  wrap.append(h('section', { class: 'block' }, h('h2', { text: 'Your tokens' }),
    tokens.length
      ? h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ...['Name', 'Starts with', 'Last used', ''].map((x) => h('th', { text: x })))), tbody)
      : h('p', { class: 'muted ui', text: 'No tokens yet. Create one above to connect an agent.' })));
  return wrap;
}

// ───────────────── preferences ─────────────────
function chipsField(id, label, hint, values, placeholder) {
  const list = [...values];
  const box = h('div', { class: 'chips' });
  const input = h('input', { id, placeholder, 'aria-label': label });
  const draw = () => {
    box.replaceChildren(...list.map((v, i) => h('span', { class: 'chip' }, v,
      h('button', { type: 'button', 'aria-label': `Remove ${v}`, text: '×', onclick: () => { list.splice(i, 1); draw(); input.focus(); } }))), input);
  };
  const add = () => { const v = input.value.trim().replace(/,$/, ''); if (v && !list.includes(v)) list.push(v); input.value = ''; draw(); };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(); input.focus(); }
    else if (e.key === 'Backspace' && !input.value && list.length) { list.pop(); draw(); input.focus(); }
  });
  input.addEventListener('blur', add);
  draw();
  return { el: h('div', { class: 'field' }, h('label', { for: id, text: label }), box, hint ? h('p', { class: 'hint', text: hint }) : null), get: () => (add(), [...list]) };
}

async function preferences() {
  const p = await api('/me/preferences');
  const wrap = h('div');
  wrap.append(...head('Preferences', 'Tell livejobs what to look for. Your agent sees only jobs that match, and none you’ve already applied to.'));

  const f = {
    title_include: chipsField('p-ti', 'Job titles or keywords to look for', 'Press Enter after each one. A job needs to match at least one. Add * to match endings: engineer* finds engineering.', p.title_include, 'data engineer*'),
    title_exclude: chipsField('p-te', 'Skip titles containing', null, p.title_exclude, 'intern'),
    locations: chipsField('p-loc', 'Places', 'Leave empty for anywhere.', p.locations, 'Berlin'),
    companies_include: chipsField('p-ci', 'Only these companies', 'Leave empty for any company.', p.companies_include, 'Acme'),
    companies_exclude: chipsField('p-ce', 'Skip these companies', null, p.companies_exclude, 'Globex'),
    countries: chipsField('p-co', 'Countries', 'Two-letter codes, like US, GB, DE or CA. Leave empty for anywhere.', p.countries || [], 'US'),
    description_exclude: chipsField('p-de', 'Skip descriptions mentioning', 'For example “security clearance” or “visa sponsorship not available”.', p.description_exclude, 'security clearance'),
  };
  const remote = h('input', { type: 'checkbox', id: 'p-remote' }); remote.checked = p.remote_only;
  const age = h('select', { class: 'select', id: 'p-age' }, ...[['', 'Any time'], ['3', 'Last 3 days'], ['7', 'Last week'], ['14', 'Last 2 weeks'], ['30', 'Last 30 days'], ['60', 'Last 60 days']]
    .map(([v, t]) => h('option', { value: v, text: t })));
  age.value = p.max_age_days ? String(p.max_age_days) : '';
  const TIERS = [['intern', 'Intern'], ['entry', 'Entry level'], ['mid', 'Mid (no level in the title)'], ['senior', 'Senior'], ['lead', 'Lead, principal or manager'], ['executive', 'Director and above']];
  const tierChecks = TIERS.map(([v, label]) => {
    const cb = h('input', { type: 'checkbox', value: v }); cb.checked = (p.seniority || []).includes(v);
    return { v, cb, el: h('label', { class: 'check' }, cb, label) };
  });
  const agency = h('input', { type: 'checkbox', id: 'p-agency' }); agency.checked = !!p.exclude_agencies;
  const cap = h('input', { class: 'input', type: 'number', id: 'p-cap', min: '0', max: '500', value: String(p.max_applications_per_day) });
  const vendorChecks = cfg.vendors.map((v) => {
    const cb = h('input', { type: 'checkbox', value: v }); cb.checked = p.vendors.includes(v);
    return { v, cb, el: h('label', { class: 'check' }, cb, VENDOR_LABEL[v] || v) };
  });

  const err = h('p', { class: 'form-error', role: 'alert' });
  const peek = h('div', { class: 'match-peek' });
  const save = h('button', { class: 'btn btn-stamp', type: 'submit', text: 'Save preferences' });

  const form = h('form', { novalidate: true },
    h('section', { class: 'block' }, h('h2', { text: 'What to look for' }), f.title_include.el, f.title_exclude.el,
      h('div', { class: 'two' }, f.locations.el, h('div', { class: 'field' }, h('span', { class: 'label', text: 'Remote' }), h('label', { class: 'check' }, remote, 'Only remote jobs')))),
    h('section', { class: 'block' }, h('h2', { text: 'Level, country and agencies' }),
      h('div', { class: 'field' }, h('span', { class: 'label', text: 'Level' }), h('div', { class: 'check-grid' }, ...tierChecks.map((x) => x.el)),
        h('p', { class: 'hint', text: 'Worked out from the job title. Leave all unticked for any level.' })),
      f.countries.el,
      h('div', { class: 'field' }, h('label', { class: 'check' }, agency, 'Skip staffing and recruiting agencies'),
        h('p', { class: 'hint', text: 'Hides postings from companies that look like agencies rather than the employer.' }))),
    h('section', { class: 'block' }, h('h2', { text: 'Narrow it down' }), h('div', { class: 'two' }, f.companies_include.el, f.companies_exclude.el),
      f.description_exclude.el,
      h('div', { class: 'two' }, h('div', { class: 'field' }, h('label', { for: 'p-age', text: 'Posted' }), age),
        h('div', { class: 'field' }, h('label', { for: 'p-cap', text: 'Most applications per day' }), cap,
          h('p', { class: 'hint', text: 'Your agent can’t start more than this in a day. 0 pauses applying.' }))),
      h('div', { class: 'field' }, h('span', { class: 'label', text: 'Hiring platforms' }), h('div', { class: 'check-grid' }, ...vendorChecks.map((x) => x.el)),
        h('p', { class: 'hint', text: 'Leave all unticked to include every platform.' }))),
    err, save, peek);

  form.addEventListener('submit', async (e) => {
    e.preventDefault(); err.textContent = ''; save.disabled = true;
    const capN = Number(cap.value);
    if (!Number.isInteger(capN) || capN < 0 || capN > 500) { err.textContent = 'Most applications per day must be a whole number from 0 to 500.'; save.disabled = false; return; }
    try {
      await api('/me/preferences', { method: 'PUT', body: {
        title_include: f.title_include.get(), title_exclude: f.title_exclude.get(), locations: f.locations.get(),
        companies_include: f.companies_include.get(), companies_exclude: f.companies_exclude.get(),
        description_exclude: f.description_exclude.get(), remote_only: remote.checked,
        seniority: tierChecks.filter((x) => x.cb.checked).map((x) => x.v), countries: f.countries.get(), exclude_agencies: agency.checked,
        max_age_days: age.value ? Number(age.value) : null, max_applications_per_day: capN,
        vendors: vendorChecks.filter((x) => x.cb.checked).map((x) => x.v),
      } });
      toast('Preferences saved');
      await showPeek(peek);
    } catch (ex) { err.textContent = ex.message; }
    save.disabled = false;
  });
  wrap.append(form);
  showPeek(peek);
  return wrap;
}

async function showPeek(box) {
  try {
    const r = await api('/me/matches?limit=5');
    box.replaceChildren(h('h2', { class: 'ui h-sm', text: 'Your matches right now' }),
      r.jobs.length
        ? h('div', {}, h('ul', {}, ...r.jobs.map((j) => h('li', {}, h('a', { href: '/jobs?job=' + j.id, text: j.title }), ' at ' + (j.company || 'a company')))),
            r.next_cursor ? h('p', { class: 'hint mt3', text: 'Showing the newest five. Your agent can page through the rest.' }) : null)
        : h('p', { class: 'muted ui', text: 'No open jobs match yet. Add a job title above, or widen your filters.' }));
  } catch { box.replaceChildren(); }
}

// ───────────────── saved ─────────────────
async function saved() {
  const jobs = await api('/me/saved?limit=200');
  counts.saved = jobs.length;
  const wrap = h('div');
  wrap.append(...head('Saved jobs', 'Jobs you or your agent bookmarked. Saving doesn’t apply to anything.'));
  if (!jobs.length) {
    wrap.append(h('p', { class: 'empty' }, h('b', { text: 'Nothing saved yet.' }), 'Press Save on any job to keep it here. ', h('a', { href: '/jobs', text: 'Browse jobs' })));
    return wrap;
  }
  const ul = h('ul', { class: 'job-list' });
  for (const j of jobs) {
    const url = safeUrl(j.url);
    const remove = h('button', { class: 'btn btn-quiet btn-sm', type: 'button', text: 'Remove' });
    remove.addEventListener('click', async () => {
      try { await api(`/me/saved/${j.id}`, { method: 'DELETE' }); toast('Removed from saved jobs'); route(); } catch (ex) { toast(ex.message, true); }
    });
    ul.append(h('li', { class: 'job job-static' },
      h('div', {}, h('a', { class: 'job-title job-link', href: j.open ? '/jobs?job=' + j.id : '/jobs', text: j.title })),
      h('div', { class: 'job-side' }, j.open ? null : h('span', { class: 'pill pill-mute', text: 'Closed' }), remove),
      h('div', { class: 'job-meta' }, h('span', { text: j.company }), j.location ? h('span', { text: j.location }) : null,
        j.open && url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: 'Company page' }) : null)));
  }
  wrap.append(ul);
  return wrap;
}

// ───────────────── applications ─────────────────
const STATUS = {
  in_progress: ['Started', 'pill-stamp'], applied: ['Applied', 'pill-ok'], failed: ['Failed', 'pill-bad'],
  skipped: ['Skipped', 'pill-mute'], interview: ['Interview', 'pill-ok'], offer: ['Offer', 'pill-ok'], rejected: ['Rejected', 'pill-mute'],
};
async function applications() {
  const wrap = h('div');
  wrap.append(...head('Applications', 'The record of what your agent did. This is all livejobs keeps: the job, a status and a date.'));
  const filter = h('select', { class: 'select narrow', id: 'a-status', 'aria-label': 'Filter by status' },
    h('option', { value: '', text: 'All statuses' }), ...Object.entries(STATUS).map(([v, [t]]) => h('option', { value: v, text: t })));
  const out = h('div', { class: 'mt4' });
  async function draw() {
    const rows = await api('/me/applications?limit=200' + (filter.value ? '&status=' + filter.value : ''));
    if (!filter.value) { counts.applications = rows.length; drawNav('applications'); }
    if (!rows.length) {
      out.replaceChildren(h('p', { class: 'empty' }, h('b', { text: filter.value ? 'Nothing with that status.' : 'No applications yet.' }),
        filter.value ? 'Try another status.' : 'Once your agent applies, each job shows up here. Connect it from the first tab.'));
      return;
    }
    out.replaceChildren(h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ...['Job', 'Status', 'Date', 'Note'].map((x) => h('th', { text: x })))),
      h('tbody', {}, ...rows.map((a) => {
        const [label, cls] = STATUS[a.status] || [a.status, 'pill-mute'];
        return h('tr', {},
          h('td', {}, h('a', { href: '/jobs?job=' + a.job.id, text: a.job.title }), h('div', { class: 'muted', text: a.job.company })),
          h('td', {}, h('span', { class: 'pill ' + cls, text: label })),
          h('td', { class: 'muted', text: fmtDate(a.applied_at || a.created_at) }),
          h('td', { class: 'muted', text: a.notes || '' }));
      }))));
  }
  filter.addEventListener('change', draw);
  wrap.append(filter, out);
  await draw();
  return wrap;
}

// ───────────────── account ─────────────────
async function account() {
  const wrap = h('div');
  wrap.append(...head('Account'));
  wrap.append(h('section', { class: 'block' }, h('h2', { text: 'Sign-in' }), h('p', { class: 'ui', text: user.email }),
    h('p', { class: 'hint', text: 'This email and your preferences are all livejobs knows about you. See the full list on the ', }, h('a', { href: '/privacy', text: 'privacy page' }), '.')));

  const cur = h('input', { class: 'input', id: 'pw-cur', type: 'password', autocomplete: 'current-password' });
  const nw = h('input', { class: 'input', id: 'pw-new', type: 'password', autocomplete: 'new-password', minlength: '10' });
  const perr = h('p', { class: 'form-error', role: 'alert' });
  const pform = h('form', { novalidate: true },
    h('div', { class: 'field' }, h('label', { for: 'pw-cur', text: 'Current password' }), cur),
    h('div', { class: 'field' }, h('label', { for: 'pw-new', text: 'New password' }), nw, h('p', { class: 'hint', text: 'At least 10 characters. Changing it signs you out everywhere. Your agent tokens keep working.' })),
    perr, h('button', { class: 'btn', type: 'submit', text: 'Change password' }));
  pform.addEventListener('submit', async (e) => {
    e.preventDefault(); perr.textContent = '';
    try {
      await api('/me/password', { method: 'PUT', body: { current_password: cur.value, new_password: nw.value } });
      session.clear(); location.href = '/login';
    } catch (ex) { perr.textContent = ex.message; }
  });
  wrap.append(h('section', { class: 'block' }, h('h2', { text: 'Password' }), pform));

  const out = h('button', { class: 'btn btn-quiet', type: 'button', text: 'Sign out' });
  out.addEventListener('click', async () => { try { await api('/auth/logout', { method: 'POST' }); } catch { /* token already gone */ } session.clear(); location.href = '/'; });
  wrap.append(h('section', { class: 'block' }, h('h2', { text: 'Sign out' }), out));

  const dpw = h('input', { class: 'input', id: 'del-pw', type: 'password', autocomplete: 'current-password' });
  const derr = h('p', { class: 'form-error', role: 'alert' });
  const dform = h('form', { novalidate: true },
    h('p', { text: 'This erases your preferences, saved jobs, application record and every agent token, immediately. It can’t be undone.' }),
    h('div', { class: 'field' }, h('label', { for: 'del-pw', text: 'Confirm with your password' }), dpw),
    derr, h('button', { class: 'btn btn-danger', type: 'submit', text: 'Delete my account' }));
  dform.addEventListener('submit', async (e) => {
    e.preventDefault(); derr.textContent = '';
    if (!confirm('Delete your account and everything in it?')) return;
    try { await api('/me/delete', { method: 'POST', body: { password: dpw.value } }); session.clear(); location.href = '/'; }
    catch (ex) { derr.textContent = ex.message; }
  });
  wrap.append(h('section', { class: 'block' }, h('h2', { text: 'Delete account' }), dform));
  return wrap;
}

// ───────────────── admin: job boards ─────────────────
const SOURCE = { manual: ['Added by hand', 'pill-stamp'], crawl: ['Found by Common Crawl', 'pill-mute'] };
const BOARD_STATUS = { enabled: ['Syncing', 'pill-ok'], discovered: ['Not syncing', 'pill-mute'], disabled: ['Disabled', 'pill-bad'] };

async function adminBoards() {
  const [vendors, inv, counts] = await Promise.all([api('/admin/vendors'), api('/admin/inventory'), api('/admin/boards/counts')]);
  const byVendor = Object.fromEntries(vendors.map((v) => [v.vendor, v]));
  const wrap = h('div');
  wrap.append(...head('Job boards', 'Every board is fetched automatically, whether Common Crawl found it or you add it by its slug here. Disable any you don’t want.'));

  // ── counters: how many boards there are ──
  const counterBox = h('dl', { class: 'counters', 'aria-label': 'Board counts' });
  function drawCounters(c) {
    const items = [['Boards added', c.total], ['Syncing now', c.enabled], ['Added by hand', c.manual],
      ['Found by Common Crawl', c.crawl], ['Not enabled yet', c.discovered], ['Disabled', c.disabled]];
    counterBox.replaceChildren(...items.map(([label, n]) => h('div', {}, h('dt', { text: label }), h('dd', { text: n.toLocaleString() }))));
  }
  async function refreshCounters() {
    try { drawCounters(await api('/admin/boards/counts')); } catch { /* the figures just stay as they were */ }
  }
  drawCounters(counts);
  wrap.append(counterBox);

  // ── Common Crawl refresh: the button, live progress, history ──
  const RUN = { complete: ['Complete', 'pill-ok'], partial: ['Incomplete', 'pill-bad'], failed: ['Failed', 'pill-bad'], running: ['Running', 'pill-stamp'] };
  const STARTED_BY = { web: 'This page', scheduled: 'The scheduler', cli: 'Command line' };
  const crawlSection = h('section', { class: 'block' });
  const crawlStatus = h('p', { class: 'mt4', role: 'status', 'aria-live': 'polite' });
  const crawlBar = h('progress', { class: 'bar', max: '1', value: '0', hidden: true, 'aria-label': 'Refresh progress' });
  const crawlBtn = h('button', { class: 'btn btn-stamp', type: 'button', text: 'Refresh from Common Crawl' });
  const crawlHistory = h('div', { class: 'mt4' });
  let wasRunning = !!inv.running, pollTimer = null;

  function drawHistory(runs) {
    crawlHistory.replaceChildren(...(runs.length ? [h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ...['Crawl', 'Started', 'Result', 'New boards', 'Pages', 'Started by'].map((x) => h('th', { text: x })))),
      h('tbody', {}, ...runs.map((r) => { const [t, cls] = RUN[r.status] || [r.status, 'pill-mute'];
        return h('tr', {}, h('td', { text: r.crawl }), h('td', { class: 'muted', text: fmtDate(r.started_at) }),
          h('td', {}, h('span', { class: 'pill ' + cls, text: t }), r.error ? h('div', { class: 'muted', text: r.error }) : null),
          h('td', { text: String(r.boards_new) }),
          h('td', { class: 'muted', text: `${r.pages_fetched}${r.pages_total ? ' of ' + r.pages_total : ''}${r.pages_failed ? `, ${r.pages_failed} failed` : ''}` }),
          h('td', { class: 'muted', text: STARTED_BY[r.trigger] || r.trigger })); })))] : []));
  }
  function paintCrawl(st) {
    drawHistory(st.runs);
    const run = st.running;
    crawlBtn.disabled = !!run;
    crawlBtn.textContent = run ? 'Refresh running…' : 'Refresh from Common Crawl';
    crawlBar.hidden = !run;
    if (run) {
      crawlBar.max = Math.max(run.pages_total, 1); crawlBar.value = run.pages_fetched;
      if (run.pages_total === 0 && run.pages_fetched === 0) {
        crawlStatus.textContent = `Contacting Common Crawl for ${run.crawl}. It can take a minute or two to answer. You can leave this page; the refresh keeps going.`;
      } else crawlStatus.textContent = `Reading ${run.crawl}: ${run.pages_fetched.toLocaleString()} of ${run.pages_total.toLocaleString()} pages so far`
        + `${run.pages_failed ? `, ${run.pages_failed} failed` : ''}. Started ${ago(run.started_at)} by ${(STARTED_BY[run.trigger] || run.trigger).toLowerCase()}. The total grows as more of the index is listed.`;
    } else if (st.last_complete) {
      crawlStatus.textContent = `Last completed ${ago(st.last_complete)}, reading ${st.last_crawl}. A new index is published about once a month; boards it finds start syncing on their own.`;
    } else {
      crawlStatus.textContent = 'The inventory hasn’t been completed yet. It refreshes once a month on a schedule, or you can start it now. Boards it finds start syncing on their own.';
    }
  }
  async function pollCrawl() {
    clearTimeout(pollTimer);
    if (!document.contains(crawlSection)) return;  // the admin left this tab: stop polling
    try {
      const st = await api('/admin/inventory');
      paintCrawl(st);
      if (wasRunning && !st.running) {  // it just finished: say how it went, and refresh the figures
        const last = st.runs[0];
        toast(last && last.status === 'complete' ? `Refresh finished: ${last.boards_new.toLocaleString()} new ${last.boards_new === 1 ? 'board' : 'boards'} found`
          : `Refresh ended: ${(RUN[last?.status] || [last?.status])[0].toLowerCase()}. See the history below.`, last && last.status !== 'complete');
        loadList(true);
      }
      wasRunning = !!st.running;
      if (st.running) { refreshCounters(); pollTimer = setTimeout(pollCrawl, 3000); }
    } catch { pollTimer = setTimeout(pollCrawl, 6000); }
  }
  crawlBtn.addEventListener('click', async () => {
    crawlBtn.disabled = true;
    try {
      const r = await api('/admin/inventory/refresh', { method: 'POST' });
      toast(r.status === 'started' ? 'Refresh started' : r.message);
      if (r.status !== 'started') crawlStatus.textContent = r.message;
      wasRunning = r.status === 'started';
    } catch (ex) {
      toast(ex.message, true);  // already running, or Common Crawl unreachable: the message says which
      if (ex.status === 409) wasRunning = true;
    }
    await pollCrawl();
    if (!wasRunning) crawlBtn.disabled = false;
  });
  crawlSection.append(h('h2', { text: 'Common Crawl refresh' }),
    h('p', { text: 'Common Crawl is the web archive this inventory is discovered from. A refresh reads its newest index and adds the company job boards it finds. It can take a long time, keeps running if you leave this page, and only one can run at a time.' }),
    h('div', { class: 'cta-row' }, crawlBtn), crawlStatus, crawlBar, crawlHistory);
  wrap.append(crawlSection);

  // ── platform health: open postings per platform vs their usual range, plus the live check ──
  const HEALTH = { ok: ['Normal', 'pill-ok'], drop: ['Dropped', 'pill-bad'], spike: ['Jumped', 'pill-stamp'], learning: ['Learning', 'pill-mute'] };
  const healthBody = h('div', { class: 'mt4' });
  const healthBtn = h('button', { class: 'btn btn-quiet', type: 'button', text: 'Run live check now' });
  const healthNote = h('p', { class: 'muted ui', role: 'status', 'aria-live': 'polite' });
  let healthTimer = null;
  const fmt = (n) => (n == null ? '' : Number(n).toLocaleString());
  function paintHealth(rows) {
    if (!rows.length) { healthBody.replaceChildren(h('p', { class: 'muted ui', text: 'Nothing to show yet. Counts are recorded after each hourly sync, and the live check runs daily.' })); return; }
    healthBody.replaceChildren(h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ...['Platform', 'Open postings', 'Usually', 'Change', 'Postings look', 'Live check'].map((x) => h('th', { text: x })))),
      h('tbody', {}, ...rows.map((r) => {
        const [lbl, cls] = HEALTH[r.state] || [r.state, 'pill-mute'];
        const c = r.canary;
        return h('tr', {},
          h('td', {}, r.label, r.boards_failing ? h('div', { class: 'muted', text: `${r.boards_failing} board${r.boards_failing === 1 ? '' : 's'} failing` }) : null),
          h('td', { text: fmt(r.active_jobs) }),
          h('td', { class: 'muted', text: r.mean == null ? 'still learning' : fmt(Math.round(r.mean)) }),
          h('td', { class: 'muted', text: r.change == null ? '' : `${r.change > 0 ? '+' : ''}${Math.round(r.change * 100)}%` }),
          h('td', {}, h('span', { class: 'pill ' + cls, text: lbl })),
          h('td', {}, c ? [h('span', { class: 'pill ' + (c.ok ? 'pill-ok' : 'pill-bad'), text: c.ok ? 'Working' : 'Failing' }),
            h('div', { class: 'muted', text: c.ok ? `${c.board}, ${c.jobs} postings, ${ago(c.checked_at)}` : `${c.board}: ${c.error || 'failed'}` })]
            : h('span', { class: 'muted', text: 'no board to check with' })));
      }))));
  }
  async function loadHealth() {
    try { paintHealth(await api('/admin/health')); } catch (ex) { healthNote.textContent = ex.message; }
  }
  healthBtn.addEventListener('click', async () => {
    healthBtn.disabled = true; healthNote.textContent = 'Checking every platform against a live board. This takes a minute or two.';
    try {
      await api('/admin/health/canary', { method: 'POST' });
      let tries = 0;
      const tick = async () => {
        await loadHealth();
        if (++tries < 40) healthTimer = setTimeout(tick, 4000); else { healthBtn.disabled = false; healthNote.textContent = ''; }
      };
      healthTimer = setTimeout(tick, 4000);
      setTimeout(() => { clearTimeout(healthTimer); healthBtn.disabled = false; healthNote.textContent = 'Done. Results are in the table.'; loadHealth(); }, 90000);
    } catch (ex) { healthNote.textContent = ex.message; healthBtn.disabled = false; }
  });
  wrap.append(h('section', { class: 'block' }, h('h2', { text: 'Platform health' }),
    h('p', { text: 'Each platform’s open postings are compared with the previous week. A sudden drop usually means a platform changed something and its parser needs a fix. A daily live check also fetches a known board per platform and confirms real postings still come out.' }),
    healthBtn, healthNote, healthBody));
  await loadHealth();
  paintCrawl(inv);
  if (inv.running) pollTimer = setTimeout(pollCrawl, 3000);

  // ── add one ──
  const platform = h('select', { class: 'select', id: 'ab-vendor' }, h('option', { value: '', text: 'Choose a platform' }),
    ...vendors.map((v) => h('option', { value: v.vendor, text: v.label })));
  const slug = h('input', { class: 'input', id: 'ab-slug', placeholder: 'Choose a platform first', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' });
  const url = h('input', { class: 'input', id: 'ab-url', type: 'url', placeholder: 'https://boards.greenhouse.io/stripe', autocomplete: 'off' });
  const interval = h('input', { class: 'input narrow', id: 'ab-int', type: 'number', min: '5', placeholder: 'Default (60)' });
  const slugHint = h('p', { class: 'hint', text: 'Pick a platform to see where its slug comes from.' });
  platform.addEventListener('change', () => {
    const v = byVendor[platform.value];
    slug.placeholder = v ? v.example : 'Choose a platform first';
    slugHint.textContent = v ? v.hint : 'Pick a platform to see where its slug comes from.';
  });
  const result = h('div', { class: 'probe', role: 'status', 'aria-live': 'polite' });
  const err = h('p', { class: 'form-error', role: 'alert' });
  const checkBtn = h('button', { class: 'btn btn-quiet', type: 'button', text: 'Check board' });
  const addBtn = h('button', { class: 'btn btn-stamp', type: 'submit', text: 'Add and start syncing' });
  const skip = h('input', { type: 'checkbox', id: 'ab-skip' });

  // A pasted URL wins: send only it, as the form says. (Sending the platform too would be rejected if they disagree.)
  const target = () => (url.value.trim() ? { url: url.value.trim() } : { vendor: platform.value || null, slug: slug.value.trim() || null });
  const body = (verify) => ({ ...target(), verify, sync_interval_min: interval.value ? Number(interval.value) : null });
  const query = () => new URLSearchParams(Object.entries(target()).filter(([, v]) => v));

  checkBtn.addEventListener('click', async () => {
    err.textContent = ''; result.replaceChildren(h('p', { class: 'muted ui', text: 'Checking…' })); checkBtn.disabled = true;
    try {
      const r = await api('/admin/boards/probe?' + query());
      const link = safeUrl(r.public_url);
      result.replaceChildren(r.exists
        ? h('div', {}, h('p', { class: 'form-ok', text: `Found ${r.company || r.board}: ${r.job_count.toLocaleString()} open ${r.job_count === 1 ? 'job' : 'jobs'}.` }),
            r.sample.length ? h('p', { class: 'hint', text: 'For example: ' + r.sample.join('; ') }) : null,
            r.already ? h('p', { class: 'hint', text: `Already in the list (${BOARD_STATUS[r.already.status]?.[0] || r.already.status}, ${r.already.source === 'manual' ? 'added by hand' : 'found by Common Crawl'}). Adding it will enable it.` }) : null,
            link ? h('a', { href: link, target: '_blank', rel: 'noopener noreferrer', text: 'Open its careers page' }) : null)
        : h('p', { class: 'form-error', text: r.error || 'Not found.' }));
    } catch (ex) { result.replaceChildren(); err.textContent = ex.message; }
    checkBtn.disabled = false;
  });

  const addForm = h('form', { novalidate: true },
    h('div', { class: 'two' },
      h('div', { class: 'field' }, h('label', { for: 'ab-vendor', text: 'Platform' }), platform),
      h('div', { class: 'field' }, h('label', { for: 'ab-slug', text: 'Board slug' }), slug, slugHint)),
    h('div', { class: 'field' }, h('label', { for: 'ab-url', text: 'Or paste the careers page URL' }), url,
      h('p', { class: 'hint', text: 'If you paste a URL, the platform and slug above are ignored.' })),
    h('div', { class: 'field' }, h('label', { for: 'ab-int', text: 'Sync every (minutes)' }), interval,
      h('p', { class: 'hint', text: 'Leave empty for the default. Minimum 5.' })),
    h('div', { class: 'cta-row' }, checkBtn, addBtn),
    h('label', { class: 'check mt4' }, skip, 'Add without checking that it exists (for a board that is temporarily unreachable)'),
    err, result);
  addForm.addEventListener('submit', async (e) => {
    e.preventDefault(); err.textContent = ''; addBtn.disabled = true;
    try {
      const b = await api('/admin/boards', { method: 'POST', body: body(!skip.checked) });
      toast(`Added ${b.board}${b.job_count ? ` with ${b.job_count} jobs` : ''}`);
      slug.value = ''; url.value = ''; result.replaceChildren();
      await loadList(true);
    } catch (ex) { err.textContent = ex.message; }
    addBtn.disabled = false;
  });
  wrap.append(h('section', { class: 'block' }, h('h2', { text: 'Add a board' }), addForm));

  // ── add many ──
  const many = h('textarea', { class: 'input', id: 'ab-many', rows: '5', placeholder: 'greenhouse:stripe\nlever:spotify\nhttps://jobs.ashbyhq.com/notion' });
  const manyOut = h('div', { class: 'mt4' });
  const manyBtn = h('button', { class: 'btn', type: 'button', text: 'Add all' });
  const LABEL = { added: ['Added', 'pill-ok'], exists: ['Already added', 'pill-mute'], not_found: ['Not found', 'pill-bad'], invalid: ['Not understood', 'pill-bad'], duplicate_in_list: ['Repeated', 'pill-mute'] };
  const SHOWN = 200; // a table of thousands of rows would freeze the page; the counts say the rest
  const SUMMARY_WORDS = { added: 'added', exists: 'already added', not_found: 'not found', invalid: 'not understood', duplicate_in_list: 'repeated' };
  const summaryText = (s) => Object.entries(s || {}).map(([k, n]) => `${n.toLocaleString()} ${SUMMARY_WORDS[k] || k}`).join(', ');
  const problemTable = (rows, more) => {
    const shown = rows.slice(0, SHOWN);
    return h('div', {},
      h('table', { class: 'table' }, h('tbody', {}, ...shown.map((x) => {
        const [t, cls] = LABEL[x.status] || [x.status, 'pill-mute'];
        return h('tr', {}, h('td', {}, h('span', { class: 'pill ' + cls, text: t })), h('td', { text: x.input }),
          h('td', { class: 'muted', text: x.error || (x.job_count != null ? `${x.job_count} jobs` : '') }));
      }))),
      (rows.length > SHOWN || more) ? h('p', { class: 'hint', text: `Showing the first ${SHOWN} lines. The counts above cover everything.` }) : null);
  };
  const importBar = h('progress', { class: 'bar', max: '1', value: '0', hidden: true, 'aria-label': 'Import progress' });
  const importLine = h('p', { class: 'muted ui', role: 'status' });
  let importTimer = null;
  const showImport = (j) => {
    importBar.hidden = j.state !== 'running';
    importBar.value = j.total ? j.done / j.total : 0;
    const counts = summaryText(j.summary);
    if (j.state === 'running') {
      importLine.textContent = `Checking and adding: ${j.done.toLocaleString()} of ${j.total.toLocaleString()}${counts ? ' (' + counts + ')' : ''}. This keeps going if you leave the page.`;
      manyOut.replaceChildren(importBar, importLine);
      return;
    }
    importLine.textContent = j.state === 'failed'
      ? `Stopped after ${j.done.toLocaleString()} of ${j.total.toLocaleString()}: ${j.error}. What was already added is kept; paste the list again to continue.`
      : `Done: ${counts || 'nothing to add'}.`;
    manyOut.replaceChildren(importLine, j.problems.length ? problemTable(j.problems, j.problems_truncated) : null);
  };
  const pollImport = async (id) => {
    clearTimeout(importTimer);
    try {
      const j = await api('/admin/boards/import/' + id);
      showImport(j);
      if (j.state === 'running') { importTimer = setTimeout(() => pollImport(id), 2000); return; }
      manyBtn.disabled = false;
      await loadList(true);
    } catch (ex) { manyOut.replaceChildren(h('p', { class: 'form-error', text: ex.message })); manyBtn.disabled = false; }
  };
  manyBtn.addEventListener('click', async () => {
    const entries = many.value.split('\n').map((x) => x.trim()).filter(Boolean);
    if (!entries.length) return;
    manyBtn.disabled = true; manyOut.replaceChildren(h('p', { class: 'muted ui', text: 'Checking and adding…' }));
    try {
      const r = await api('/admin/boards/import', { method: 'POST', body: { entries, verify: true } });
      if (r.id) { pollImport(r.id); return; } // a long list runs in the background
      manyOut.replaceChildren(h('p', { class: 'muted ui', text: `Done: ${summaryText(r.summary)}.` }), problemTable(r.results, false));
      await loadList(true);
    } catch (ex) { manyOut.replaceChildren(h('p', { class: 'form-error', text: ex.message })); }
    manyBtn.disabled = false;
  });
  api('/admin/boards/import/current').then((j) => { // reopening the page while an import is still going
    if (j && j.id && j.state === 'running') { manyBtn.disabled = true; pollImport(j.id); }
  }).catch(() => {});
  wrap.append(h('section', { class: 'block' }, h('h2', { text: 'Add several at once' }),
    h('p', { class: 'hint', text: 'One per line: platform:slug or a careers page URL. Lines that don’t check out are reported and the rest are added. Added boards start syncing right away. A long list is processed in the background, with progress shown here.' }),
    h('div', { class: 'field' }, h('label', { for: 'ab-many', text: 'Boards' }), many), manyBtn, manyOut));

  // ── the list ──
  const fStatus = h('select', { class: 'select', 'aria-label': 'Status' }, ...[['', 'Any status'], ['enabled', 'Syncing'], ['discovered', 'Not syncing'], ['disabled', 'Disabled']].map(([v, t]) => h('option', { value: v, text: t })));
  const fSource = h('select', { class: 'select', 'aria-label': 'How it was added' }, ...[['', 'Any source'], ['manual', 'Added by hand'], ['crawl', 'Found by Common Crawl']].map(([v, t]) => h('option', { value: v, text: t })));
  const fVendor = h('select', { class: 'select', 'aria-label': 'Platform' }, h('option', { value: '', text: 'Any platform' }), ...vendors.map((v) => h('option', { value: v.vendor, text: v.label })));
  const SORTS = [['', 'Most referenced first'], ['board:asc', 'Board A to Z'], ['board:desc', 'Board Z to A'], ['status:asc', 'Status'],
    ['jobs:desc', 'Most jobs'], ['jobs:asc', 'Fewest jobs'], ['synced:desc', 'Synced most recently'], ['synced:asc', 'Synced longest ago']];
  const fSort = h('select', { class: 'select', 'aria-label': 'Sort by' }, ...SORTS.map(([v, t]) => h('option', { value: v, text: t })));
  const fQ = h('input', { class: 'input', type: 'search', placeholder: 'Search by slug', 'aria-label': 'Search by slug' });
  const tbody = h('tbody');
  const more = h('button', { class: 'btn btn-quiet', type: 'button', text: 'Show more', hidden: true });
  const PAGE = 50; let offset = 0, ticket = 0, sortKey = '', sortDir = 'asc';

  async function loadList(reset) {
    const mine = ++ticket;
    if (reset) { offset = 0; tbody.replaceChildren(); }
    const q = new URLSearchParams({ limit: PAGE, offset });
    if (sortKey) { q.set('sort', sortKey); q.set('dir', sortDir); }
    for (const [k, el] of [['status', fStatus], ['source', fSource], ['vendor', fVendor], ['q', fQ]]) if (el.value.trim()) q.set(k, el.value.trim());
    let rows;
    try { rows = await api('/admin/boards?' + q); } catch (ex) { toast(ex.message, true); return; }
    if (mine !== ticket) return;
    for (const b of rows) tbody.append(boardRow(b));
    offset += rows.length;
    more.hidden = rows.length < PAGE;
    if (reset) refreshCounters();
    if (!tbody.children.length) tbody.append(h('tr', {}, h('td', { colspan: '6', class: 'empty', text: 'No boards match.' })));
  }
  function boardRow(b) {
    const [stLabel, stCls] = BOARD_STATUS[b.status] || [b.status, 'pill-mute'];
    const [srcLabel, srcCls] = SOURCE[b.source] || [b.source, 'pill-mute'];
    const link = safeUrl(b.public_url);
    const toggle = h('button', { class: 'btn btn-quiet btn-sm', type: 'button', text: b.status === 'enabled' ? 'Disable' : 'Enable' });
    toggle.addEventListener('click', async () => {
      try { await api(`/admin/boards/${b.id}`, { method: 'PATCH', body: { status: b.status === 'enabled' ? 'disabled' : 'enabled' } }); loadList(true); }
      catch (ex) { toast(ex.message, true); }
    });
    const agencyBtn = h('button', { class: 'btn btn-quiet btn-sm', type: 'button', text: b.is_agency ? 'Not an agency' : 'Mark as agency' });
    agencyBtn.addEventListener('click', async () => {
      try { await api(`/admin/boards/${b.id}`, { method: 'PATCH', body: { is_agency: !b.is_agency } }); loadList(true); }
      catch (ex) { toast(ex.message, true); }
    });
    const syncBtn = h('button', { class: 'btn btn-quiet btn-sm', type: 'button', text: 'Sync now' });
    syncBtn.addEventListener('click', async () => {
      syncBtn.disabled = true;
      try { const r = await api(`/admin/boards/${b.id}/sync`, { method: 'POST' }); toast(r.ok ? `Synced: ${r.jobs} jobs (${r.new} new)` : `Failed: ${r.error}`, !r.ok); loadList(true); }
      catch (ex) { toast(ex.message, true); syncBtn.disabled = false; }
    });
    return h('tr', {},
      h('td', {}, link ? h('a', { href: link, target: '_blank', rel: 'noopener noreferrer', text: b.board }) : b.board,
        h('div', { class: 'muted', text: (byVendor[b.vendor]?.label || b.vendor) + (b.company_name && b.company_name.toLowerCase() !== b.board.toLowerCase() ? ' · ' + b.company_name : '') })),
      h('td', {}, h('span', { class: 'pill ' + srcCls, text: srcLabel })),
      h('td', {}, h('span', { class: 'pill ' + stCls, text: stLabel }), b.is_agency ? h('span', { class: 'pill pill-stamp', text: 'Agency' }) : null, b.last_error ? h('div', { class: 'muted', text: b.last_error }) : null),
      h('td', { text: b.status === 'enabled' || b.job_count ? String(b.job_count) : '' }),
      h('td', { class: 'muted' }, b.last_sync_at
        ? [h('time', { datetime: b.last_sync_at + 'Z', text: fmtDateTime(b.last_sync_at) }), h('div', { text: ago(b.last_sync_at) + (b.last_error ? ', failed' : '') })]
        : (b.status === 'enabled' ? 'Waiting' : '')),
      h('td', {}, h('div', { class: 'row-actions' }, b.status === 'enabled' ? syncBtn : null, agencyBtn, toggle)));
  }
  // sortable headers: click to sort, click again to reverse. The server sorts (the list is paged).
  const COLUMNS = [['Board', 'board'], ['Added', null], ['Status', 'status'], ['Jobs', 'jobs'], ['Last synced', 'synced'], ['', null]];
  const thead = h('thead');
  function drawHead() {
    thead.replaceChildren(h('tr', {}, ...COLUMNS.map(([label, key]) => {
      if (!key) return h('th', { text: label });
      const active = sortKey === key;
      const btn = h('button', { class: 'th-sort', type: 'button', text: label + (active ? (sortDir === 'asc' ? ' ▲' : ' ▼') : '') });
      btn.addEventListener('click', () => {
        // numbers and dates are most useful biggest/newest first; names A to Z
        sortDir = active ? (sortDir === 'asc' ? 'desc' : 'asc') : (key === 'jobs' || key === 'synced' ? 'desc' : 'asc');
        sortKey = key; fSort.value = `${sortKey}:${sortDir}`; drawHead(); loadList(true);
      });
      return h('th', { 'aria-sort': active ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none' }, btn);
    })));
  }
  drawHead();
  fSort.addEventListener('change', () => { [sortKey, sortDir] = fSort.value ? fSort.value.split(':') : ['', 'asc']; drawHead(); loadList(true); });

  // ── sync all boards now: a background run with live progress ──
  const syncAllBtn = h('button', { class: 'btn btn-stamp', type: 'button', text: 'Sync all boards' });
  const syncAllBar = h('progress', { class: 'bar', max: '1', value: '0', hidden: true, 'aria-label': 'Sync progress' });
  const syncAllLine = h('p', { class: 'muted ui', role: 'status', 'aria-live': 'polite' });
  const syncAllRow = h('div', { class: 'mt4' }, syncAllBtn, syncAllLine, syncAllBar);
  let syncAllTimer = null;
  function paintSyncAll(st) {
    const running = st.state === 'running';
    syncAllBtn.disabled = running;
    syncAllBtn.textContent = running ? 'Syncing…' : 'Sync all boards';
    syncAllBar.hidden = !running;
    syncAllBar.max = Math.max(st.total, 1); syncAllBar.value = st.done;
    const n = (x) => x.toLocaleString();
    const tail = `${n(st.ok)} ok, ${n(st.failed)} failed, ${n(st.jobs)} jobs (${n(st.new)} new)`;
    syncAllLine.textContent = running
      ? `Syncing ${n(st.done)} of ${n(st.total)} boards: ${tail}. This keeps going if you leave the page.`
      : st.state === 'failed' ? `Stopped after ${n(st.done)} of ${n(st.total)} boards: ${st.error}`
        : `Synced ${n(st.done)} boards ${ago(st.finished_at)}: ${tail}.`;
  }
  async function pollSyncAll() {
    clearTimeout(syncAllTimer);
    try {
      const st = await api('/admin/boards/sync-all');
      if (!st.state) return;
      paintSyncAll(st);
      if (st.state === 'running') { syncAllTimer = setTimeout(pollSyncAll, 2000); loadList(true); return; }
      await loadList(true);
    } catch (ex) { syncAllLine.textContent = ex.message; }
  }
  syncAllBtn.addEventListener('click', async () => {
    syncAllBtn.disabled = true;
    try { paintSyncAll(await api('/admin/boards/sync-all', { method: 'POST' })); pollSyncAll(); }
    catch (ex) { toast(ex.message, true); syncAllBtn.disabled = false; pollSyncAll(); }
  });
  pollSyncAll(); // reopening the page while a sync is still going

  let timer; const refilter = () => { clearTimeout(timer); timer = setTimeout(() => loadList(true), 250); };
  [fStatus, fSource, fVendor].forEach((el) => el.addEventListener('change', () => loadList(true)));
  fQ.addEventListener('input', refilter);
  more.addEventListener('click', () => loadList(false));
  wrap.append(h('section', { class: 'block' }, h('h2', { text: 'All boards' }),
    h('div', { class: 'two' }, h('div', { class: 'field' }, fStatus), h('div', { class: 'field' }, fSource)),
    h('div', { class: 'two' }, h('div', { class: 'field' }, fVendor), h('div', { class: 'field' }, fQ)),
    h('div', { class: 'field' }, fSort),
    syncAllRow,
    h('table', { class: 'table' }, thead, tbody),
    h('p', { class: 'mt4' }, more)));
  await loadList(true);

  return wrap;
}

// counts in the nav
if (user) {
  api('/me/saved/ids').then((ids) => { counts.saved = ids.length; drawNav(location.hash.slice(1) || 'connect'); }).catch(() => {});
  api('/me/applications?limit=200').then((r) => { counts.applications = r.length; drawNav(location.hash.slice(1) || 'connect'); }).catch(() => {});
  route();
}
