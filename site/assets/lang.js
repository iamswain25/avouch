// language switcher: persist choice (cookie for edge middleware, localStorage for the JS fallback)
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-lang]');
  if (!el) return;
  const lang = el.getAttribute('data-lang');
  if (lang !== 'ko' && lang !== 'en') return;
  document.cookie = `lang=${lang};path=/;max-age=31536000;SameSite=Lax`;
  try { localStorage.setItem('avouch_lang', lang); } catch (err) {}
});
