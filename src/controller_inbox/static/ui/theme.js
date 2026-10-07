/* Runs before the page paints, so a saved theme or a collapsed side bar never flashes the other way. */
(function () {
  "use strict";
  var root = document.documentElement;
  try {
    var theme = localStorage.getItem("closedesk-theme");
    if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
    if (localStorage.getItem("closedesk-rail") === "collapsed") root.setAttribute("data-rail", "collapsed");
    if (localStorage.getItem("closedesk-ws-chat") === "open") root.setAttribute("data-chat", "open");
  } catch (error) {
    /* private mode: the system theme is used */
  }
})();
