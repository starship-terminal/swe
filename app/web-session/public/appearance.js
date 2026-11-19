/* Appearance for the session view page, the web app's own way: Settings >
 * General keeps "light" or "dark" in localStorage under "swe.appearance"
 * (System removes the key), and data-theme on <html> forces that palette in
 * tokens.css. A classic script in <head>, so the first paint already has the
 * right palette, and it follows a change made in another tab. */
(function () {
  var KEY = "swe.appearance";
  var root = document.documentElement;
  function apply() {
    var mode = null;
    try { mode = window.localStorage.getItem(KEY); } catch (e) { /* storage off: follow the system */ }
    if (mode === "light" || mode === "dark") root.dataset.theme = mode;
    else delete root.dataset.theme;
  }
  apply();
  window.addEventListener("storage", function (event) {
    if (event.key === KEY || event.key === null) apply();
  });
})();
