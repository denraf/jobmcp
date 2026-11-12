import { $, ago, api, applyInstructions, chrome, copyButton, h, safeUrl, session, toast, VENDOR_LABEL } from '/static/js/common.js';
import { renderDescription } from '/static/js/richtext.js';

const user = await chrome('jobs');
const PAGE = 25;
const state = { offset: 0, total: 0, openId: null, saved: new Set() };
const form = $('#filters');
const list = $('#list');

// ── filters <-> URL ──
const FIELDS = ['title', 'keywords', 'location', 'vendor', 'age', 'seniority', 'country'];
function readUrl() {
  const q = new URLSearchParams(location.search);
  for (const f of FIELDS) if (q.has(f)) form.elements[f].value = q.get(f);
  form.elements.remote.checked = q.get('remote') === '1';
  form.elements.noagency.checked = q.get('noagency') === '1';
  state.openId = q.get('job') ? Number(q.get('job')) : null;
}
function writeUrl() {
  const q = new URLSearchParams();
  for (const f of FIELDS) if (form.elements[f].value.trim()) q.set(f, form.elements[f].value.trim());
  if (form.elements.remote.checked) q.set('remote', '1');
  if (form.elements.noagency.checked) q.set('noagency', '1');
  if (state.openId) q.set('job', state.openId);
  history.replaceState(null, '', q.toString() ? '?' + q : location.pathname);
}

const COUNTRIES = { US: 'United States', GB: 'United Kingdom', CA: 'Canada', DE: 'Germany', FR: 'France', NL: 'Netherlands', ES: 'Spain', IT: 'Italy', IE: 'Ireland', PT: 'Portugal', SE: 'Sweden', DK: 'Denmark', NO: 'Norway', FI: 'Finland', PL: 'Poland', CH: 'Switzerland', AT: 'Austria', BE: 'Belgium', IN: 'India', SG: 'Singapore', JP: 'Japan', AU: 'Australia', NZ: 'New Zealand', BR: 'Brazil', MX: 'Mexico', IL: 'Israel', AE: 'United Arab Emirates', ZA: 'South Africa' };
for (const [code, name] of Object.entries(COUNTRIES)) $('#f-country').append(h('option', { value: code, text: name }));
const LEVEL = { intern: 'Intern', entry: 'Entry level', senior: 'Senior', lead: 'Lead or manager', executive: 'Director+' };
const cfg = await api('/public/config', { auth: false }).catch(() => ({ vendors: [] }));
for (const v of cfg.vendors) $('#f-vendor').append(h('option', { value: v, text: VENDOR_LABEL[v] || v }));
readUrl();

if (user) {
  try { state.saved = new Set(await api('/me/saved/ids')); } catch { /* signed-out view still works */ }
}

// ── results ──
function params(offset) {
  const q = new URLSearchParams({ limit: PAGE, offset });
  const t = form.elements.title.value.trim(), k = form.elements.keywords.value.trim();
  if (t) q.set('title', t);
  if (k) q.set('keywords', k);
  if (form.elements.location.value.trim()) q.set('location', form.elements.location.value.trim());
  if (form.elements.vendor.value) q.set('vendor', form.elements.vendor.value);
  if (form.elements.age.value) q.set('max_age_days', form.elements.age.value);
  if (form.elements.remote.checked) q.set('remote_only', 'true');
  if (form.elements.seniority.value) q.set('seniority', form.elements.seniority.value);
  if (form.elements.country.value) q.set('country', form.elements.country.value);
  if (form.elements.noagency.checked) q.set('exclude_agencies', 'true');
  return q;
}

// 'Remote' / 'Hybrid' as a chip, unless the location already says it
function workplaceChip(j) {
  if (!j.workplace || (j.location || '').toLowerCase().includes(j.workplace)) return null;
  return h('span', { text: j.workplace === 'remote' ? 'Remote' : 'Hybrid' });
}

function saveButton(id) {
  const b = h('button', { class: 'save-btn', type: 'button', 'aria-pressed': String(state.saved.has(id)) });
  const paint = () => { const on = state.saved.has(id); b.setAttribute('aria-pressed', String(on)); b.textContent = on ? 'Saved' : 'Save'; };
  paint();
  b.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!session.get()) { location.href = '/login?next=' + encodeURIComponent('/jobs?job=' + id); return; }
    const on = state.saved.has(id);
    try {
      await api(`/me/saved/${id}`, { method: on ? 'DELETE' : 'PUT' });
      on ? state.saved.delete(id) : state.saved.add(id);
      document.querySelectorAll(`[data-save="${id}"]`).forEach((x) => x.replaceWith(saveButton(id)));
    } catch (ex) { toast(ex.message, true); }
  });
  b.dataset.save = id;
  return b;
}

function row(j) {
  const open = () => openDetail(j.id);
  const li = h('li', { class: 'job', 'data-id': j.id, 'aria-current': j.id === state.openId ? 'true' : false, onclick: open },
    h('div', {}, h('button', { class: 'job-title', type: 'button', text: j.title, onclick: (e) => { e.stopPropagation(); open(); } })),
    h('div', { class: 'job-side' }, h('span', { text: j.posted_at ? ago(j.posted_at) : 'Added ' + ago(j.listed_at) }), saveButton(j.id)),
    h('div', { class: 'job-meta' }, h('span', { text: j.company || 'Company' }), j.location ? h('span', { text: j.location }) : null,
      workplaceChip(j),
      LEVEL[j.seniority] ? h('span', { text: LEVEL[j.seniority] }) : null,
      j.agency ? h('span', { text: 'Staffing agency' }) : null,
      h('span', { text: VENDOR_LABEL[j.vendor] || j.vendor })),
    j.snippet ? h('p', { class: 'snippet', text: j.snippet }) : null);
  return li;
}

let ticket = 0;
async function load(reset) {
  const mine = ++ticket;
  if (reset) { state.offset = 0; list.replaceChildren(); }
  try {
    const r = await api('/public/jobs?' + params(state.offset), { auth: false });
    if (mine !== ticket) return; // a newer search started
    state.total = r.total;
    for (const j of r.hits) list.append(row(j));
    state.offset = r.next_offset ?? state.offset;
    $('#more').hidden = r.next_offset == null;
    $('#count').textContent = r.total === 0 ? '' : `${r.total.toLocaleString()} ${r.total === 1 ? 'job' : 'jobs'}`;
    if (r.total === 0) {
      list.replaceChildren(h('li', { class: 'empty' }, h('b', { text: 'No jobs match these filters.' }),
        'Try fewer words, or clear the location and platform filters.'));
    }
  } catch (ex) {
    if (mine !== ticket) return;
    list.replaceChildren(h('li', { class: 'empty' }, h('b', { text: 'Couldn’t load jobs.' }), ex.message));
  }
}

let timer;
form.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { writeUrl(); load(true); }, 300); });
form.addEventListener('submit', (e) => { e.preventDefault(); clearTimeout(timer); writeUrl(); load(true); });
$('#more').addEventListener('click', () => load(false));

// ── detail ──
const layout = $('#layout');
const detail = $('#detail');
async function openDetail(id) {
  state.openId = id;
  writeUrl();
  document.querySelectorAll('.job').forEach((li) => li.setAttribute('aria-current', String(Number(li.dataset.id) === id)));
  layout.classList.remove('no-detail');
  detail.hidden = false;
  detail.replaceChildren(h('div', { class: 'detail-inner' }, h('p', { class: 'muted ui', text: 'Loading' })));
  try {
    const j = await api(`/public/jobs/${id}`, { auth: false });
    if (state.openId !== id) return;
    const url = safeUrl(j.url);
    detail.replaceChildren(h('div', { class: 'detail-inner' },
      h('button', { class: 'btn btn-quiet btn-sm detail-close', type: 'button', text: 'Back to jobs', onclick: closeDetail }),
      h('h2', { text: j.title }),
      h('div', { class: 'job-meta ui' }, h('span', { text: j.company }), j.location ? h('span', { text: j.location }) : null,
        workplaceChip(j),
        LEVEL[j.seniority] ? h('span', { text: LEVEL[j.seniority] }) : null,
        j.agency ? h('span', { text: 'Staffing agency' }) : null,
        h('span', { text: VENDOR_LABEL[j.vendor] || j.vendor }), j.posted_at ? h('span', { text: 'Posted ' + ago(j.posted_at) }) : h('span', { text: 'Added ' + ago(j.listed_at) })),
      h('div', { class: 'detail-actions' },
        url ? h('a', { class: 'btn btn-stamp', href: url, target: '_blank', rel: 'noopener noreferrer', text: 'Open the company’s page' }) : null,
        saveButton(j.id),
        copyButton(() => applyInstructions(j.id), 'Copy agent instructions'),
        h('button', { class: 'btn btn-quiet btn-sm detail-x', type: 'button', text: 'Close', onclick: closeDetail })),
      h('div', { class: 'detail-body' }, ...(j.description ? renderDescription(j.description, h) : ['The company hasn’t shared a description here. Open the company’s page for details.'])),
      h('p', { class: 'detail-note', text: user
        ? 'To have your agent apply, connect it from your portal. jobmcp never applies for you.'
        : 'Want your own agent to apply for you? Create an account and connect it. jobmcp never applies for you.' })));
    detail.scrollTop = 0;
  } catch (ex) {
    detail.replaceChildren(h('div', { class: 'detail-inner' },
      h('button', { class: 'btn btn-quiet btn-sm', type: 'button', text: 'Close', onclick: closeDetail }),
      h('p', { class: 'empty', text: ex.status === 404 ? 'This job is no longer open.' : ex.message })));
  }
}
function closeDetail() {
  state.openId = null; writeUrl();
  detail.hidden = true; layout.classList.add('no-detail');
  document.querySelectorAll('.job').forEach((li) => li.setAttribute('aria-current', 'false'));
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !detail.hidden) closeDetail(); });

await load(true);
if (state.openId) openDetail(state.openId);
