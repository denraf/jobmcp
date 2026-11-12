import { $, api, chrome, session } from '/static/js/common.js';

const mode = $('main').dataset.mode;
if (await chrome(mode)) location.replace('/portal');

// Only follow same-site paths from ?next=, never an arbitrary URL.
const next = (() => {
  const n = new URLSearchParams(location.search).get('next') || '';
  return n.startsWith('/') && !n.startsWith('//') ? n : '/portal';
})();

if (mode === 'login' && new URLSearchParams(location.search).get('reset')) {
  $('#notice').textContent = 'Password changed. Sign in with your new password.';
}

if (mode === 'register') {
  try {
    const cfg = await api('/public/config', { auth: false });
    if (!cfg.registration_open) { $('#form').hidden = true; $('#closed').hidden = false; }
  } catch { /* let the submit report errors */ }
}

let lastEmail = '';
async function resend(email) {
  const note = $('#resent');
  note.textContent = '';
  try {
    await api('/auth/resend-verification', { method: 'POST', body: { email }, auth: false });
    note.textContent = 'Sent. Check your inbox.';
  } catch (ex) { note.textContent = ex.message; }
}
$('#resend')?.addEventListener('click', () => resend(lastEmail || $('#email')?.value.trim()));

$('#form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('#error');
  const btn = e.target.querySelector('button[type=submit]');
  err.textContent = '';
  if ($('#unverified')) $('#unverified').hidden = true;
  const email = $('#email').value.trim();
  const password = $('#password').value;
  if (!email || !password) { err.textContent = 'Enter your email and password.'; return; }
  if (mode === 'register' && password.length < 10) { err.textContent = 'Use a password with at least 10 characters.'; return; }
  btn.disabled = true;
  try {
    const r = await api(mode === 'register' ? '/auth/register' : '/auth/login', { method: 'POST', body: { email, password }, auth: false });
    if (r.token) {  // signed in (login, or a server with email verification switched off)
      session.set(r.token);
      location.href = mode === 'register' ? '/portal#connect' : next;
      return;
    }
    lastEmail = email;  // registration: the account exists only once the emailed link is opened
    $('#sent-to').textContent = email;
    $('#form').hidden = true;
    $('#page-title').hidden = true;
    document.querySelector('.auth-aside').hidden = true;
    $('#sent').hidden = false;
  } catch (ex) {
    lastEmail = email;
    if (ex.status === 403 && mode === 'login') { err.textContent = ex.message; $('#unverified').hidden = false; }
    else err.textContent = ex.status === 401 ? 'That email and password don’t match. Check them and try again.' : ex.message;
    btn.disabled = false;
  }
});
