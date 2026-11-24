import { $, api, chrome, h, session } from '/static/js/common.js';

const q = new URLSearchParams(location.search);

// Arrived from the emailed link: trade its one-time token for a session, then carry on as signed in.
let linkError = '';
const magic = q.get('magic');
if (magic) {
  q.delete('magic');
  history.replaceState(null, '', location.pathname + (q.toString() ? '?' + q : ''));  // keep the token out of the address bar and history
  try {
    const r = await api('/auth/magic', { method: 'POST', body: { token: magic }, auth: false });
    session.set(r.token);
  } catch (ex) {
    linkError = ex.status === 429 ? ex.message : 'That link has expired or was already used. Ask for a new one below.';
  }
}
const user = await chrome('');
const KEYS = ['client_id', 'redirect_uri', 'response_type', 'code_challenge', 'code_challenge_method', 'state', 'resource'];
const params = Object.fromEntries(KEYS.filter((k) => q.has(k)).map((k) => [k, q.get(k)]));

const title = $('#title'), msg = $('#msg'), actions = $('#actions'), can = $('#can'), err = $('#error');
const here = location.pathname + location.search;

function stop(heading, text) {
  title.textContent = heading;
  msg.textContent = text;
  can.hidden = actions.hidden = true;
}

let who;
try {
  const { client_id, redirect_uri, response_type, code_challenge, code_challenge_method, resource } = params;
  const qs = new URLSearchParams({ client_id, redirect_uri, response_type, code_challenge, code_challenge_method });
  if (resource) qs.set('resource', resource);
  who = await api('/oauth/request?' + qs.toString(), { auth: false });
} catch (ex) {
  stop('This request can’t be approved', `${ex.message}. Go back to your agent and start the connection again.`);
  throw ex;
}

if (!user) {
  title.textContent = `Sign in to connect ${who.name}`;
  msg.textContent = 'Your agent asked to use your jobmcp account. Enter your email and we send you a link. It signs you in, or creates a free account if you have none, and brings you back here to approve.';
  err.textContent = linkError;
  const email = h('input', { id: 'email', type: 'email', autocomplete: 'email', required: true, value: q.get('login_hint') || '' });
  const send = h('button', { class: 'btn btn-stamp', type: 'submit', text: 'Email me a link' });
  const form = h('form', { class: 'field' },
    h('label', { for: 'email', text: 'Email' }), email,
    h('div', { class: 'cta-row' }, send));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.textContent = '';
    send.disabled = true;
    try {
      await api('/oauth/email-link', { method: 'POST', body: { ...params, email: email.value.trim() }, auth: false });
      title.textContent = 'Check your email';
      msg.textContent = `If ${email.value.trim()} can be used, a link is on its way. Open it on this device to approve ${who.name}. It works once.`;
      form.hidden = true;
    } catch (ex) {
      err.textContent = ex.message;
      send.disabled = false;
    }
  });
  can.replaceChildren(h('li', {}, 'Prefer a password? ',
    h('a', { href: '/login?next=' + encodeURIComponent(here), text: 'Sign in' }), ' or ',
    h('a', { href: '/register?next=' + encodeURIComponent(here), text: 'create an account' }), '.'));
  can.hidden = false;
  actions.replaceChildren(form);
  actions.hidden = false;
} else {
  title.textContent = `Connect ${who.name}?`;
  msg.textContent = `${who.name} will use your jobmcp account (${user.email}). After you approve, it sends you back to ${who.sends_you_to}. Only approve an app you just started yourself.`;
  can.replaceChildren(
    h('li', { text: 'It can search jobs, read your preferences, saved jobs and application record, and record what it applied to.' }),
    h('li', { text: 'It never receives a password or résumé from jobmcp, and jobmcp holds none.' }),
    h('li', { text: 'You can disconnect it any time in your portal, under Connect your agent.' }));
  can.hidden = false;
  const decide = async (approve) => {
    err.textContent = '';
    actions.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    try {
      const r = await api('/oauth/decision', { method: 'POST', body: { ...params, approve } });
      location.href = r.redirect_to;
    } catch (ex) {
      err.textContent = ex.message;
      actions.querySelectorAll('button').forEach((b) => { b.disabled = false; });
    }
  };
  actions.replaceChildren(
    h('button', { class: 'btn btn-stamp', type: 'button', text: 'Approve', onclick: () => decide(true) }),
    h('button', { class: 'btn btn-quiet', type: 'button', text: 'Cancel', onclick: () => decide(false) }));
  actions.hidden = false;
}
