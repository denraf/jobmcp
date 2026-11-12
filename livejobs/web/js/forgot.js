import { $, api, chrome } from '/static/js/common.js';

if (await chrome('')) location.replace('/portal');
$('#form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('#error'), btn = e.target.querySelector('button[type=submit]');
  err.textContent = '';
  const email = $('#email').value.trim();
  if (!email) { err.textContent = 'Enter your email address.'; return; }
  btn.disabled = true;
  try {
    await api('/auth/forgot', { method: 'POST', body: { email }, auth: false });
    $('#form').hidden = true;
    $('#done').hidden = false;
  } catch (ex) { err.textContent = ex.message; btn.disabled = false; }
});
