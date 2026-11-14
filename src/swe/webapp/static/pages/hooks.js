/* Hooks: a port of the Mac app's HooksPage. Every script under
 * ~/.swe/hooks with its link state, tier and declarations, the
 * swe hooks check findings, Link with swe init and Replace and back up
 * (the AgentsMdPage strip), and New hook (swe hooks new — the sheet
 * shows the stanza to paste; a settings file is never written). The
 * list is #/hooks; one hook is #/hooks?hook=<name>&harness=<h>.
 * Data: /v1/app/hooks, POST /v1/app/hooks/link|replace|new
 * (pages/hooks.py). Run counts (fires, blocks, block rate, top reasons,
 * recent runs) come from /v1/app/hooks?stats=<window>, a read the hub
 * runs off the request: the list paints first, its number columns fill
 * in when the stats answer, and a slow read polls ("loading"). The bundle runs each page script in its own scope,
 * so helpers are hk-prefixed and copies, not another page's. */
"use strict";

const hooksPage = {
  data: null, seq: 0,
  // HooksModel.linking ("link" | "replace" while init runs), the inline
  // confirm Replace opens, and the last run's failure (actionError).
  running: null, confirming: false, actionError: null,
  newToken: 0,            // the New hook form on show; older replies are stale
  // Run stats: the chosen window, what each window last answered
  // ({state: "loading"|"ready"|"error", rows: {source_path: row},
  // uncertain: Set of source_path, at}), and a counter that drops the
  // replies of an older ask.
  window: null, statsBy: {}, statsSeq: 0, forceStats: false,
};

/* The windows the page offers: [value, option label, "in the last ..."]. */
const HK_WINDOWS = [
  ["24h", "Last 24 hours", "24 hours"],
  ["7d", "Last 7 days", "7 days"],
  ["30d", "Last 30 days", "30 days"],
];
const HK_WINDOW_KEY = "swe.hooks.window";
const HK_STATS_FRESH_MS = 20000;
let HK_POLL_MS = 1500;   // how often a loading or refreshing read is asked again
const HK_DASH = "\u2013";

/* The chosen window, kept per browser. */
function hkWindow() {
  const p = hooksPage;
  if (!p.window) {
    let saved = null;
    try { saved = localStorage.getItem(HK_WINDOW_KEY); } catch { /* private window */ }
    p.window = HK_WINDOWS.some((w) => w[0] === saved) ? saved : "7d";
  }
  return p.window;
}
const hkWindowSpan = () => (HK_WINDOWS.find((w) => w[0] === hkWindow()) || HK_WINDOWS[1])[2];

/* The stats row a hook's numbers come from: the hub names the list rows
 * each stats row belongs to (``source_paths``, worked out once by
 * swe's hooks/identity.py), so the join is the list row's
 * ``source_path`` and nothing else. A source the hub marks uncertain (a
 * run it could not tell from another hook's) shows dashes, never a
 * count that may be short or someone else's. */
function hkMatch(source, s) {
  if (!s || s.state !== "ready" || !source) return null;
  if (s.uncertain && s.uncertain.has(source)) return null;
  const row = s.rows[source];
  return row && row.fires ? row : null;
}
const hkUncertain = (source, s) => !!(s && s.state === "ready" && s.uncertain && s.uncertain.has(source));

// The app's "link" and "plus" glyphs, and the lock note's.
const HK_LINK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.2 1.2"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.2-1.2"/></svg>';
const HK_PLUS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
const HK_LOCK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';

/* LinkState.title */
const HK_STATE_TITLE = {
  linked: "Linked", relink: "Points elsewhere", create: "Not linked", absorb: "Copy to absorb",
  keep: "Only copy, kept", conflict: "File in the way", skipped: "Not installed", ignored: "Archived",
};
const hkStateTitle = (s) => HK_STATE_TITLE[s] || (s ? s[0].toUpperCase() + s.slice(1) : "");
const hkInitFixes = (s) => ["create", "relink", "absorb"].includes(s);
const hkBlocked = (s) => ["conflict", "keep"].includes(s);

/* Hook.Tier.title */
const HK_TIER_TITLE = { shared: "Shared", harness: "Harness", foreign: "Foreign" };

/* HookFinding.kindTitle */
const HK_FINDING_TITLE = { "declared-missing": "Declared, missing", undeclared: "Undeclared", unlinked: "Unlinked", managed: "Managed elsewhere" };

/* `~/…` for a path under the hub's home (the list's, when not given). */
function hkPath(path, home) {
  return Fmt.tilde(path, home || (hooksPage.data && hooksPage.data.home));
}

/* HookFinding.displayMessage: home paths as `~/…`, and a managed note cut
 * to its path, since the badge already says "Managed elsewhere". */
function hkFindingMessage(f, home) {
  let text = f.message;
  const cut = text.indexOf(" — declaration managed elsewhere");
  if (f.kind === "managed" && cut >= 0) text = text.slice(0, cut);
  const h = (home || "").replace(/\/$/, "");
  if (!h) return text;
  // Only where a path starts: `/backup/Users/k/x` is not under home.
  const escaped = (h + "/").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return text.replace(new RegExp(`(?<![^\\s("'])${escaped}`, "g"), "~/");
}

/* HarnessMarkView (Fmt.markView). */
const hkMark = (h) => Fmt.markView(h, "hk-mark");

/* HookDeclaration.label: PreToolUse:Bash, or Stop with no matcher. */
const hkDeclLabel = (d) => (d.matcher ? `${d.event}:${d.matcher}` : d.event);

const hooksOnRoute = () => /^#\/hooks(\?|$)/.test(location.hash);

/* ------------------------------------------------------------------ list */

function renderHooks() {
  const q = new URLSearchParams((location.hash.split("?")[1]) || "");
  const hook = q.get("hook");
  if (hook) return renderHookDetail(hook, q.get("harness") || "", q.get("tier") || "");
  return renderHooksList();
}

async function renderHooksList() {
  const p = hooksPage, gen = routeGen, mine = ++p.seq;
  if (!p.data) {
    document.getElementById("panel").innerHTML = `<div class="page">${hooksHeader(true)}
      ${hooksSkeleton()}</div>`;
    bindHooksHeader();
  } else {
    paintHooks();
  }
  // The counts are their own request: the list never waits for them.
  hooksLoadStats();
  const { status, data } = await api("/v1/app/hooks");
  if (gen !== routeGen || mine !== p.seq) return;
  if (status === 401) return renderGate();
  if (status !== 200 || !data) return pageError(hooksHeader(false), status, data, renderHooks);
  p.data = data;
  paintHooks();
}

function hooksHeader(busy) {
  return pageHeader("Hooks",
    "Scripts in ~/.swe/hooks that run when a harness fires an event, and which harnesses each is linked and declared for.",
    `<button class="btn hairline hk-button primary hk-new-button" id="hk-new" type="button" title="Write a new hook script into ~/.swe/hooks">${HK_PLUS}New hook</button>
     <button class="icon-button${busy ? " spinning" : ""}" id="page-refresh" aria-label="Refresh" title="Refresh">${icon("rescan")}</button>`);
}

function bindHooksHeader() {
  const add = document.getElementById("hk-new");
  if (add) add.onclick = () => openNewHook();
  // Refresh asks the hub for a new read, not the cached one.
  bindRefresh(renderHooks, () => { hooksPage.confirming = false; hooksPage.forceStats = true; });
}

function paintHooks() {
  const p = hooksPage, d = p.data;
  let body;
  if (!d.hooks.length) {
    body = `<div class="card hk-empty"><p class="empty">No hooks in ~/.swe/hooks yet. A hook script runs when its harness fires an event.</p>
      <div><button class="btn hairline hk-button primary" id="hk-new-empty" type="button">New hook</button></div></div>`;
  } else {
    body = hkGroups(d.hooks).map(hkGroupCard).join("");
  }
  document.getElementById("panel").innerHTML = `<div class="page">${hooksHeader(false)}
    <div class="stack">
      ${p.actionError ? `<div class="card hk-action-error"><p class="error" role="alert">${esc(p.actionError)}</p></div>` : ""}
      ${d.check_error ? `<p class="note">The hooks list loaded, but its check didn't run: ${esc(d.check_error)}</p>` : ""}
      ${d.warning ? `<p class="note">${esc(d.warning)}</p>` : ""}
      ${hkLinksStrip(d)}
      ${d.findings.length ? hkFindingsCard(d) : ""}
      ${d.hooks.length ? hkStatsBar() : ""}
      ${body}
    </div></div>`;
  bindHooksHeader();
  bindHkWindow();
  const addEmpty = document.getElementById("hk-new-empty");
  if (addEmpty) addEmpty.onclick = () => openNewHook();
  for (const btn of document.querySelectorAll("[data-hk]")) {
    btn.onclick = () => {
      const act = btn.dataset.hk;
      if (act === "link" || act === "replace") hooksRun(act);
      else if (act === "ask" || act === "cancel") {
        p.confirming = act === "ask" ? !p.confirming : false;
        paintHooks();
        const next = document.querySelector(`[data-hk="${p.confirming ? "replace" : "ask"}"]`);
        if (next) next.focus();
      }
    };
  }
  const confirm = document.querySelector(".hk-confirm");
  if (confirm) {
    confirm.onkeydown = (event) => {
      if (event.key !== "Escape") return;
      p.confirming = false;
      paintHooks();
      const ask = document.querySelector('[data-hk="ask"]');
      if (ask) ask.focus();
    };
  }
}

/* LinksStrip: what swe init would do, and the buttons that run it. The
 * strip is the AGENTS.md page's — init also links the shared skills and
 * instructions, so the copy says so. */
function hkLinksStrip(d) {
  const p = hooksPage;
  const pending = d.hooks.filter((h) => hkInitFixes(h.link_status));
  const blocked = d.hooks.filter((h) => hkBlocked(h.link_status));
  if (!pending.length && !blocked.length && !p.running) return "";
  const link = pending.length || p.running === "link"
    ? `<button class="btn hairline hk-button primary hk-run" data-hk="link"${p.running ? " disabled" : ""}
        title="Runs swe init, which also links the shared skills and instructions and never replaces a real file.">${
        p.running === "link" ? '<span class="spinner ink" aria-hidden="true"></span>Linking…' : `${HK_LINK_ICON}Link with swe init`}</button>` : "";
  const replace = blocked.length || p.running === "replace" ? `<div class="hk-blocked">
      <p class="caption">A real file is in the way for ${blocked.length === 1 ? "a hook" : `${blocked.length} hooks`}. swe init leaves those alone.</p>
      <button class="btn hairline hk-button hk-run" data-hk="ask" aria-expanded="${p.confirming}"${p.running ? " disabled" : ""}>${
        p.running === "replace" ? '<span class="spinner ink" aria-hidden="true"></span>Replacing…' : "Replace and back up…"}</button></div>
    ${p.confirming && !p.running ? hkConfirm(d) : ""}` : "";
  return `<div class="hk-strip"><div class="hk-strip-row">
      <span class="card-title" title="From swe init --check">Links</span>
      <span class="caption">${pending.length
        ? `${pending.length} ${pending.length === 1 ? "hook" : "hooks"} will be linked into place.`
        : "Every hook script is where its harness reads it."}</span>
      ${link}</div>
    ${replace}
  </div>`;
}

/* Replace and back up asks first: it deletes real files after copying them. */
function hkConfirm(d) {
  const machine = d.machine || "the hub";
  return `<div class="hk-confirm" role="group" aria-label="Run swe init --force?">
    <p class="caption">This runs swe init --force on ${esc(machine)}. Each of these is copied to ~/.swe/backups, then replaced with a link to the shared copy:</p>
    <ul class="hk-confirm-paths">${(d.blocked || []).map((path) => `<li><code>${esc(path)}</code></li>`).join("")}</ul>
    <div class="hk-confirm-buttons">
      <button class="btn hairline hk-button" data-hk="cancel">Cancel</button>
      <button class="btn hairline hk-button danger" data-hk="replace">Replace and back up</button>
    </div></div>`;
}

/* FindingsCard: the swe hooks check report. managed is a note, not drift. */
function hkFindingsCard(d) {
  return `<div class="card"><div><div class="card-title">Check</div>
    <div class="card-subtitle">swe hooks check compares the declarations, the links and the hub.</div></div>
    <div class="hk-findings">${d.findings.map((f) => `<div class="hk-finding">
      <span class="badge${f.kind === "managed" ? " hk-dim" : ""}">${esc(HK_FINDING_TITLE[f.kind] || f.kind)}</span>
      ${f.harness ? `<span class="hk-fmark">${hkMark(f.harness)}</span>` : ""}
      <span class="caption hk-fmsg">${esc(hkFindingMessage(f, d.home))}</span></div>`).join("")}</div></div>`;
}

/* HooksModel.groups: Shared first, Foreign last, harnesses in
 * first-seen order between. */
function hkGroups(hooks) {
  const order = [], byKey = {};
  for (const h of hooks) {
    const key = h.tier === "shared" ? "shared" : h.tier === "foreign" ? "foreign" : h.harness;
    if (!byKey[key]) { order.push(key); byKey[key] = []; }
    byKey[key].push(h);
  }
  const groups = order.map((key) => ({ key, hooks: byKey[key] }));
  return groups.filter((g) => g.key === "shared")
    .concat(groups.filter((g) => g.key !== "shared" && g.key !== "foreign"))
    .concat(groups.filter((g) => g.key === "foreign"));
}

function hkGroupCard(g) {
  let head;
  if (g.key === "shared") head = `<span class="card-title" title="Scripts every harness can link">Shared</span>`;
  else if (g.key === "foreign") head = `<span class="card-title" title="Scripts swe lists but does not manage">Foreign</span>`;
  else head = `<div class="hk-group-head">${hkMark(g.key)}<span class="card-title">${esc(Fmt.harnessName(g.key))}</span>
    <span class="caption">${g.hooks.length === 1 ? "1 hook" : `${g.hooks.length} hooks`}</span></div>`;
  return `<div class="card hk-group">${head}<div class="hk-rows">${hkColsHeader()}${g.hooks.map(hkRow).join("")}</div></div>`;
}

/* Grouped hook rows in the list's own shape: a card per harness, rows
 * with name + badges, the script path and a chevron. */
function hooksSkeleton() {
  const row = (i) => `<div class="hk-row"><span class="hk-main">
    <span class="hk-title">${Sk.bar(120 - (i % 3) * 20, 13)}${Sk.pill(52)}${i % 2 ? Sk.pill(62) : ""}</span>
    ${Sk.bar(`${52 - (i % 4) * 9}%`, 11)}
    ${i % 2 ? `<span class="hk-badges">${Sk.pill(56)}${Sk.pill(72)}</span>` : ""}
  </span>${hkNumsWrap("", "loading")}<span class="hk-chevron">${Sk.bar(8, 16)}</span></div>`;
  const group = (n, off) => `<div class="card hk-group"><div class="hk-group-head">${Sk.bar(16, 16)}${Sk.bar(96 + off * 20, 14)}
    <span class="caption">${Sk.bar(52, 11)}</span></div>
    <div class="hk-rows">${hkColsHeader()}${Array.from({ length: n }, (_, i) => row(i + off)).join("")}</div></div>`;
  return `<div role="status" aria-label="Loading" class="stack">${group(3, 0)}${group(2, 3)}</div>`;
}

function hkRow(h) {
  const declared = (h.declared || []).map((d) => `<span class="badge">${esc(hkDeclLabel(d))}</span>`).join("");
  const events = (h.events_supported || []).map((e) => `<span class="badge">${esc(e)}</span>`).join("");
  const undeclared = h.link_status === "linked" && !(h.declared || []).length;
  const enabled = h.enabled == null ? "" : `<span class="badge">${h.enabled ? "Enabled" : "Disabled"}</span>`;
  const to = `#/hooks?hook=${encodeURIComponent(h.name)}&harness=${encodeURIComponent(h.harness)}&tier=${encodeURIComponent(h.tier)}`;
  return `<a class="hk-row" href="${esc(to)}" title="Open ${esc(h.name)}">
    <span class="hk-main">
      <span class="hk-title"><span class="name">${esc(h.name)}</span>
        <span class="badge">${esc(hkStateTitle(h.link_status))}</span>
        ${undeclared ? '<span class="badge">Undeclared</span>' : ""}${enabled}</span>
      <code class="hk-path" title="${esc(h.dest_path)}">${esc(hkPath(h.dest_path))}</code>
      ${h.detail ? `<span class="caption">${esc(h.detail)}</span>` : ""}
      ${declared || events ? `<span class="hk-badges">${declared}${events}</span>` : ""}
    </span>
    ${hkNumsWrap(h.source_path)}
    <span class="hk-chevron" aria-hidden="true">›</span></a>`;
}

/* ---------------------------------------------------------------- detail */

async function renderHookDetail(name, harness, tier) {
  const p = hooksPage, gen = routeGen, mine = ++p.seq;
  document.getElementById("panel").innerHTML = `<div class="page">${hooksHeader(false)}${hkBack()}
    <div class="stack" role="status" aria-label="Loading"><div class="card hk-head">${Sk.bar("40%", 18)}
      ${Sk.bar("55%", 11)}</div>
    <div class="card hk-doc"><div class="card-subtitle">${Sk.bar(200, 11)}</div>
      <pre class="hk-text"><div class="skel-para">${Sk.linesInner(10)}</div></pre></div></div></div>`;
  bindHooksHeader();
  hooksLoadStats();
  const q = new URLSearchParams({ hook: name });
  if (harness) q.set("harness", harness);
  if (tier) q.set("tier", tier);
  const { status, data } = await api(`/v1/app/hooks?${q}`);
  if (gen !== routeGen || mine !== p.seq) return;
  if (status === 401) return renderGate();
  if (status !== 200 || !data) {
    const msg = failText(status, data);
    document.getElementById("panel").innerHTML = `<div class="page">${hooksHeader(false)}${hkBack()}
      <div class="card"><p class="error">${esc(msg)}</p></div></div>`;
    bindHooksHeader();
    return;
  }
  paintHookDetail(data);
}

function hkBack() {
  return `<a class="hk-back" href="#/hooks">‹ Hooks</a>`;
}

function paintHookDetail(d) {
  const undeclared = d.link_status === "linked" && !(d.declared || []).length;
  const enabled = d.enabled == null ? "" : `<span class="badge">${d.enabled ? "Enabled" : "Disabled"}</span>`;
  const lock = !d.editable ? `<div class="hk-lock">${HK_LOCK_ICON}<span>Only scripts inside ~/.swe/hooks can be edited, so ${esc(d.name)} is read-only.</span></div>` : "";
  const declared = (d.declared || []).map((x) => `<div class="hk-decl">
      <span class="hk-decl-head"><span class="badge">${esc(hkDeclLabel(x))}</span>
        ${x.timeout ? `<span class="caption">timeout ${esc(x.timeout)}s</span>` : ""}
        <code class="hk-mono">${esc(x.command)}</code></span>
      <code class="hk-mono hk-file">${esc(hkPath(x.file, d.home))}</code></div>`).join("");
  const events = (d.events_supported || []).length ? `<div class="hk-events"><span class="caption">Events</span>
    ${d.events_supported.map((e) => `<span class="badge">${esc(e)}</span>`).join("")}</div>` : "";
  let doc;
  if (!d.exists) doc = `<div class="card"><p class="caption">The script is missing on disk; swe init may have removed it.</p></div>`;
  else if (d.is_dir) doc = `<div class="card"><p class="caption">A directory hook — the scripts it runs live inside ${esc(hkPath(d.source_path, d.home))}.</p></div>`;
  else if (d.text == null) doc = `<div class="card"><p class="caption">Can't preview this file.</p></div>`;
  else doc = `<div class="card hk-doc"><div class="card-subtitle hk-mono">${esc(hkPath(d.source_path, d.home))}</div>
      <pre class="hk-text">${esc(d.text)}</pre></div>`;
  document.getElementById("panel").innerHTML = `<div class="page">${hooksHeader(false)}${hkBack()}
    <div class="stack">
      ${lock}
      <div class="card hk-head">
        <div class="hk-title"><span class="name">${esc(d.name)}</span>
          <span class="badge">${esc(hkStateTitle(d.link_status))}</span>
          ${undeclared ? '<span class="badge">Undeclared</span>' : ""}${enabled}</div>
        <div class="hk-line">${d.harness === "shared" ? '<span class="caption">Shared</span>'
          : `${hkMark(d.harness)}<span class="caption">${esc(Fmt.harnessName(d.harness))}</span>`}
          ${d.tier ? `<span class="badge">${esc(HK_TIER_TITLE[d.tier] || d.tier)}</span>` : ""}</div>
        ${d.summary ? `<p class="hk-body">${esc(d.summary)}</p>` : ""}
        <div class="hk-kv"><span class="caption">Linked at</span><code class="hk-mono" title="${esc(d.dest_path)}">${esc(hkPath(d.dest_path, d.home))}</code></div>
        ${d.detail ? `<p class="caption">${esc(d.detail)}</p>` : ""}
        ${declared ? `<div class="hk-decls"><span class="caption">Declared in</span>${declared}</div>` : ""}
        ${events}
      </div>
      ${hkDetailStats(d.source_path)}
      ${doc}
    </div></div>`;
  bindHooksHeader();
  bindHkWindow();
}

/* ----------------------------------------------------------- run stats */

const hkDelay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const hkCount = (n) => Number(n || 0).toLocaleString("en-US");

/* What the current window last answered, or undefined before the first ask. */
const hkStats = () => hooksPage.statsBy[hkWindow()];

/* One hook's row in the current window, or undefined with no runs. */
const hkStatRow = (source) => hkMatch(source, hkStats());

/* Ask for the current window. A window answered under HK_STATS_FRESH_MS
 * ago is reused; an older one stays on screen while its refresh runs.
 * "loading" is the hub saying the read is still going: ask again. */
async function hooksLoadStats() {
  const p = hooksPage, w = hkWindow(), mine = ++p.statsSeq;
  const force = p.forceStats;
  p.forceStats = false;
  const have = p.statsBy[w];
  if (!force && have && have.state === "ready" && Date.now() - have.at < HK_STATS_FRESH_MS) {
    hkPaintStats(false);
    return;
  }
  if (!have || have.state !== "ready") {
    p.statsBy[w] = { state: "loading", rows: {}, uncertain: new Set(), at: 0 };
    hkPaintStats(false);
  }
  for (let tries = 0; tries < 60; tries++) {
    // Only the first ask forces: the polls after it just wait for the read.
    const { status, data } = await api(`/v1/app/hooks?stats=${w}${force && !tries ? "&refresh=1" : ""}`);
    if (mine !== p.statsSeq || !hooksOnRoute()) return;
    if (status === 200 && data && data.state === "ready") {
      const rows = {};
      for (const r of data.hooks || []) for (const sp of r.source_paths || []) rows[sp] = r;
      const uncertain = new Set(data.uncertain || []);
      // An answer with a newer read behind it shows, but is not fresh.
      p.statsBy[w] = { state: "ready", rows, uncertain, at: data.refreshing ? 0 : Date.now() };
      hkPaintStats(true);
      if (!data.refreshing) return;
    } else if (!(status === 200 && data && data.state === "loading")) {
      if (!have || have.state !== "ready") p.statsBy[w] = { state: "error", rows: {}, uncertain: new Set(), at: Date.now() };
      hkPaintStats(true);
      return;
    }
    await hkDelay(HK_POLL_MS);
    if (mine !== p.statsSeq || !hooksOnRoute()) return;
  }
  if (!p.statsBy[w] || p.statsBy[w].state !== "ready") p.statsBy[w] = { state: "error", rows: {}, uncertain: new Set(), at: Date.now() };
  hkPaintStats(true);
}

/* The number columns of one row. Loading, error and no runs hold their
 * three cells at the same width, so nothing moves when the numbers land. */
function hkNumsHtml(source, forced) {
  const s = forced ? { state: forced } : hkStats();
  const cell = (inner, cls = "") => `<span class="hk-num${cls ? ` ${cls}` : ""}">${inner}</span>`;
  const dash = cell(HK_DASH, "hk-zero");
  if (!s || s.state === "loading") return cell(Sk.num(22)) + cell(Sk.num(22)) + cell(Sk.num(24));
  const row = hkMatch(source, s);
  if (!row) return dash + dash + dash;
  const pct = Math.round(row.block_rate * 100);
  const rate = row.blocks ? (pct < 1 ? "&lt;1%" : `${pct}%`) : "0%";
  return cell(hkCount(row.fires))
    + cell(hkCount(row.blocks), row.blocks ? "hk-bad" : "hk-zero")
    + cell(rate, row.blocks ? "hk-bad" : "hk-zero");
}

function hkNumsTitle(source) {
  const row = hkStatRow(source);
  if (!row) return "";
  return `${hkCount(row.fires)} ${row.fires === 1 ? "run" : "runs"}, ${hkCount(row.blocks)} ${row.blocks === 1 ? "block" : "blocks"} in the last ${hkWindowSpan()}`;
}

function hkNumsWrap(source, forced) {
  // A forced state is the list's own skeleton: nothing to patch later.
  if (forced) return `<span class="hk-nums">${hkNumsHtml(source, forced)}</span>`;
  const title = hkNumsTitle(source);
  return `<span class="hk-nums" data-hk-source="${esc(source || "")}"${title ? ` title="${esc(title)}"` : ""}>${hkNumsHtml(source)}</span>`;
}

/* The column labels over each card's rows. The spacer matches the
 * chevron, so the labels sit over the numbers. */
function hkColsHeader() {
  return `<div class="hk-cols"><span class="hk-cols-gap"></span>
    <span class="hk-nums"><span class="hk-num">Fires</span><span class="hk-num">Blocks</span><span class="hk-num">Rate</span></span>
    <span class="hk-chevron" aria-hidden="true"></span></div>`;
}

function hkWindowSelect() {
  const w = hkWindow();
  return `<select class="field hk-window" aria-label="Run counts window">${HK_WINDOWS.map(([v, label]) =>
    `<option value="${v}"${v === w ? " selected" : ""}>${label}</option>`).join("")}</select>`;
}

function hkStatsNote() {
  const s = hkStats();
  if (s && s.state === "error") return "Run counts are not available right now.";
  if (s && s.state === "ready" && !Object.keys(s.rows).length) return `No hook runs recorded in the last ${hkWindowSpan()}.`;
  return "Runs, with every harness added up.";
}

function hkStatsBar() {
  return `<div class="hk-statsbar"><span class="caption" id="hk-stats-note">${esc(hkStatsNote())}</span>${hkWindowSelect()}</div>`;
}

/* The picker: remembers the choice and asks for that window. */
function bindHkWindow() {
  for (const sel of document.querySelectorAll(".hk-window")) {
    sel.onchange = () => {
      const p = hooksPage;
      p.window = sel.value;
      try { localStorage.setItem(HK_WINDOW_KEY, sel.value); } catch { /* private window */ }
      for (const other of document.querySelectorAll(".hk-window")) other.value = sel.value;
      hooksLoadStats();
    };
  }
}

/* Patch the numbers in place, so the picker keeps focus and the page does
 * not repaint. ``animate`` fades the new numbers in (not under reduced
 * motion, which the stylesheet honours). */
function hkPaintStats(animate) {
  const note = document.getElementById("hk-stats-note");
  if (note) note.textContent = hkStatsNote();
  for (const box of document.querySelectorAll(".hk-nums[data-hk-source]")) {
    const source = box.dataset.hkSource, title = hkNumsTitle(source);
    box.innerHTML = hkNumsHtml(source);
    if (title) box.title = title; else box.removeAttribute("title");
    hkFade(box, animate);
  }
  const card = document.getElementById("hk-detail-stats");
  if (card) {
    // The picker is replaced with the rest: keep the keyboard on it.
    const focused = card.contains(document.activeElement);
    card.innerHTML = hkDetailStatsInner(card.dataset.hkSource);
    hkFade(card, animate);
    bindHkWindow();
    if (focused) card.querySelector(".hk-window").focus();
  }
}

function hkFade(el, animate) {
  el.classList.remove("hk-fresh");
  if (!animate) return;
  void el.offsetWidth;   // restart the animation
  el.classList.add("hk-fresh");
}

/* "Last block" and "Recent runs" ages, as the other pages write them. */
const hkAgo = (ts) => (ts ? Fmt.ago(ts) : HK_DASH);

const HK_DECISION = { block: "hk-bad", warn: "hk-warn", error: "hk-warn", allow: "hk-zero" };
const HK_REASON_WORD = { block: ["block", "blocks"], warn: ["warn", "warns"], error: ["error", "errors"] };

/* A repo's folder name: the repo root when the run had one, else its cwd. */
function hkRepoName(run) {
  const place = String(run.repo || run.cwd || "").replace(/\/+$/, "");
  return place ? place.split("/").pop() : "";
}

function hkTile(label, value, cls = "") {
  return `<div class="hk-tile"><span class="caption">${label}</span>
    <span class="hk-tile-value${cls ? ` ${cls}` : ""}">${value}</span></div>`;
}

function hkDetailStatsInner(source) {
  const s = hkStats();
  const head = `<div class="hk-stats-head"><div class="card-title">Runs</div>${hkWindowSelect()}</div>`;
  if (!s || s.state === "loading") {
    return `${head}<div class="hk-tiles" role="status" aria-label="Loading">${[0, 1, 2].map(() => `<div class="hk-tile">${Sk.bar(40, 11)}${Sk.bar(54, 18)}</div>`).join("")}</div>`;
  }
  if (s.state === "error") {
    return `${head}<div class="hk-tiles">${hkTile("Fires", HK_DASH, "hk-zero")}${hkTile("Blocks", HK_DASH, "hk-zero")}${hkTile("Last block", HK_DASH, "hk-zero")}</div>
      <p class="caption">Run counts are not available right now.</p>`;
  }
  if (hkUncertain(source, s)) {
    return `${head}<p class="caption hk-none">swe can't tell which runs belong to this script.</p>`;
  }
  const row = hkMatch(source, s);
  if (!row) {
    return `${head}<p class="caption hk-none">No runs in the last ${hkWindowSpan()}.</p>`;
  }
  const reasons = (row.top_reasons || []).slice(0, 5).map((r) => {
    const word = (HK_REASON_WORD[r.decision] || [r.decision, r.decision + "s"])[r.count === 1 ? 0 : 1];
    return `<div class="hk-line-row"><span class="hk-line-text" title="${esc(r.reason)}">${esc(r.reason)}</span>
      <span class="caption hk-line-side">${hkCount(r.count)} ${esc(word)}</span></div>`;
  }).join("");
  const runs = (row.recent || []).slice(0, 5).map((r) => {
    const repo = hkRepoName(r);
    const where = [repo, r.git_branch].filter(Boolean).map((t) => esc(t)).join(" · ");
    return `<div class="hk-line-row"><span class="hk-line-text"><span class="${HK_DECISION[r.decision] || ""}">${esc(r.decision)}</span>${where ? ` · ${where}` : ""}</span>
      <span class="caption hk-line-side">${esc(hkAgo(r.ts))}</span></div>`;
  }).join("");
  return `${head}
    <div class="hk-tiles">${hkTile("Fires", hkCount(row.fires))}${hkTile("Blocks", hkCount(row.blocks), row.blocks ? "hk-bad" : "hk-zero")}${hkTile("Last block", esc(hkAgo(row.last_block)), row.last_block ? "" : "hk-zero")}</div>
    ${reasons ? `<div class="hk-sub"><span class="caption">Top reasons</span>${reasons}</div>` : ""}
    ${runs ? `<div class="hk-sub"><span class="caption">Recent runs</span>${runs}</div>` : ""}`;
}

/* The detail page's card: the same numbers as the list's row, opened up. */
function hkDetailStats(source) {
  return `<div class="card hk-stats-card" id="hk-detail-stats" data-hk-source="${esc(source || "")}">${hkDetailStatsInner(source)}</div>`;
}

/* ---------------------------------------------------------------- writes */

/* HooksModel.link: one init at a time; the hub answers with the list
 * read again, whether the run worked or not. */
async function hooksRun(action) {
  const p = hooksPage;
  if (p.running) return;
  p.running = action;
  p.confirming = false;
  p.actionError = null;
  paintHooks();
  const { status, data } = await apiPost(`/v1/app/hooks/${action}`, {});
  p.running = null;
  if (status === 401) {
    if (hooksOnRoute()) renderGate();
    return;
  }
  if (status === 200 && data) {
    // A list read still in flight began before this run: drop it.
    p.seq++;
    p.data = data;
    p.actionError = data.error || null;
  } else {
    p.actionError = failText(status, data);
  }
  if (!hooksOnRoute()) return;
  if (new URLSearchParams((location.hash.split("?")[1]) || "").get("hook")) renderHooks();
  else paintHooks();
}

/* ------------------------------------------------------------- new hook */

/* NewHookSheet.nameProblem: swe hooks new's own rule from create.py. */
const HK_NAME_RULE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
function hkNameProblem(name) {
  if (!name) return "Letters, digits, '_' and '-': prose-guard";
  if (name.length > 64) return "At most 64 characters";
  return HK_NAME_RULE.test(name) ? null : "Letters, digits, '_' and '-', starting with a letter or digit";
}

/* The sheet lives on document.body (bodySheet), so a list or detail
 * repaint behind it (the refresh that lands after it was opened) cannot
 * close it. A close repaints the list: a created hook's row should be
 * there when Done closes the success view. */
function hkNewSheet() {
  const made = !document.getElementById("hk-new-sheet");
  const sheet = bodySheet("hk-new-sheet", "sheet hk-new-sheet", "New hook");
  if (made) {
    sheet.addEventListener("close", () => {
      if (hooksOnRoute() && !new URLSearchParams((location.hash.split("?")[1]) || "").get("hook")
          && hooksPage.data) paintHooks();
    });
  }
  return sheet;
}

/* NewHookSheet: swe hooks new <harness> <name> on the hub, POST
 * /v1/app/hooks/new. The sheet stays open on "Created …" so the stanza
 * to paste is not lost. */
function openNewHook() {
  const sheet = hkNewSheet();
  const d = hooksPage.data || {};
  const names = [...new Set([...(d.harnesses || ["claude"]), "shared"])];
  const picked = names.includes("claude") ? "claude" : names[0] || "shared";
  sheet.innerHTML = `<form class="sheet-body" id="hk-new-form" novalidate>
    <div class="sheet-title">New hook</div>
    <label class="hk-new-field"><span class="hk-new-label">Harness</span>
      <select class="field" id="hk-new-harness">${names.map((h) =>
        `<option value="${esc(h)}"${h === picked ? " selected" : ""}>${esc(h === "shared" ? "Shared" : Fmt.harnessName(h))}</option>`).join("")}</select></label>
    <label class="hk-new-field"><span class="hk-new-label">Name</span>
      <input class="field hk-new-name" id="hk-new-name" type="text" placeholder="prose-guard" autocomplete="off" spellcheck="false" maxlength="80">
      <span class="caption" id="hk-new-hint"></span></label>
    <p class="error" id="hk-new-error" role="alert" hidden></p>
    <div class="sheet-actions" id="hk-new-actions">
      <button class="btn hairline hk-button" type="button" id="hk-new-close">Cancel</button>
      <button class="btn hairline hk-button primary" type="submit" id="hk-new-create" disabled>Create</button>
    </div></form>`;
  const $ = (id) => document.getElementById(id);
  const sync = () => {
    const problem = hkNameProblem($("hk-new-name").value.trim());
    $("hk-new-hint").textContent = problem || "";
    $("hk-new-create").disabled = !!problem || sheet.dataset.working === "1";
    $("hk-new-error").hidden = true;
  };
  $("hk-new-name").oninput = sync;
  $("hk-new-close").onclick = () => sheet.close();
  $("hk-new-form").onsubmit = (e) => { e.preventDefault(); createHook(sheet); };
  // A reply to a form closed and opened again since must not touch this one.
  sheet.dataset.token = String(++hooksPage.newToken);
  sheet.dataset.working = "";
  sync();
  if (!sheet.open) sheet.showModal();
  $("hk-new-name").focus();
}

async function createHook(sheet) {
  const $ = (id) => document.getElementById(id);
  // The success view replaced the fields: an Enter there submits nothing.
  if (!$("hk-new-harness")) return;
  const harness = $("hk-new-harness").value, name = $("hk-new-name").value.trim();
  if (hkNameProblem(name) || sheet.dataset.working === "1") return;
  const token = sheet.dataset.token;
  sheet.dataset.working = "1";
  $("hk-new-create").disabled = true;
  $("hk-new-create").textContent = "Creating…";
  const { status, data } = await apiPost("/v1/app/hooks/new", { harness, name });
  const current = sheet.dataset.token === token && sheet.open && !!document.getElementById("hk-new-form");
  if (current) sheet.dataset.working = "";
  if (status === 401) {
    if (current) sheet.close();
    if (hooksOnRoute()) renderGate();
    return;
  }
  // The script exists whatever happened to the form: show it in the list.
  // A list read still in flight began before the write: drop it, as
  // hooksRun does, or it would put the old list back.
  if (status === 200 && data) { hooksPage.seq++; hooksPage.data = data; }
  if (!current) {
    // The bump dropped any read out, a hook's detail included: read it
    // again there (as hooksRun does), else paint the list just stored.
    if (status === 200 && hooksOnRoute()) {
      if (new URLSearchParams((location.hash.split("?")[1]) || "").get("hook")) renderHooks();
      else paintHooks();
    }
    return;
  }
  if (status !== 200 || !data) {
    $("hk-new-create").textContent = "Create";
    $("hk-new-create").disabled = false;
    const err = $("hk-new-error");
    err.textContent = failText(status, data);
    err.hidden = false;
    return;
  }
  hkCreated(sheet, data.created || {});
}

/* The sheet's success face: the path, the stanza to paste and where,
 * then "swe init links it into place". */
function hkCreated(sheet, c) {
  const home = (hooksPage.data && hooksPage.data.home) || "";
  let where, block;
  if (c.settings && c.settings.length) {
    where = c.settings.map((f) => hkPath(f, home)).join(" or ");
  } else if (c.commands) {
    const targets = Object.keys(c.commands).filter((k) => c.commands[k]).sort();
    where = targets.length ? `the settings file of ${Fmt.harnessName(targets[0])}` : "a harness's settings file";
  } else {
    where = "the harness's settings file";
  }
  if (c.stanza) {
    block = `<p class="caption">Declare it yourself — swe never writes a settings file. Paste this into ${esc(where)}:</p>
      <div class="hk-stanza command-wrap"><pre class="hk-text">${esc(c.stanza)}</pre>
        <button type="button" class="icon-button command-copy" id="hk-copy" aria-label="Copy the stanza" title="Copy the stanza">${Fmt.copyIcon()}</button></div>`;
  } else if (c.commands && Object.keys(c.commands).length) {
    const lines = Object.keys(c.commands).sort().filter((k) => c.commands[k])
      .map((k) => `${k}: ${c.commands[k]}`).join("\n");
    block = `<p class="caption">Declare it yourself — swe never writes a settings file. Declare the wrapper in each harness's hooks map:</p>
      <pre class="hk-text">${esc(lines)}</pre>`;
  } else {
    block = `<p class="caption">Edit its # events: [...] header — that is what deploys it — then swe hooks sync generates the wrappers.</p>`;
  }
  document.getElementById("hk-new-form").innerHTML = `<div class="sheet-title">New hook</div>
    <p class="caption">Created ${esc(hkPath(c.path, home))}</p>
    ${block}
    <p class="caption">Then swe init links it into place.</p>
    <div class="sheet-actions"><button class="btn hairline hk-button primary" type="button" id="hk-new-done">Done</button></div>`;
  document.getElementById("hk-new-done").onclick = () => sheet.close();
  // copyTick awaits the write before the tick; a failed copy (plain http
  // has no clipboard API) selects the stanza to grab by hand.
  Fmt.copyTick(document.getElementById("hk-copy"), c.stanza, (btn) => {
    btn.title = "Couldn't copy — select it by hand";
    const el = sheet.querySelector(".hk-stanza .hk-text");
    if (el) window.getSelection().selectAllChildren(el);
  });
  document.getElementById("hk-new-done").focus();
}

registerPage("hooks", renderHooks);
