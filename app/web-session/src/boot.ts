// The standalone /session/ page: mount the view on #root for ?key=.
import "./page.css";
import "./main";

import { keyFromSearch } from "./session-key";

const el = document.getElementById("root");
if (el && window.SWESessionView) {
  window.SWESessionView.mount(el, { key: keyFromSearch(window.location.search) });
}
