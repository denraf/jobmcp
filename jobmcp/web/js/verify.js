import { $, api, chrome, h } from '/static/js/common.js';

await chrome('');
const token = new URLSearchParams(location.search).get('token');
history.replaceState(null, '', location.pathname);  // keep the one-time token out of the address bar and history

// Someone who came here from an agent's approval page goes back to it after signing in.
const resume = (() => {
  try { const n = localStorage.getItem('lj_resume') || ''; return n.startsWith('/') && !n.startsWith('//') && n !== '/portal' ? n : ''; } catch { return ''; }
})();
const signInHref = resume ? '/login?next=' + encodeURIComponent(resume) : '/login';
const title = $('#title'), msg = $('#msg'), actions = $('#actions');
if (!token) {
  title.textContent = 'This link is incomplete';
  msg.textContent = 'Open the link from your confirmation email, or ask for a new one.';
  actions.append(h('a', { class: 'btn btn-stamp', href: '/login', text: 'Go to sign in' }));
  actions.hidden = false;
} else {
  try {
    await api('/auth/verify', { method: 'POST', body: { token }, auth: false });
    title.textContent = 'Email confirmed';
    msg.textContent = 'Your account is ready. Sign in to connect your agent.';
    actions.append(h('a', { class: 'btn btn-stamp', href: signInHref, text: 'Sign in' }));
  } catch (ex) {
    title.textContent = 'This link doesn’t work';
    msg.textContent = ex.status === 429 ? ex.message : 'It may have expired or already been used. Sign in with your password and we’ll send a new one.';
    actions.append(h('a', { class: 'btn btn-stamp', href: '/login', text: 'Go to sign in' }));
  }
  actions.hidden = false;
}
