/* SWE web app: the Mac app's pages, served by the hub at its root.
 * No build step, no deps. Data comes from /v1/app/*. The hub owner's own
 * tailnet devices need no pairing (the hub knows them from Tailscale);
 * any other browser uses the device credential its #pair= link minted,
 * kept in localStorage.
 */
"use strict";

/* Every key this web app keeps in localStorage was named quiver.* before
 * the rename. Each moves to its swe.* name on the first load that finds
 * it: the old value is copied unless the new key already holds one (a
 * newer page wrote it), then the old key goes. It runs before any page
 * script reads storage, so no reader needs to know the old names.
 *
 * The credential is the exception: quiver.remote is copied but kept, so
 * a tab still running a page from before the rename (which shares this
 * storage) keeps signing in until it reloads, and credential() falls back
 * to it if the copy could not be written (storage full). Banking or
 * forgetting a credential drops the old key. */
const LEGACY_CREDENTIAL_KEY = "quiver.remote";
function migrateStorageKeys(store) {
  const old = [];
  for (let i = 0; i < (store.length || 0); i++) {
    const key = store.key(i);
    if (key && key.startsWith("quiver.")) old.push(key);
  }
  for (const key of old) {
    const next = "swe." + key.slice("quiver.".length);
    if (store.getItem(next) == null) store.setItem(next, store.getItem(key));
    if (key !== LEGACY_CREDENTIAL_KEY) store.removeItem(key);
  }
}
try { migrateStorageKeys(localStorage); } catch { /* storage off or full: the old keys wait for the next load */ }

const credential = () => {
  try {
    const saved = localStorage.getItem("swe.remote") ?? localStorage.getItem(LEGACY_CREDENTIAL_KEY);
    return (JSON.parse(saved || "null") || {}).credential || "";
  }
  catch { return ""; }
};

/* X-Swe-Pair marks the request as this page's own: a page on another
 * site can't send a custom header without the hub's CORS consent, so the
 * hub only honours the owner's tailnet identity with it. */
function authHeaders(extra = {}) {
  const cred = credential();
  return { "X-Swe-Pair": "1", ...(cred ? { Authorization: `Bearer ${cred}` } : {}), ...extra };
}

const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

/* The two theme-color metas follow the system scheme by media query. A
 * Light or Dark chosen in Settings sets data-theme on <html> and beats the
 * system, so the browser chrome takes the page's own --wash then; System
 * puts each meta's own colour back. */
function syncThemeColor() {
  const root = document.documentElement;
  const forced = root.dataset.theme === "light" || root.dataset.theme === "dark";
  const wash = forced ? getComputedStyle(root).getPropertyValue("--wash").trim() : "";
  document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => {
    if (!meta.dataset.system) meta.dataset.system = meta.content;
    meta.content = wash || meta.dataset.system;
  });
}

/* An href for a URL the hub passed on from somewhere else (GitHub, a
 * tracker, a status feed, a plugin's manifest), escaped for an attribute.
 * Only http(s) and mailto pass: a `javascript:` or `data:` URL in that
 * data would run in the app's origin on a click, so it comes back "" and
 * the caller shows no link. */
const safeHref = (url) => {
  const s = String(url == null ? "" : url).trim();
  return /^(https?:\/\/|mailto:)/i.test(s) ? esc(s) : "";
};

/* What a failed read or write says: the hub's own message, else its
 * status, else that it didn't answer. ``next`` ends the offline sentence
 * ("then refresh"); null leaves just "Couldn't reach the hub." */
function failText(status, data, next = "then try again") {
  return (data && data.error && data.error.message)
    || (status ? `The hub answered ${status}.`
      : next ? `Couldn't reach the hub. Check the connection, ${next}.` : "Couldn't reach the hub.");
}

/* A write: ``body`` as JSON with api()'s headers. Never throws; status 0
 * means the hub didn't answer at all. Every page's POST and DELETE goes
 * through it (apiPost, GhUI.post). Not plain send(): chat.js has its own. */
async function apiSend(method, path, body) {
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    });
  } catch {
    return { status: 0, data: null };
  }
  let data = null;
  try { data = await resp.json(); } catch { /* empty body */ }
  return { status: resp.status, data };
}
const apiPost = (path, body) => apiSend("POST", path, body);

/* status 0 means the hub didn't answer at all (offline, connection lost,
 * or the request was aborted through `opts.signal`). */
async function api(path, opts = {}) {
  let resp;
  try {
    resp = await fetch(path, { headers: authHeaders(), signal: opts.signal });
  } catch {
    return { status: 0, data: null };
  }
  let data = null;
  try { data = await resp.json(); } catch { /* empty body */ }
  return { status: resp.status, data };
}

/* Small line icons standing in for the Mac app's SF Symbols. */
const ICON = {
  overview: '<path d="M3 20h18M6 16V9M11 16V5M16 16v-5M21 16V8"/>',
  sessions: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  pricing: '<circle cx="12" cy="12" r="9"/><path d="M15 9.5c-.5-1-1.6-1.5-3-1.5-1.7 0-3 .8-3 2s1.3 1.7 3 2 3 .9 3 2.1-1.3 1.9-3 1.9c-1.4 0-2.6-.6-3-1.6M12 6v2M12 16v2"/>',
  repositories: '<path d="M21 8 12 3 3 8v8l9 5 9-5zM3 8l9 5 9-5M12 13v8"/>',
  tasks: '<path d="m4.2 5.4 1.6 1.6L8.5 4.3M4.2 11.4l1.6 1.6 2.7-2.7M4.2 17.4l1.6 1.6 2.7-2.7M11.5 6h8.5M11.5 12h8.5M11.5 18h8.5"/>',
  inbox: '<path d="M3 13h5l1 3h6l1-3h5M5 5h14l2 8v6H3v-6z"/>',
  accounts: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.5-3.5 3.2-5.5 6.5-5.5s6 2 6.5 5.5M16 4.8a3.5 3.5 0 0 1 0 6.4M18 14.8c2 .7 3.2 2.5 3.5 5.2"/>',
  harnesses: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 10 3 2-3 2M12 15h5"/>',
  compute: '<rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
  plugins: '<path d="M10 4a2 2 0 1 1 4 0v2h4v4h-2a2 2 0 1 0 0 4h2v4h-4v-2a2 2 0 1 0-4 0v2H6v-4H4a2 2 0 1 1 0-4h2V6h4z"/>',
  skills: '<path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z"/>',
  agents: '<path d="M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6"/>',
  mcp: '<path d="M9 3v5M15 3v5M7 8h10v4a5 5 0 0 1-10 0zM12 17v4"/>',
  hooks: '<path d="m13 2-9 12h6l-1 8 9-12h-6z"/>',
  worktrees: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="9" r="2"/><path d="M6 7v10M18 11c0 4-4 5-12 7"/>',
  models: '<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9"/>',
  menubar: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 8h18"/>',
  widgets: '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
  status: '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  rescan: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;

/* The Mac app's sidebar, in its order. Pages not ported yet stay listed,
 * dimmed, so the map of the app is the same in both. */
const SIDEBAR = [
  ["Usage", [["overview", "Overview"], ["sessions", "Sessions"], ["calendar", "Calendar"], ["pricing", "Pricing"]]],
  ["GitHub", [["repositories", "Repositories"], ["tasks", "Tasks"], ["inbox", "Inbox"]]],
  ["Manage", [["accounts", "Accounts"], ["harnesses", "Harnesses"], ["compute", "Compute"]]],
  ["Primitives", [["plugins", "Plugins"], ["skills", "Skills"], ["agents", "AGENTS.md"], ["mcp", "MCP servers"], ["hooks", "Hooks"], ["worktrees", "Worktrees"], ["models", "Models"]]],
  ["System", [["menubar", "Menu bar"], ["widgets", "Widgets"], ["status", "Service status"], ["settings", "Settings"]]],
];

const PAGES = { overview: renderOverview, sessions: renderSessions, harnesses: renderHarnesses, accounts: renderAccounts };
// Parts of the Mac app that only make sense on a Mac. The sidebar lists
// them, unclickable, under the same pill Settings puts on its Mac-only rows.
const MAC_ONLY = new Set(["menubar", "widgets"]);
const MAC_ONLY_BADGE = '<span class="badge mac-only">Mac app only</span>';

/* Page plugins (static/pages/*.js, served as /pages.js) register here:
 * registerPage("skills", render). render(id) paints #panel; a page that
 * awaits a request compares routeGen before and after, so a reply that
 * lands after the user moved on is dropped. */
let routeGen = 0;
function registerPage(id, render) {
  PAGES[id] = render;
}

function renderSidebar(current) {
  const nav = document.getElementById("sidebar");
  const brand = nav.querySelector(".brand").outerHTML;
  nav.innerHTML = brand + SIDEBAR.map(([heading, items]) => `
    <div class="nav-group">
      <div class="nav-heading">${heading}</div>
      ${items.map(([id, label]) => {
        // Every row carries data-page for Settings' hide switches. A
        // Mac-only row is not a link, so neither a click nor Tab reaches it.
        if (MAC_ONLY.has(id)) {
          return `<span class="nav-item soon mac-only" data-page="${id}" aria-disabled="true">${icon(id)}<span>${label}</span>${MAC_ONLY_BADGE}</span>`;
        }
        const live = id in PAGES;
        const cls = ["nav-item", id === current ? "active" : "", live ? "" : "soon"].join(" ");
        const title = live ? "" : ' title="Not in the web app yet"';
        return `<a class="${cls}" data-page="${id}" href="#/${id}"${title}${id === current ? ' aria-current="page"' : ""}>${icon(id)}<span>${label}</span></a>`;
      }).join("")}
    </div>`).join("");
  revealActiveTab(nav);
}

/* On a phone the sidebar is one row that scrolls sideways: centre the
 * current page's tab in it, or a page past the first few has no visible
 * tab saying where you are. A sidebar that fits does nothing. */
function revealActiveTab(nav) {
  const active = nav.querySelector(".nav-item.active");
  if (!active || !(nav.scrollWidth > nav.clientWidth)) return;
  const box = nav.getBoundingClientRect(), tab = active.getBoundingClientRect();
  nav.scrollLeft += tab.left - box.left - (box.width - tab.width) / 2;
}

function pageHeader(title, subtitle, actions = "") {
  return `<div class="page-header"><div class="titles">
      <h1 class="page-title">${esc(title)}</h1>
      <p class="page-subtitle">${esc(subtitle)}</p></div>
    <div class="page-actions">${actions}</div></div>`;
}

/* The gate's line for a credential the hub refused (a 401), and the
 * error pages show when a write answers 401. */
const CREDENTIAL_REFUSED = "The hub doesn't know this browser's credential. It may have been revoked.";

/* A page's sheet, made once on document.body so a repaint of #panel
 * behind it (a reply landing, a poll) can't take it away. It belongs to
 * the view that opened it: route() closes it on every navigation, which
 * fires its close handlers, so a page never finds it open again later. */
function bodySheet(id, className, label = "") {
  let sheet = document.getElementById(id);
  if (sheet) return sheet;
  sheet = document.createElement("dialog");
  sheet.id = id;
  sheet.className = className;
  if (label) sheet.setAttribute("aria-label", label);
  sheet.dataset.routeSheet = "";
  document.body.appendChild(sheet);
  return sheet;
}

function closeRouteSheets() {
  for (const sheet of document.querySelectorAll("dialog[data-route-sheet][open]")) sheet.close();
}

/* Only a device the hub can't place reaches this: the owner's own tailnet
 * devices are let in without pairing. The card is the old remote page's
 * pair view: ask the tailnet for a code the owner approves elsewhere, or
 * paste a token a ``swe remote pair`` or Settings' QR minted. ``message``
 * explains a credential the hub refused; with none stored the card says
 * the device needs pairing, unless ``always`` keeps the message. */
function renderGate(message = CREDENTIAL_REFUSED, always = false) {
  // A page's open sheet would stay modal above the gate.
  closeRouteSheets();
  // The gate is the view now: a page's read or poll still out compares
  // routeGen before it paints, so it drops its reply instead of drawing
  // over the pair form.
  routeGen++;
  const lead = credential() || always ? message
    : "This device isn't signed in to the hub owner's Tailscale account, so it needs pairing.";
  document.getElementById("panel").innerHTML = `<div class="page"><div class="card gate">
    <div class="card-title">Pair this browser</div>
    <p>${esc(lead)}</p>
    <div id="pair-start">
      <input class="field gate-field" id="pair-name" type="text" autocomplete="off"
        placeholder="Name for this device" aria-label="Name for this device">
      <button class="btn medium set-button primary" id="pair-ts">Pair with Tailscale</button>
      <p class="gate-error" id="pair-error"></p>
      <details class="gate-token">
        <summary>Have a pair token?</summary>
        <input class="field gate-field" id="pair-token" type="text" inputmode="text"
          autocomplete="off" placeholder="pair token" spellcheck="false">
        <button class="btn medium set-button" id="pair-go">Pair with token</button>
      </details>
    </div>
    <div id="pair-code-card" hidden>
      <div class="gate-code" id="pair-code"></div>
      <p class="caption" id="pair-node"></p>
      <p class="caption">Approve it on the hub with
        <code>swe remote devices --approve=<span id="pair-code-flat"></span></code>,
        or in Settings &rsaquo; Integrations &rsaquo; Remote on a signed-in browser.</p>
      <p class="caption" id="pair-code-status"></p>
      <button class="btn medium set-button" id="pair-retry" hidden>Try again</button>
    </div>
  </div></div>`;
  const el = (id) => document.getElementById(id);
  el("pair-ts").onclick = pairRequestStart;
  el("pair-retry").onclick = pairRequestStart;
  el("pair-go").onclick = () => {
    const token = el("pair-token").value.trim();
    if (token) pairExchange(token, el("pair-name").value.trim());
  };
}

/* ── pairing entry points (the old remote PWA's, now the app's) ────── */

/* What the pair exchange claims about this browser. The hub merges it
 * over what the User-Agent lets it infer, and the browser knows things
 * the UA won't say: an iPad in desktop mode sends a Macintosh UA but
 * still has a touch screen. */
function pairMeta() {
  const ua = navigator.userAgent || "";
  let kind = "browser";
  if (/iPhone|iPod/.test(ua)) kind = "iphone";
  else if (/iPad/.test(ua)) kind = "ipad";
  else if (/Android/.test(ua)) kind = "android";
  else if (/Macintosh|Mac OS X/.test(ua)) kind = (navigator.maxTouchPoints || 0) > 1 ? "ipad" : "mac";
  else if (/Windows/.test(ua)) kind = "windows";
  else if (/Linux/.test(ua)) kind = "linux";
  let os = "";
  let m = ua.match(/OS (\d+[._\d]*) like Mac OS X/);
  if (m) os = (/iPad/.test(ua) ? "iPadOS " : "iOS ") + m[1].replace(/_/g, ".");
  else if ((m = ua.match(/Android (\d+[\d.]*)/))) os = "Android " + m[1];
  else if ((m = ua.match(/Mac OS X (\d+[._\d]+)/))) os = "macOS " + m[1].replace(/_/g, ".");
  else if ((m = ua.match(/Windows NT ([\d.]+)/))) os = "Windows NT " + m[1];
  // Desktop-mode iPadOS: the UA is all lies; the touchscreen isn't.
  if (kind === "ipad" && !/iPad/.test(ua)) os = "iPadOS";
  return { kind, client: "SWE Web", os };
}

/* One pair flow at a time: a new request or navigation bumps the
 * generation and late replies drop instead of painting over the page
 * the user moved to. A good credential still lands — pairing is the
 * point — it just doesn't drag the route back. */
let pairGen = 0;

function pairStillLive(gen, rgen) {
  return gen === pairGen && rgen === routeGen;
}

/* Bank a minted credential where every page looks for it. */
function pairStore(data) {
  try { localStorage.setItem("swe.remote", JSON.stringify(data)); localStorage.removeItem(LEGACY_CREDENTIAL_KEY); } catch { }
}

/* And drop it once the hub has revoked it, so this browser stops
 * presenting a dead credential. */
function pairForget() {
  try { localStorage.removeItem("swe.remote"); localStorage.removeItem(LEGACY_CREDENTIAL_KEY); } catch { }
  /* The session view keeps the message being typed per session under ``swe.sv.draft.v1:``; they go with the credential. */
  try {
    const stale = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith("swe.sv.draft.v1:")) stale.push(key);
    }
    for (const key of stale) localStorage.removeItem(key);
  } catch { }
}

/* ``#pair=<token>[&name=]``: the address `swe remote pair` and Settings'
 * QR mint. The token buys one device credential, kept where every page
 * looks for it, then leaves the URL so a refresh can't spend it again.
 * The POST is unauthenticated — the token is the credential. */
async function pairExchange(token, name) {
  const gen = ++pairGen, rgen = routeGen;
  renderSidebar("");
  document.getElementById("panel").innerHTML = `<div class="page"><div class="card gate">
    <div class="card-title">Pair this browser</div><p>Pairing…</p></div></div>`;
  let status = 0, data = null;
  try {
    const resp = await fetch("/v1/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, name: name || "", device: pairMeta() }),
    });
    status = resp.status;
    try { data = await resp.json(); } catch { /* empty body */ }
  } catch { /* offline or connection lost */ }
  if (status === 200 && data && data.credential) {
    pairStore(data);
    if (!pairStillLive(gen, rgen)) return;
    history.replaceState(null, "", "#/overview");
    route();
    return;
  }
  if (!pairStillLive(gen, rgen)) return;
  const msg = (data && data.error && data.error.message)
    || (status ? `the hub answered ${status}` : "the hub couldn't be reached");
  renderGate(`The pair link didn't work: ${msg} Mint a fresh one — it lives only minutes.`, true);
}

/* ── pair by tailnet identity (the asking side of /v1/pair/request) ── */

/* "Pair with Tailscale": the hub reads this device's tailnet identity
 * itself, so the browser just asks; a 6-digit code comes back and a
 * paired device or the hub's own shell approves it. The code poll
 * carries the request's secret, so only this browser sees the minted
 * credential. */
async function pairRequestStart() {
  const gen = ++pairGen;
  const name = (document.getElementById("pair-name") || {}).value || "";
  const err = document.getElementById("pair-error");
  if (err) err.textContent = "";
  let status = 0, data = null;
  try {
    const resp = await fetch("/v1/pair/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), device: pairMeta() }),
    });
    status = resp.status;
    try { data = await resp.json(); } catch { /* empty body */ }
  } catch { /* offline or connection lost */ }
  if (gen !== pairGen) return;
  if (status === 201 && data) {
    showPairCode(data);
    pairRequestPoll(data.request_id, data.poll_secret, gen, routeGen);
    return;
  }
  if (err) err.textContent = (status === 403)
    ? "This device isn't identifiable on the tailnet — use a pair token below."
    : (data && data.error && data.error.message) || `pair request failed (${status || "offline"})`;
}

function pairCodeEl(id) { return document.getElementById(id); }

function showPairCode(req) {
  const start = pairCodeEl("pair-start"), card = pairCodeEl("pair-code-card");
  if (!start || !card) return;
  start.hidden = true;
  card.hidden = false;
  const code = String(req.code || "");
  pairCodeEl("pair-code").textContent = `${code.slice(0, 3)} ${code.slice(3)}`;
  pairCodeEl("pair-code-flat").textContent = code;
  pairCodeEl("pair-node").textContent =
    `as ${(req.node && req.node.name) || "this device"}` +
    (req.node && req.node.login ? ` (${req.node.login})` : "");
  pairCodeEl("pair-code-status").textContent = "waiting for approval…";
  pairCodeEl("pair-retry").hidden = true;
}

/* Every 2 s until approved, denied or expired. A hidden tab asks nothing
 * (#1554). One listener, there for the whole poll, asks at once whenever
 * the tab is shown, so an approval made on another device lands as soon
 * as you come back, without waiting for the next tick. */
function pairRequestPoll(requestId, secret, gen, rgen) {
  let timer = null, asking = false;
  const done = () => {
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", shown);
  };
  const shown = () => {
    if (gen !== pairGen) return done();
    if (!document.hidden && !asking) { clearTimeout(timer); tick(); }
  };
  const tick = async () => {
    timer = null;
    if (gen !== pairGen) return done();
    // Hidden: no next tick; showing the tab asks (shown).
    if (document.hidden) return;
    asking = true;
    let status = 0, data = null;
    try {
      // The secret goes in a header, never the URL, which logs keep.
      const resp = await fetch(`/v1/pair/request/${encodeURIComponent(requestId)}`,
        { headers: { "X-Pair-Secret": secret } });
      status = resp.status;
      try { data = await resp.json(); } catch { /* empty body */ }
    } catch { /* transient — poll again */ }
    asking = false;
    if (gen !== pairGen) return done();
    if (data && data.status === "approved" && data.credential) {
      done();
      pairStore(data);
      if (rgen !== routeGen) return;
      history.replaceState(null, "", "#/overview");
      route();
      return;
    }
    if (data && data.status === "denied") { done(); return pairRequestFailed("Pairing was denied."); }
    if (status === 404) { done(); return pairRequestFailed("Request expired."); }
    timer = setTimeout(tick, 2000);
  };
  document.addEventListener("visibilitychange", shown);
  timer = setTimeout(tick, 2000);
}

function pairRequestFailed(msg) {
  ++pairGen;
  const st = pairCodeEl("pair-code-status");
  if (st) st.textContent = msg;
  const retry = pairCodeEl("pair-retry");
  if (retry) retry.hidden = false;
  // Back to the form, where the same message lands on another miss.
  const start = pairCodeEl("pair-start"), err = pairCodeEl("pair-error");
  if (start) start.hidden = false;
  if (err) err.textContent = msg;
}

/* Bare fragments pair and approve links carry: ``/#pair=<token>``, the
 * older ``/app/#pair=`` (the hub redirects it here, fragment intact) and
 * ``/#approve``. Returns true when the hash was claimed. */
function pairHash() {
  const hash = location.hash || "";
  if (hash === "#approve") {
    history.replaceState(null, "", "#/settings?section=integrations");
    return false;
  }
  const m = hash.match(/#pair=([^&]+)/);
  if (!m) return false;
  const nm = hash.match(/[?&]name=([^&]+)/);
  let token, name;
  try {
    token = decodeURIComponent(m[1]);
    name = nm ? decodeURIComponent(nm[1]) : "";
  } catch {
    // A link cut short mid-escape (#pair=abc%) can't be decoded; left to
    // throw, it stopped route() and the page stayed blank.
    renderSidebar("");
    renderGate("This pair link is damaged: it may have been cut short when it was copied. Copy the whole link, or mint a fresh one.", true);
    return true;
  }
  pairExchange(token, name);
  return true;
}

function renderSoon(id) {
  const label = SIDEBAR.flatMap(([, items]) => items).find(([key]) => key === id)?.[1] || id;
  const ready = SIDEBAR.flatMap(([, items]) => items).filter(([key]) => key in PAGES).map(([, l]) => l);
  document.getElementById("panel").innerHTML = `<div class="page">
    ${pageHeader(label, "This page is in the Mac app and not in the web app yet.")}
    <div class="card"><p class="empty">${esc(`Coming to the web app. Ready now: ${ready.join(", ")}.`)}</p></div></div>`;
}

/* The page the hash names: "#/skills?x=1" is "skills", a bare one
 * "overview". Pages compare it to their own id to tell whether they are
 * still the one showing before a late reply paints. */
function currentRoute() {
  return (location.hash.replace(/^#\/?/, "") || "overview").split("?")[0];
}

function route() {
  if (pairHash()) return;
  let id = currentRoute();
  // A Mac-only page has nothing to show here, so an old link or a typed
  // #/menubar lands on Overview instead.
  if (MAC_ONLY.has(id)) {
    id = "overview";
    history.replaceState(null, "", "#/overview");
  }
  // A response still in flight belongs to the page left behind, and so
  // does any sheet it opened.
  closeRouteSheets();
  routeGen++;
  overview.gen++;
  sessionsPage.gen++;
  harnessesPage.gen++;
  accountsPage.gen++;
  renderSidebar(id);
  (PAGES[id] || renderSoon)(id);
}

/* ---------------------------------------------------------------- Overview */

const PERIODS = [["today", "Today"], ["7d", "7 days"], ["month", "This month"], ["30d", "30 days"],
  ["90d", "90 days"], ["365d", "365 days"], ["all", "All time"]];
// machine: "" adds every machine up, else one machine's id (#525).
const overview = { period: "month", machine: "", data: null, gen: 0, focus: null };

/* First run (no shared AGENTS.md on the hub machine): the Mac app opens
 * its setup sheet once at launch, then remembers it was offered. Here a
 * bare / opens #/setup once per browser; after that Overview keeps a
 * card linking it until setup runs. */
const SETUP_OFFERED_KEY = "swe.setup.offered";
let landedBare = !location.hash.replace(/^#\/?/, "");
// Leaving before Overview answered spends the offer: only the landing counts.
window.addEventListener("hashchange", () => { landedBare = false; });

function offerSetupOnce(d) {
  const bare = landedBare;
  landedBare = false;
  if (!bare || !d.first_run) return false;
  try {
    if (localStorage.getItem(SETUP_OFFERED_KEY)) return false;
    localStorage.setItem(SETUP_OFFERED_KEY, "1");
  } catch { return false; }
  location.hash = "#/setup";
  return true;
}

function setupCard(d) {
  if (!d.first_run) return "";
  const where = d.machine ? esc(d.machine) : "this machine";
  return `<div class="card offer">
    <div class="offer-text"><div class="card-title">Set up ${where}</div>
      <p class="card-subtitle">Pick the agents you use, then give them one AGENTS.md, one skills folder and one list of MCP servers.</p></div>
    <a class="offer-button" href="#/setup">Set up</a></div>`;
}

function timeZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; }
}

async function renderOverview(_id, rescan = false) {
  const panel = document.getElementById("panel");
  const gen = ++overview.gen;
  if (!overview.data || rescan) {
    panel.innerHTML = `<div class="page">${overviewHeader(true)}<div class="card"><p class="empty">${
      overview.data ? "Rescanning…" : "Reading usage logs. The first scan can take a minute."}</p></div></div>`;
  } else {
    // Show what we have while the request runs, so the previous page's
    // controls never stay on screen.
    paintOverview();
  }
  const q = new URLSearchParams({ period: overview.period, tz: timeZone() });
  if (overview.machine) q.set("machine", overview.machine);
  if (rescan) q.set("rescan", "1");
  const { status, data } = await api(`/v1/app/overview?${q}`);
  if (gen !== overview.gen) return;
  if (status === 401) return renderGate();
  if (status !== 200 || !data) {
    const msg = failText(status, data, "then rescan");
    panel.innerHTML = `<div class="page">${overviewHeader(false)}<div class="card"><p class="error">${esc(msg)}</p></div></div>`;
    bindOverviewHeader();
    return;
  }
  overview.data = data;
  if (offerSetupOnce(data)) return;
  paintOverview();
}

function overviewHeader(busy) {
  const d = overview.data;
  const where = d ? overviewWhere(d) : "";
  const updated = d && d.scanned_at ? `<span class="caption">Updated ${Fmt.ago(d.scanned_at)}</span>` : "";
  return pageHeader("Overview", `Tokens and spend across every harness${where}.`,
    `${updated}<button class="icon-button${busy ? " spinning" : ""}" id="rescan" aria-label="Rescan" title="Rescan">${icon("rescan")}</button>`);
}

/* " on <machine>" or " across N machines" for the header. */
function overviewWhere(d) {
  const machines = d.machines || [];
  if (machines.length > 1 && (d.machine_filter || "all") === "all") {
    return ` across ${machines.length} machines`;
  }
  const one = machines.find((m) => m.id === d.machine_filter);
  const name = one ? one.name : d.machine;
  return name ? ` on ${name}` : "";
}

/* Each machine's tokens and spend for the period, the hub first, as rows
 * that filter the page to that machine; shown once a satellite reports
 * (#525). A satellite's figures are its daily totals, so its row and its
 * filtered page carry no top models. */
function machinesCard(d) {
  const machines = d.machines || [];
  if (machines.length < 2) return "";
  const total = machines.reduce((sum, m) => sum + m.tokens, 0);
  const pick = d.machine_filter || "all";
  const row = (id, name, tokens, cost, note) => {
    const share = total > 0 ? tokens / total : 0;
    return `<button class="machine-row${pick === id ? " selected" : ""}" data-machine="${esc(id)}"
      aria-pressed="${pick === id}" title="${esc(note || name)}">
      <span class="machine-name">${esc(name)}</span>
      <span class="machine-figures num">${Fmt.tokens(tokens)} · ${Fmt.dollars(cost)}</span>
      <span class="machine-bar" style="--share:${(share * 100).toFixed(1)}%"></span></button>`;
  };
  const cost = machines.reduce((sum, m) => sum + m.cost_usd, 0);
  const rows = [row("all", "All machines", total, cost, "Every machine added up")]
    .concat(machines.map((m) => row(m.id, m.name, m.tokens, m.cost_usd,
      m.hub ? `${m.name} (this hub)` : m.reported_at ? `${m.name}, reported ${Fmt.ago(m.reported_at)}` : m.name)));
  return `<div class="card"><div class="card-title">Machines</div>
    <div class="machine-rows">${rows.join("")}</div></div>`;
}

function bindOverviewHeader() {
  const btn = document.getElementById("rescan");
  if (btn) btn.onclick = () => renderOverview("overview", true);
}

function paintOverview() {
  const d = overview.data;
  const notes = (d.notes || []).map((n) => `<div class="card"><p class="note">${esc(n)}</p></div>`).join("");
  document.getElementById("panel").innerHTML = `<div class="page">
    ${overviewHeader(false)}
    <div class="stack">${setupCard(d)}${notes}
      <div class="overview">
        <div class="main stack">${totalCard(d)}${breakdownCard(d)}</div>
        <div class="side stack">${machinesCard(d)}${quickCard(d)}${activityCard(d)}</div>
      </div>
    </div></div>`;
  bindOverviewHeader();
  for (const btn of document.querySelectorAll("[data-machine]")) {
    btn.onclick = () => {
      overview.machine = btn.dataset.machine === "all" ? "" : btn.dataset.machine;
      renderOverview("overview");
    };
  }
  for (const tab of document.querySelectorAll("[data-period]")) {
    tab.onclick = () => { overview.period = tab.dataset.period; renderOverview("overview"); };
  }
  for (const tab of document.querySelectorAll("[data-breakdown]")) {
    tab.onclick = () => { overview.breakdown = tab.dataset.breakdown; paintOverview(); };
  }
  bindHarnessFocus();
  drawMarbles();
  drawHeatmap();
  bindHeatmap();
}

function rangeCaption(d, scale) {
  let range;
  if (d.period === "all") {
    range = d.first_day ? `Since ${dayLabel(d.first_day, true)}` : "";
  } else if (d.range) {
    const start = dayLabel(d.range.start_day), end = dayLabel(d.range.end_day);
    range = start === end ? start : `${start} to ${end}`;
  }
  let marble = "";
  if (scale && scale.counts.length) {
    marble = scale.bySpend
      ? `1 marble = ${scale.step >= 1 ? "$" + Math.round(scale.step).toLocaleString("en-US") : "$" + scale.step.toFixed(2)}`
      : `1 marble = ${Fmt.tokens(scale.step)} tokens`;
  }
  return [range, marble].filter(Boolean).join(" · ");
}

function dayLabel(iso, withYear = false) {
  const [y, m, day] = iso.split("-").map(Number);
  const opts = { month: "short", day: "numeric", timeZone: "UTC" };
  if (withYear) opts.year = "numeric";
  return new Date(Date.UTC(y, m - 1, day)).toLocaleDateString("en-US", opts);
}

function marbleCapacity() {
  const width = Math.min(document.getElementById("panel").clientWidth - 420, 900);
  return Math.min(Math.max(Math.floor(width / 4 / 10) * 10, 20), 220);
}

function totalCard(d) {
  const t = d.totals;
  // Biggest spender first, so the logo row reads as a ranking like the Mac's.
  const shares = d.harnesses.filter((h) => h.tokens > 0)
    .sort((a, b) => b.cost_usd - a.cost_usd || b.tokens - a.tokens);
  overview.scale = Fmt.marbleScale(shares, marbleCapacity());
  const chips = shares.map((h) => `<span class="harness-chip" data-harness="${esc(h.harness)}"
      title="${esc(Fmt.harnessName(h.harness))}" tabindex="0">${Fmt.logo(h.harness)}</span>`).join("");
  return `<div class="card total-card">
    <div class="tabs" role="tablist">${PERIODS.map(([id, label]) =>
      `<button class="tab${id === d.period ? " selected" : ""}" data-period="${id}" role="tab" aria-selected="${id === d.period}">${label}</button>`).join("")}</div>
    <div class="total-figures">
      <div class="total-tokens num" id="total-tokens">${Fmt.tokens(t.tokens)}</div>
      <div class="total-cost num" id="total-cost">${Fmt.dollars(t.cost_usd)}</div>
      <div class="total-range num" id="total-detail">${esc(rangeCaption(d, overview.scale))}</div>
      ${t.unpriced_tokens ? `<div class="total-range" title="${esc((t.unpriced_models || []).join(", "))}">${Fmt.tokens(t.unpriced_tokens)} tokens from models with no list price aren't in this total</div>` : ""}
      <div class="harness-chips" id="harness-chips">${chips}</div>
    </div>
    <svg class="marbles" id="marbles" preserveAspectRatio="xMidYMax meet"></svg>
  </div>`;
}

function bindHarnessFocus() {
  const row = document.getElementById("harness-chips");
  if (!row) return;
  const d = overview.data;
  const reset = () => {
    row.classList.remove("focusing");
    row.querySelectorAll(".harness-chip").forEach((c) => { c.classList.remove("focus"); c.style.borderColor = ""; });
    document.getElementById("total-tokens").textContent = Fmt.tokens(d.totals.tokens);
    document.getElementById("total-cost").textContent = Fmt.dollars(d.totals.cost_usd);
    document.getElementById("total-detail").textContent = rangeCaption(d, overview.scale);
  };
  // The Mac's "<Name> · <pct> of spend · N models": percent falls back to
  // the token share when nothing is priced, and squeezes to "<0.1%".
  const pct = (part) => part > 0 && part < 0.001 ? "<0.1%"
    : `${part < 0.1 ? (part * 100).toFixed(1) : Math.round(part * 100)}%`;
  for (const chip of row.querySelectorAll(".harness-chip")) {
    const focus = () => {
      const h = d.harnesses.find((x) => x.harness === chip.dataset.harness);
      if (!h) return;
      row.classList.add("focusing");
      chip.classList.add("focus");
      chip.style.borderColor = Fmt.harnessColor(h.harness);
      const costTotal = d.totals.cost_usd;
      const part = costTotal > 0 ? h.cost_usd / costTotal
        : d.totals.tokens > 0 ? h.tokens / d.totals.tokens : 0;
      document.getElementById("total-tokens").textContent = Fmt.tokens(h.tokens);
      document.getElementById("total-cost").textContent = Fmt.dollars(h.cost_usd);
      document.getElementById("total-detail").textContent =
        `${Fmt.harnessName(h.harness)} · ${pct(part)} of ${costTotal > 0 ? "spend" : "tokens"} · ${h.models} model${h.models === 1 ? "" : "s"}`;
    };
    chip.onmouseenter = focus; chip.onfocus = focus;
    chip.onmouseleave = reset; chip.onblur = reset;
  }
}

/* A still pile of marbles in harness colours along the card's floor,
 * kept clear of the figures in the middle. The Mac app drops them with
 * physics; a settled pile says the same thing. */
function drawMarbles() {
  const svg = document.getElementById("marbles");
  if (!svg || !overview.data) return;
  const box = svg.getBoundingClientRect();
  const width = Math.round(box.width) || 800, height = Math.round(box.height) || 240;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  // Keep clear of the widest line of the figures (the unpriced note can
  // be wider than the total), so no marble sits behind text.
  const r = 9, mid = width / 2;
  const widest = Math.max(0, ...[...document.querySelectorAll(".total-figures > *")]
    .map((el) => el.scrollWidth || el.getBoundingClientRect().width));
  const clear = Math.max(Math.min(380, width * 0.5), widest + 4 * r) / 2;
  // Every place a marble can sit, bottom row first, rows offset like a pile.
  const slots = [];
  for (let row = 0; row < 40; row++) {
    const y = height - r - 1 - row * r * 1.75;
    if (y < r) break;
    const shrink = row * r * 0.9;
    for (let x = r + (row % 2 ? r : 0) + shrink; x < width - r - shrink; x += r * 2) {
      if (Math.abs(x - mid) < clear && y < height - r * 2.5) continue;
      slots.push([x, y]);
    }
  }
  // The scale is picked for the room the pile really has, so the caption's
  // "1 marble = …" matches what is drawn.
  const shares = overview.data.harnesses.filter((h) => h.tokens > 0);
  overview.scale = Fmt.marbleScale(shares, Math.min(marbleCapacity(), slots.length));
  const row = document.getElementById("harness-chips");
  const detail = document.getElementById("total-detail");
  if (detail && !(row && row.classList.contains("focusing"))) {
    detail.textContent = rangeCaption(overview.data, overview.scale);
  }
  const marbles = [];
  for (const { harness, count } of overview.scale.counts) {
    for (let i = 0; i < count; i++) marbles.push(Fmt.harnessColor(harness));
  }
  let seed = 7;
  const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  const shuffled = marbles.map((c) => [rand(), c]).sort((a, b) => a[0] - b[0]).map((x) => x[1]);
  svg.innerHTML = shuffled.slice(0, slots.length).map((fill, i) =>
    `<circle cx="${slots[i][0].toFixed(1)}" cy="${slots[i][1].toFixed(1)}" r="${r - 0.6}" fill="${fill}" opacity=".9"/>`).join("");
}

function breakdownCard(d) {
  const which = overview.breakdown || "daily";
  const tabs = `<div class="tabs table">${[["daily", "Daily breakdown"], ["harness", "By harness"]].map(([id, label]) =>
    `<button class="tab${id === which ? " selected" : ""}" data-breakdown="${id}">${label}</button>`).join("")}</div>`;
  let body;
  if (which === "daily") {
    body = d.days.length ? `<div class="table-wrap"><table class="breakdown num">
      <thead><tr><th>Date</th><th>Total</th><th>Input</th><th>Output</th><th>Cached</th><th>Spend</th></tr></thead>
      <tbody>${d.days.map((r) => `<tr><td>${r.date}</td><td>${Fmt.tokens(r.tokens)}</td><td>${Fmt.tokens(r.input)}</td>
        <td>${Fmt.tokens(r.output)}</td><td>${Fmt.tokens(r.cached)}</td><td class="spend">${Fmt.dollars(r.cost_usd)}</td></tr>`).join("")}</tbody>
      </table></div>` : `<p class="empty">No usage in this period.</p>`;
  } else {
    const rows = d.harnesses.filter((h) => h.tokens > 0);
    body = rows.length ? `<table class="breakdown num"><tbody>${rows.map((h) => `<tr>
        <td><span class="name">${Fmt.logo(h.harness)}${esc(Fmt.harnessName(h.harness))}</span></td>
        <td class="caption">${Fmt.tokens(h.tokens)}</td><td class="spend">${Fmt.dollars(h.cost_usd)}</td></tr>`).join("")}</tbody></table>`
      : `<p class="empty">No usage in this period.</p>`;
  }
  return `<div class="card">${tabs}${body}</div>`;
}

function quickCard(d) {
  const q = d.quick;
  const chips = `<div class="chips num">
    <div class="chip"><div class="value">${q.active_days}</div><div class="label">active days</div></div>
    <div class="chip"><div class="value">${Fmt.tokens(q.tokens_per_day)}</div><div class="label">a day</div></div>
    <div class="chip"><div class="value">${q.busiest_day ? dayLabel(q.busiest_day) : "–"}</div><div class="label">busiest</div></div>
  </div>`;
  const models = d.top_models.length ? `<div class="models">${d.top_models.map((m, i) => {
    const marks = m.harnesses.slice(0, 3).map((h) => Fmt.logo(h, "logo logo-sm")).join("");
    const more = m.harnesses.length > 3 ? `+${m.harnesses.length - 3}` : "";
    return `<div class="model-row"><span class="rank">${i + 1}</span>
      <span class="model-name" title="${esc(m.model)}">${esc(m.model)}</span>
      <span class="model-harnesses">${marks}${more}</span>
      <span class="model-share num">${(m.share * 100).toFixed(1)}%</span></div>`;
  }).join("")}</div>` : `<p class="empty">No models in this period.</p>`;
  return `<div class="card">${chips}<div class="card-title">Top models</div>${models}</div>`;
}

function activityCard() {
  return `<div class="card"><div><div class="card-title">Activity</div>
    <div class="card-subtitle" id="heatmap-subtitle"></div></div>
    <div class="heatmap-wrap" id="heatmap-wrap"></div>
    <div class="legend">Less ${[0, 1, 2, 3, 4].map((l) => `<span class="cell" style="background: var(--heat-${l})"></span>`).join("")} More</div>
  </div>`;
}

/* As many recent weeks as the card's measured width fits, 8 to 26, like
 * the app. Runs after layout and again on resize. */
function drawHeatmap() {
  const wrap = document.getElementById("heatmap-wrap");
  if (!wrap || !overview.data) return;
  const hm = overview.data.heatmap;
  const fit = Math.floor((wrap.getBoundingClientRect().width - 24) / 15);
  const weeks = Math.max(8, Math.min(hm.weeks, fit || hm.weeks));
  const days = hm.days.slice((hm.weeks - weeks) * 7);
  const cells = [];
  // Column 0 carries the weekday labels; each week is a header cell
  // (month name on a month's first week) and seven days.
  cells.push('<span class="wd"></span>');
  ["", "Mon", "", "Wed", "", "Fri", ""].forEach((w) => cells.push(`<span class="wd">${w}</span>`));
  let lastMonth = "";
  for (let w = 0; w < weeks; w++) {
    const week = days.slice(w * 7, w * 7 + 7);
    const month = week.length ? dayLabel(week[0].date).split(" ")[0] : "";
    // A label is wider than its column, so the first week's gives way when
    // the next week already starts another month.
    const next = days[(w + 1) * 7] ? dayLabel(days[(w + 1) * 7].date).split(" ")[0] : month;
    const show = month !== lastMonth && !(w === 0 && next !== month);
    cells.push(`<span class="month">${show ? month : ""}</span>`);
    lastMonth = month;
    for (const day of week) {
      cells.push(day.future ? '<span></span>'
        : `<span class="cell l${day.level}" data-day="${day.date}" data-tokens="${day.tokens}" data-cost="${day.cost_usd}"></span>`);
    }
  }
  document.getElementById("heatmap-subtitle").textContent = `Tokens a day, last ${weeks} weeks`;
  wrap.innerHTML = `<div class="heatmap" style="grid-template-columns: 24px repeat(${weeks}, 12px)">${cells.join("")}</div>`;
}

function bindHeatmap() {
  const wrap = document.getElementById("heatmap-wrap");
  if (!wrap) return;
  let bubble = null;
  wrap.onmouseover = (e) => {
    const cell = e.target.closest("[data-day]");
    if (!cell) return;
    if (bubble) bubble.remove();
    const [y, m, day] = cell.dataset.day.split("-").map(Number);
    const label = new Date(Date.UTC(y, m - 1, day)).toLocaleDateString("en-US",
      { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
    const tokens = Number(cell.dataset.tokens);
    bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = `<div>${label}</div><div class="sub num">${
      tokens ? `${Fmt.tokens(tokens)} tokens · ${Fmt.dollars(Number(cell.dataset.cost))}` : "No usage"}</div>`;
    wrap.appendChild(bubble);
    const left = Math.min(cell.offsetLeft, wrap.clientWidth - bubble.offsetWidth);
    bubble.style.left = `${Math.max(0, left)}px`;
    bubble.style.top = `${cell.offsetTop - bubble.offsetHeight - 6}px`;
  };
  wrap.onmouseleave = () => { if (bubble) bubble.remove(); bubble = null; };
}

window.addEventListener("hashchange", route);
// The sidebar turns into a scrolling row at the CSS breakpoint: a window
// narrowed past it must bring the current tab into view too.
if (typeof window.matchMedia === "function") {
  window.matchMedia("(max-width: 720px)").addEventListener("change", () => {
    const nav = document.getElementById("sidebar");
    if (nav) revealActiveTab(nav);
  });
}
// A drag fires resize many times a frame: the charts redraw once per
// frame at most (#1557).
let overviewResizeFrame = 0;
window.addEventListener("resize", () => {
  if (overviewResizeFrame) return;
  overviewResizeFrame = requestAnimationFrame(() => {
    overviewResizeFrame = 0;
    if (!overview.data || !document.getElementById("marbles")) return;
    drawMarbles();
    drawHeatmap();
  });
});
// After every script, page plugins included, has run.
document.addEventListener("DOMContentLoaded", route);
