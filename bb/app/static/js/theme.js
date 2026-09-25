/* Applies the theme someone picked before the stylesheet paints anything.
 *
 * A classic (non-module) script loaded from <head> on purpose: module scripts
 * run after the first paint, which would flash the computer's theme and then
 * switch. No stored choice means "follow the computer" — nothing is set.
 * Stored per browser only (localStorage); it is a viewing preference, not an
 * account setting. See the theme toggle in app.js. */
(function () {
  try {
    var theme = localStorage.getItem('bb.theme');
    if (theme === 'light' || theme === 'dark') {
      document.documentElement.setAttribute('data-theme', theme);
    }
  } catch (e) { /* storage blocked: follow the computer */ }
})();
