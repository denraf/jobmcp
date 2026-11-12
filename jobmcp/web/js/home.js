import { $, api, chrome } from '/static/js/common.js';

const user = await chrome('home');
if (user) {
  const cta = $('#cta-register');
  cta.href = '/portal';
  cta.textContent = 'Open your portal';
}

try {
  const s = await api('/public/stats', { auth: false });
  if (s.open_jobs > 0) {
    const n = (v) => v.toLocaleString();
    const el = $('#numbers');
    el.append(
      Object.assign(document.createElement('b'), { textContent: n(s.open_jobs) }), ' open jobs from ',
      Object.assign(document.createElement('b'), { textContent: n(s.companies) }), ' companies on ',
      Object.assign(document.createElement('b'), { textContent: String(s.platforms) }), ' hiring platforms, refreshed through the day.');
    $('#numbers-section').hidden = false;
  }
} catch { /* stats are decoration; the page works without them */ }
