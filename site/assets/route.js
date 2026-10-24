// Locale router for hosts without edge middleware (nginx has its own map;
// Cloudflare Pages uses functions/_middleware.js). Reads the saved choice first,
// then the browser languages, and defaults to English.
(function () {
  var saved = null;
  try { saved = localStorage.getItem('avouch_lang'); } catch (e) {}
  var m = document.cookie.match(/(?:^|;\s*)lang=(ko|en)\b/);
  if (m) saved = m[1];
  var langs = (navigator.languages || [navigator.language || 'en']).join(',').toLowerCase();
  var lang = (saved === 'ko' || saved === 'en') ? saved : (langs.indexOf('ko') === 0 ? 'ko' : 'en');
  location.replace('/' + lang + '/');
})();
