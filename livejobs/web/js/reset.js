import { $, api, chrome, session } from '/static/js/common.js';

await chrome('');
const token = new URLSearchParams(location.search).get('token');
history.replaceState(null, '', location.pathname);  // keep the one-time token out of the address bar and history
if (!token) { $('#form').hidden = true; $('#bad').hidden = false; }

$('#form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('#error'), btn = e.target.querySelector('button[type=submit]');
  err.textContent = '';
  const password = $('#password').value;
  if (password.length < 10) { err.textContent = 'Use a password with at least 10 characters.'; return; }
  btn.disabled = true;
  try {
    await api('/auth/reset', { method: 'POST', body: { token, password }, auth: false });
    session.clear();
    location.href = '/login?reset=1';
  } catch (ex) {
    if (ex.status === 400 && /link/.test(ex.message)) { $('#form').hidden = true; $('#bad').hidden = false; }
    else { err.textContent = ex.message; btn.disabled = false; }
  }
});
