// Loaded as a plain blocking script in <head> of every page, before the
// stylesheet paints, so a saved choice is applied with no flash. Light is the
// default: with nothing saved (or anything other than 'dark' saved) the page
// is light, whatever the operating system prefers. The choice is kept in this
// browser only (localStorage). common.js adds the switch to the page header.
(() => {
  const KEY = 'jobmcp-theme';
  const root = document.documentElement;
  let saved = null;
  try { saved = localStorage.getItem(KEY); } catch { /* storage blocked: stay on the default */ }
  if (saved === 'dark') root.dataset.theme = 'dark';
  globalThis.jobmcpTheme = {
    // 'light' or 'dark': what is actually showing
    current() { return root.dataset.theme === 'dark' ? 'dark' : 'light'; },
    set(theme) {
      if (theme === 'dark') root.dataset.theme = 'dark'; else delete root.dataset.theme;
      try { localStorage.setItem(KEY, theme === 'dark' ? 'dark' : 'light'); } catch { /* not saved, still applied for this visit */ }
      dispatchEvent(new CustomEvent('jobmcp-theme'));
    },
  };
})();
