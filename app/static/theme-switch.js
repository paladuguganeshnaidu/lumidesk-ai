/* LumiDesk appearance control. No account data is stored or sent to the server. */
(() => {
  'use strict';
  const KEY = 'lumidesk-appearance';
  let current = 'dark';
  try {
    const saved = window.localStorage.getItem(KEY);
    if (saved === 'dark' || saved === 'light') current = saved;
  } catch (_) {}
  document.documentElement.dataset.theme = current;
  const paint = () => {
    document.documentElement.dataset.theme = current;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = current === 'light' ? '#F7F5F0' : '#0A0C0E';
    document.querySelectorAll('[data-theme-choice]').forEach(button => {
      const selected = button.dataset.themeChoice === current;
      button.setAttribute('aria-pressed', String(selected));
      button.classList.toggle('is-active', selected);
    });
  };
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-theme-choice]');
    if (!button) return;
    const next = button.dataset.themeChoice;
    if (next !== 'dark' && next !== 'light') return;
    current = next;
    try { window.localStorage.setItem(KEY, current); } catch (_) {}
    paint();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', paint);
  else paint();
})();
