/* Sessions page: the hub's sessions with tokens and spend, filtered,
 * grouped by day. Ports the Mac app's SessionsPage. Loaded before app.js;
 * it uses app.js's helpers (api, esc, icon, pageHeader) only when called.
 * A row opens the session's chat (pages/chat.js, #/sessions?chat=<key>),
 * whose Details button shows the sheet this page used to open.
 *
 * The search box filters the rows' details in the browser and also asks
 * the hub's /v1/search, which matches what was said in each transcript
 * when the hub has a search index. A transcript hit keeps its row and
 * shows the matching snippet under it.
 *
 * A row's "More actions" button (or a right-click on it) offers the
 * iPhone's swipe-left actions: Pin or Unpin, Rename, and Fork where the
 * hub says the harness can. Pinned rows sit in a Pinned group above the
 * days. The hub keeps pins and names (POST .../pin, .../title), so every
 * device agrees.
 */
"use strict";

const WINDOWS = [["today", "Today"], ["7d", "7 days"], ["30d", "30 days"], ["90d", "90 days"],
  ["365d", "365 days"], ["all", "All time"]];
const DAY_FOLD = 10;
const STATUS_BADGE = { active: "Active", approval: "Approval", error: "Error", interrupted: "Interrupted", followup: "Waiting" };
/* The quiver state arrows shipped beside the page (state-<kind>.svg). */
const STATUS_ARROW = { active: "green", approval: "yellow", error: "red", interrupted: "grey", followup: "yellow" };
function statusBadge(status) {
  const title = STATUS_BADGE[status];
  if (!title) return "";
  const arrow = STATUS_ARROW[status];
  const img = arrow ? `<img class="badge-arrow" src="state-${arrow}.svg" alt="" aria-hidden="true">` : "";
  return `<span class="badge status-${status}">${img}${title}</span>`;
}
/* A mirrored session's harness deleted its transcript; swe kept a
 * read-only copy (#1918). One wording wherever the page says so: the
 * row's tag, the details sheet, the menu's Fork and the chat's notice. */
const MIRRORED_HINT = "Its transcript was deleted by the harness. swe kept a read-only copy.";
// Transcript search waits for a pause in typing and a query this long.
const SEARCH_DEBOUNCE_MS = 250;
const SEARCH_MIN_CHARS = 2;
const SEARCH_LIMIT = 200;
// The list redraws this long after the last keystroke in the search box.
const SEARCH_PAINT_MS = 80;

const sessionsPage = {
  data: null, gen: 0,
  query: "", tool: "", window: "90d", project: "", model: "",
  showTrivial: false, openDays: new Set(),
  // The machine chip chosen in the strip ("" lists every machine), and the
  // /v1/machines list the strip draws (null until it answers).
  machine: "", machines: null,
  // /v1/search for `search.query`: key -> { snippet, marked }, whether the
  // hub has an index, and "pending" / "error" while it has no answer.
  search: { query: "", hits: new Map(), indexed: null, state: "" },
  searchTimer: null, searchSeq: 0, searchPaint: null,
  // Where the list was scrolled when a chat opened, restored on the way back.
  scrollY: null,
  // Keys with a control write in flight (their action button reads
  // "Attaching…"/"Detaching…"), and the one-line result an action left.
  acting: new Set(), actionNote: "",
};

const sessionChat = () => globalThis.SessionChat || null;

async function renderSessions(_id, rescan = false) {
  const chat = sessionChat();
  const chatKey = chat ? chat.keyFromHash() : null;
  if (chatKey) {
    sessionsPage.gen++;
    return chat.open(chatKey, { row: sessionRowFor(chatKey), details: () => sessionDetails(chatKey) });
  }
  if (chat) chat.close();
  const panel = document.getElementById("panel");
  const gen = ++sessionsPage.gen;
  if (!sessionsPage.data || rescan) {
    panel.innerHTML = `<div class="page">${sessionsHeader(true)}${sessionsSkeleton()}</div>`;
  } else {
    paintSessions();
    if (sessionsPage.scrollY != null) window.scrollTo(0, sessionsPage.scrollY);
    scheduleTranscriptSearch();
  }
  sessionsPage.scrollY = null;
  const mark = pinLedger.clock;
  // The strip is an extra: its read never holds up the list, and a
  // failed one just hides the strip.
  const machinesRead = api("/v1/machines");
  const { status, data } = await api(`/v1/app/sessions${rescan ? "?rescan=1" : ""}`);
  if (gen !== sessionsPage.gen) return;
  if (status === 401) return renderGate();
  if (status !== 200 || !data) {
    const msg = failText(status, data, "then refresh");
    panel.innerHTML = `<div class="page">${sessionsHeader(false)}<div class="card"><p class="error">${esc(msg)}</p></div></div>`;
    bindSessionsHeader();
    return;
  }
  pinOverlay(data.sessions || [], mark);
  unreadReconcile(data.sessions);
  sessionsPage.data = data;
  paintSessions();
  scheduleTranscriptSearch();
  machinesRead.then((m) => {
    if (gen !== sessionsPage.gen) return;
    sessionsPage.machines = m && m.status === 200 && m.data ? m.data.machines || [] : null;
    if (onSessionsList()) repaintKeepingFocus();
  });
}

function sessionsHeader(busy) {
  const d = sessionsPage.data;
  const updated = d && d.scanned_at ? `<span class="caption">Updated ${Fmt.ago(d.scanned_at)}</span>` : "";
  const where = d && d.machine ? ` on ${d.machine} and its satellites` : "";
  return pageHeader("Sessions", `Sessions${where}, usage and estimated cost.`,
    `${updated}<button class="tab" id="new-session">New session</button>
    <button class="icon-button${busy ? " spinning" : ""}" id="sessions-refresh" aria-label="Refresh" title="Refresh">${icon("rescan")}</button>`);
}

function bindSessionsHeader() {
  const btn = document.getElementById("sessions-refresh");
  if (btn) btn.onclick = () => renderSessions("sessions", true);
  const add = document.getElementById("new-session");
  if (add) add.onclick = () => openNewSession();
}

function windowStart(win, now = Date.now() / 1000) {
  if (win === "today") { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; }
  const days = { "7d": 7, "30d": 30, "90d": 90, "365d": 365 }[win];
  return days ? now - days * 86400 : null;
}

/* The transcript hit for a row, when the answer on hand is for the
 * query in the box now. */
function transcriptHit(key) {
  const s = sessionsPage;
  if (s.search.query !== s.query.trim()) return null;
  return s.search.hits.get(key) || null;
}

/* A snippet with its matched terms in <mark>. The hub wraps them in the
 * private-use pair U+E000/U+E001, which esc() leaves alone. */
function markedSnippet(hit) {
  const text = hit.marked || hit.snippet || "";
  return esc(text).replace(/\uE000/g, "<mark>").replace(/\uE001/g, "</mark>");
}

/* Ask /v1/search for the box's query once typing pauses. Only the newest
 * answer lands; an older one that returns late is dropped. */
function scheduleTranscriptSearch() {
  const s = sessionsPage;
  const q = s.query.trim();
  // The same trimmed query (a trailing space, a page coming back) keeps
  // the request or answer it already has.
  if (s.search.query === q && s.search.state !== "error") return;
  clearTimeout(s.searchTimer);
  const seq = ++s.searchSeq;
  if (q.length < SEARCH_MIN_CHARS) {
    s.search = { query: q, hits: new Map(), indexed: s.search.indexed, state: "" };
    return;
  }
  s.search = { query: q, hits: new Map(), indexed: s.search.indexed, state: "pending" };
  s.searchTimer = setTimeout(async () => {
    const { status, data } = await api(`/v1/search?q=${encodeURIComponent(q)}&limit=${SEARCH_LIMIT}`);
    if (seq !== s.searchSeq) return;
    if (!onSessionsList()) {
      // Left for a chat or another page: forget the request, so the list
      // asks again when it opens.
      s.search = { query: "", hits: new Map(), indexed: s.search.indexed, state: "" };
      return;
    }
    const hits = new Map();
    if (status === 200 && data) {
      for (const r of data.results || []) {
        if (r.source === "index" || r.snippet) hits.set(r.key, { snippet: r.snippet, marked: r.marked_snippet });
      }
    }
    s.search = status === 200 && data
      ? { query: q, hits, indexed: !!data.indexed, state: "" }
      : { query: q, hits, indexed: s.search.indexed, state: "error" };
    repaintKeepingFocus();
  }, SEARCH_DEBOUNCE_MS);
}

function onSessionsList() {
  return !!document.getElementById("session-search");
}

/* The line under the filters while a query is in the box. */
function searchNote(kept) {
  const s = sessionsPage, q = s.query.trim();
  if (q.length < SEARCH_MIN_CHARS || s.search.query !== q) return "";
  if (s.search.state === "pending") return `<p class="caption search-note">Searching what was said…</p>`;
  if (s.search.state === "error") {
    return `<p class="caption search-note">Couldn't reach transcript search, so this matches titles and details only.</p>`;
  }
  if (s.search.indexed === false) {
    return `<p class="caption search-note">Searching titles and details only. Turn on <a href="#/settings?section=experimental">Session search</a> in Settings › Experimental to search what was said.</p>`;
  }
  const said = kept.filter((r) => transcriptHit(r.key)).length;
  if (!said) return "";
  const n = kept.length.toLocaleString("en-US");
  const text = kept.length === 1 ? "1 session matches on what was said."
    : said === kept.length ? `${n} sessions match, all on what was said.`
      : `${n} sessions match, ${said.toLocaleString("en-US")} of them on what was said.`;
  return `<p class="caption search-note">${text}</p>`;
}

/* A row's search text, lower-cased once rather than on every keystroke
 * (#1557). Kept per row object, so a reload builds it again; the title
 * is the one searched field written in place (pin, rename), so an entry
 * built from another title is built again. */
const sessionHaystacks = new WeakMap();
function sessionHaystack(r) {
  const hit = sessionHaystacks.get(r);
  if (hit && hit.title === r.title) return hit.text;
  const text = [r.title, r.cwd, r.project, r.id, r.agent, r.location, r.machine,
    ...(r.usage ? r.usage.models.map((m) => m.model) : [])].join(" ").toLowerCase();
  sessionHaystacks.set(r, { title: r.title, text });
  return text;
}

/* The rows the filters keep, and how many of them are accidental. */
function filteredSessions() {
  const s = sessionsPage;
  const start = windowStart(s.window);
  const q = s.query.trim().toLowerCase();
  const machine = chosenMachine();
  const kept = [];
  let trivial = 0;
  for (const r of s.data.sessions) {
    // A pinned row stays listed whatever the time window says, as the
    // phone's Pinned section has no window at all.
    if (!r.pinned && start != null && r.last_active_at < start) continue;
    if (s.tool && r.harness !== s.tool) continue;
    if (machine && r.machine_id !== machine) continue;
    if (s.project && r.project_key !== s.project) continue;
    if (s.model && !(r.usage && r.usage.models.some((m) => m.model === s.model))) continue;
    if (q && !sessionHaystack(r).includes(q) && !transcriptHit(r.key)) continue;
    if (r.trivial && !r.pinned && !s.showTrivial) { trivial++; continue; }
    kept.push(r);
  }
  return { kept, trivial };
}

/* The strip's chips, and the chosen machine (dropped when no longer
 * listed). The strip is an extra: without machine-strip.js (it failed to
 * load) the list shows no strip and no machine filter. */
const machineStrip = () => (typeof MachineStrip === "undefined" ? null : MachineStrip);
const machineChips = () => {
  const strip = machineStrip();
  return strip ? strip.chips(sessionsPage.machines, sessionsPage.data.sessions) : [];
};
const chosenMachine = () => {
  const strip = machineStrip();
  return strip ? strip.effective(sessionsPage.machine, machineChips()) : "";
};

function paintSessions() {
  const s = sessionsPage, d = s.data;
  const { kept, trivial } = filteredSessions();
  const notes = (d.notes || []).map((n) => `<div class="card"><p class="note">${esc(n)}</p></div>`).join("");
  const actNote = s.actionNote ? `<div class="card"><p class="note">${esc(s.actionNote)}</p></div>` : "";
  const body = !d.sessions.length
    ? `<div class="card"><p class="empty">No sessions listed. Sessions from every harness on the hub machine, and on paired satellites, show here.</p></div>`
    : !kept.length
      ? `<div class="card"><p class="empty">No sessions match these filters.</p>
          <div><button class="tab selected" id="clear-filters">Clear filters</button></div></div>`
      : sessionsTable(kept);
  const footer = trivial || s.showTrivial ? trivialFooter(trivial) : "";
  document.getElementById("panel").innerHTML = `<div class="page">
    ${sessionsHeader(false)}
    <div class="stack">${notes}${actNote}${machineStrip() ? machineStrip().html(machineChips(), chosenMachine()) : ""}${filterBar()}${searchNote(kept)}${summaryStrip(kept)}${body}${footer}</div>
  </div>
  <dialog class="sheet" id="session-sheet"></dialog>`;
  bindSessionsHeader();
  bindSessionControls();
}

function options(list, selected) {
  return list.map(([value, label]) =>
    `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`).join("");
}

function filterBar() {
  const s = sessionsPage, rows = s.data.sessions;
  const tools = [...new Set(rows.map((r) => r.harness))].sort();
  // Projects: counts over every row, largest first; hash-like names fold
  // into one "Other" entry at the end.
  const projects = new Map();
  let other = 0;
  for (const r of rows) {
    if (!r.project_key) continue;
    if (r.project_key === "other") { other++; continue; }
    const p = projects.get(r.project_key) || { name: r.project, n: 0 };
    p.n++;
    projects.set(r.project_key, p);
  }
  const projectList = [...projects.entries()]
    .sort((a, b) => b[1].n - a[1].n || a[1].name.localeCompare(b[1].name, undefined, { sensitivity: "base" }))
    .map(([key, p]) => [key, `${p.name} (${p.n})`]);
  if (other) projectList.push(["other", `Other (${other})`]);
  // Models: tokens inside the chosen window, largest first.
  const start = windowStart(s.window);
  const models = new Map();
  for (const r of rows) {
    if (!r.usage || (start != null && r.last_active_at < start)) continue;
    for (const m of r.usage.models) models.set(m.model, (models.get(m.model) || 0) + m.tokens);
  }
  const modelList = [...models.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([m, t]) => [m, `${m} (${Fmt.tokens(t)})`]);
  // A chosen model stays listed when the window has none of it, so the
  // menu always shows the filter that is on.
  if (s.model && !models.has(s.model)) modelList.unshift([s.model, `${s.model} (none in this window)`]);
  return `<div class="filters">
    <input class="field search" id="session-search" type="search" placeholder="Search sessions"
      value="${esc(s.query)}" autocomplete="off" spellcheck="false">
    <select class="field" id="filter-tool" aria-label="Tool">${options([["", "All tools"],
      ...tools.map((t) => [t, Fmt.harnessName(t)])], s.tool)}</select>
    <select class="field" id="filter-window" aria-label="Time">${options(WINDOWS, s.window)}</select>
    <select class="field" id="filter-project" aria-label="Project">${options([["", "All projects"], ...projectList], s.project)}</select>
    <select class="field" id="filter-model" aria-label="Model">${options([["", "All models"], ...modelList], s.model)}</select>
  </div>`;
}

function summaryStrip(rows) {
  let tokens = 0, cost = 0, unpriced = 0;
  for (const r of rows) {
    if (!r.usage) continue;
    tokens += r.usage.tokens; cost += r.usage.cost_usd; unpriced += r.usage.unpriced_tokens;
  }
  const note = unpriced ? ` <span class="caption" title="Plus ${Fmt.tokens(unpriced)} tokens with no list price">ⓘ</span>` : "";
  return `<div class="card summary num">
    <div><div class="caption">Matching sessions</div><div class="figure">${rows.length.toLocaleString("en-US")}</div></div>
    <div><div class="caption">Tokens</div><div class="figure">${Fmt.tokens(tokens)}</div></div>
    <div><div class="caption">Spend</div><div class="figure spend">${Fmt.dollars(cost)}${note}</div></div>
  </div>`;
}

function localDay(epoch) {
  const d = new Date(epoch * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/* The phone's groupsWithPins: pinned rows first, in the order they were
 * pinned, and out of their day groups. */
function splitPinned(rows) {
  const pinned = rows.filter((r) => r.pinned).sort((a, b) =>
    (a.pinned_at ?? Infinity) - (b.pinned_at ?? Infinity)
      || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  return { pinned, rest: rows.filter((r) => !r.pinned) };
}

function sessionsTable(all) {
  const { pinned, rest: rows } = splitPinned(all);
  const days = [];
  for (const r of rows) {
    const day = localDay(r.last_active_at);
    if (!days.length || days[days.length - 1].day !== day) days.push({ day, rows: [] });
    days[days.length - 1].rows.push(r);
  }
  const groups = days.map(({ day, rows: list }) => {
    const open = sessionsPage.openDays.has(day);
    const shown = open ? list : list.slice(0, DAY_FOLD);
    const hidden = list.length - DAY_FOLD;
    const fold = hidden > 0
      ? `<button class="fold" data-day="${day}">${open ? `Show only the first ${DAY_FOLD}` : `Show every session from this day (${hidden} more)`}</button>`
      : "";
    const [y, m, dd] = day.split("-").map(Number);
    const label = new Date(y, m - 1, dd).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
    return `<div class="day"><div class="day-label">${label}</div>${shown.map(sessionRow).join("")}${fold}</div>`;
  }).join("");
  const pins = pinned.length
    ? `<div class="day pinned"><div class="day-label">Pinned</div>${pinned.map(sessionRow).join("")}</div>` : "";
  return `<div class="card sessions">
    <div class="session-head"><span>Session / project</span><span class="num-col">Tokens</span><span class="num-col">Spend</span></div>
    ${pins}${groups}</div>`;
}

function duration(seconds) {
  if (seconds == null || seconds < 60) return "";
  const m = Math.floor(seconds / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

/* The lifecycle action a row offers, the phone's leading-swipe rule:
 * detach an attached one, attach one the hub can take over (a kept pane
 * attaches even mid-turn: it is adopted, nothing restarts). */
function rowAction(r) {
  // Nothing to resume: no Attach, Detach or Kept for a mirrored copy.
  if (r.mirrored) return "";
  if (r.ownership === "attached") return "detach";
  const busy = r.status === "active" || r.status === "approval";
  if ((r.attachable || r.kept) && (r.kept || !busy)) return "attach";
  return "";
}

/* The models a row names: the newest reply's first, as the phone's list
 * shows it (dateless, no provider), then the ledger's other models. The
 * ledger's own spelling of the newest one is left out, so a model never
 * reads twice. */
function rowModels(r) {
  const current = Fmt.modelName(r.model);
  const used = (r.usage ? r.usage.models.map((m) => m.model) : [])
    .filter((m) => !current || Fmt.modelName(m) !== current);
  return (current ? [current, ...used] : used).join(", ");
}

/* A table whose rows carry the real .session-row grid, so the loading
 * card is the same shape as the painted one. */
function sessionsSkeleton() {
  const row = (i) => `<div class="session-row">
    <span class="session-logo">${Sk.bar(16, 16)}</span>
    <span class="session-main"><span class="session-title">${Sk.bar(`${58 - (i % 4) * 10}%`, 13)}</span>
      <span class="session-sub">${Sk.bar(`${36 - (i % 3) * 8}%`, 10)}</span></span>
    <span class="session-acts">${Sk.bar(20, 20)}</span>
    <span class="num-col">${Sk.num(40)}</span>
    <span class="num-col spend">${Sk.num(32)}</span>
  </div>`;
  const day = (n, off) => `<div class="day"><div class="day-label">${Sk.bar(90, 11)}</div>${
    Array.from({ length: n }, (_, i) => row(i + off)).join("")}</div>`;
  return `<div class="card sessions" role="status" aria-label="Loading">
    <div class="session-head"><span>${Sk.bar(110, 11)}</span><span class="num-col">${Sk.num(40, 11)}</span><span class="num-col">${Sk.num(34, 11)}</span></div>
    ${day(5, 0)}${day(4, 5)}</div>`;
}

/* The tag in the status badges' slot. Live-only marks (status, Attached,
 * Kept) are not drawn for a mirrored row: it has no process to describe. */
function mirroredTag() {
  return `<span class="badge mirrored" title="${esc(MIRRORED_HINT)}">Mirrored</span>`;
}

function sessionRow(r) {
  const badge = r.mirrored ? mirroredTag()
    : statusBadge(r.status)
    + (r.ownership === "attached" ? '<span class="badge ownership-attached" title="Runs under SWE: you can send to it">Attached</span>' : "")
    + (r.kept ? '<span class="badge" title="Detached, still running in tmux">Kept</span>' : "");
  const time = new Date(r.last_active_at * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  const hit = sessionsPage.query.trim() ? transcriptHit(r.key) : null;
  const snippet = hit && (hit.marked || hit.snippet)
    ? `<span class="session-snippet">${markedSnippet(hit)}</span>` : "";
  const second = [r.project, Fmt.harnessName(r.harness), r.remote ? r.machine : r.location,
    rowModels(r), time, duration(r.duration_s)]
    .filter(Boolean).map(esc).join(" · ");
  const act = rowAction(r);
  const acting = sessionsPage.acting.has(r.key);
  const actHtml = act
    ? `<button type="button" class="row-act" data-act="${act}" data-key="${esc(r.key)}"${acting ? " disabled" : ""}>${
        acting ? (act === "attach" ? "Attaching…" : "Detaching…") : (act === "attach" ? "Attach" : "Detach")}</button>`
    : "";
  // Not a <button>: the row carries its own Attach/Detach button inside,
  // and a button can't hold one. Click and Enter/Space act the same.
  return `<div class="session-row" role="button" tabindex="0" data-key="${esc(r.key)}">
    <span class="session-logo" title="${esc(Fmt.harnessName(r.harness))}">${Fmt.logo(r.harness)}</span>
    <span class="session-main"><span class="session-title">${unreadShown(r)
      ? '<span class="unread-dot" role="img" aria-label="Unread" title="Unread"></span>' : ""}<span class="ellipsis">${esc(r.title || r.id || "Untitled")}</span>${badge}</span>
      <span class="session-sub">${second}</span>${snippet}</span>
    <span class="session-acts">${actHtml}<button type="button" class="row-more" data-key="${esc(r.key)}"
      aria-label="More actions" aria-haspopup="menu" title="Pin, rename or fork">${MORE_ICON}</button></span>
    <span class="num-col num">${r.usage ? Fmt.tokens(r.usage.tokens) : "–"}</span>
    <span class="num-col num spend">${r.usage ? Fmt.dollars(r.usage.cost_usd) : "–"}</span>
  </div>`;
}

function trivialFooter(count) {
  return sessionsPage.showTrivial
    ? `<p class="caption footer">Showing short or empty sessions. <button class="link" id="toggle-trivial">Hide</button></p>`
    : `<p class="caption footer">${count} short or empty session${count === 1 ? "" : "s"} hidden. <button class="link" id="toggle-trivial">Show</button></p>`;
}

/* The shell line that resumes a session where it ran. */
function resumeLine(r) {
  if (!r.resume || !r.resume.argv) return "";
  const quote = Fmt.shellWord;
  const cmd = r.resume.argv.map(quote).join(" ");
  const cwd = r.resume.cwd || r.cwd;
  return cwd ? `cd ${quote(cwd)} && ${cmd}` : cmd;
}

function openSheet(r) {
  const sheet = document.getElementById("session-sheet");
  const u = r.usage;
  const field = (label, value) => (value ? `<div class="field-row"><span class="caption">${label}</span><span>${value}</span></div>` : "");
  const lastActive = new Date(r.last_active_at * 1000).toLocaleString("en-US",
    { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
  const figures = u ? `<div class="chips five num">
      ${[["Input", u.input], ["Cache read", u.cache_read], ["Cache write", u.cache_write], ["Output", u.output + u.reasoning]]
        .map(([l, v]) => `<div class="chip"><div class="value">${Fmt.tokens(v)}</div><div class="label">${l}</div></div>`).join("")}
      <div class="chip"><div class="value spend">${Fmt.dollars(u.cost_usd)}</div><div class="label">Spend</div></div>
    </div>${u.unpriced_tokens ? `<p class="caption">${Fmt.tokens(u.unpriced_tokens)} tokens from models with no list price.</p>` : ""}`
    : `<p class="caption">${r.remote ? "Usage is read on the hub machine only; this session ran on " + esc(r.machine) + "."
      : "No token usage recorded for this session in the ledger."}</p>`;
  const line = resumeLine(r);
  // The supervised channel: the attach mode, and the swe-… pane name for
  // tmux runs (a kept one stays alive detached until adopted or reaped).
  const supervised = (r.ownership === "attached" || r.kept) && (r.mode || r.tmux);
  const tmuxLine = r.tmux ? `tmux attach -t ${Fmt.shellWord(r.tmux)}` : "";
  const canOpenTerminal = !!(tmuxLine && !r.remote &&
    sessionsPage.data && sessionsPage.data.open_terminal);
  sheet.innerHTML = `<form method="dialog" class="sheet-body">
    <div class="sheet-title">${Fmt.logo(r.harness)}<span>${esc(r.title || r.id || "Untitled")}</span>
      ${r.mirrored ? mirroredTag() : statusBadge(r.status)}</div>
    ${r.mirrored ? `<p class="caption mirrored-note">${esc(MIRRORED_HINT)}</p>` : ""}
    <div class="fields">
      ${field("Harness", esc(Fmt.harnessName(r.harness)))}
      ${field("Machine", esc(r.machine || ""))}
      ${field("Location", esc(r.location))}
      ${field("Folder", esc(r.cwd || "Unknown"))}
      ${field("Last active", esc(lastActive))}
      ${field("Session id", `<code>${esc(r.id || "")}</code><button type="button" class="icon-button copy-inline" id="copy-id" aria-label="Copy session ID" title="Copy session ID">${Fmt.copyIcon()}</button>`)}
      ${u ? field("Models", u.models.map((m) => esc(m.model)).join(", ")) : ""}
      ${supervised ? field("Supervised",
        `${esc(r.mode || "tmux")}${r.tmux ? ` · <code>${esc(r.tmux)}</code>` : ""}${r.kept ? " (kept)" : ""}`) : ""}
      ${field("Ran for", esc(duration(r.duration_s)))}
    </div>
    ${figures}
    ${line ? `<div class="caption">Resume</div><div class="command-wrap"><pre class="command" id="resume-line">${esc(line)}</pre>
      <button type="button" class="icon-button command-copy" id="copy-resume" aria-label="Copy resume command" title="Copy resume command">${Fmt.copyIcon()}</button></div>` : ""}
    ${tmuxLine ? `<div class="caption">Attach from a terminal</div><div class="command-wrap"><pre class="command" id="tmux-line">${esc(tmuxLine)}</pre>
      <button type="button" class="icon-button command-copy" id="copy-tmux" aria-label="Copy attach command" title="Copy attach command">${Fmt.copyIcon()}</button></div>` : ""}
    <div class="sheet-actions">
      ${(() => { const act = rowAction(r); return act
        ? `<button type="button" class="tab" id="sheet-act">${act === "attach" ? "Attach" : "Detach"}</button>` : ""; })()}
      ${canOpenTerminal ? '<button type="button" class="tab" id="run-terminal">Run in terminal</button>' : ""}
      <button class="tab selected" value="close">Close</button>
    </div>
  </form>`;
  // Plain http has no clipboard API: Fmt.copy's fallback still fails on
  // some browsers, so a failed copy selects the text to grab by hand.
  const byHand = (sel) => (btn) => {
    btn.title = "Couldn't copy — select it by hand";
    const el = sel ? sheet.querySelector(sel)
      : btn.parentElement && btn.parentElement.querySelector("code");
    if (el) window.getSelection().selectAllChildren(el);
  };
  Fmt.copyTick(sheet.querySelector("#copy-id"), () => r.id || "", byHand(null));
  Fmt.copyTick(sheet.querySelector("#copy-resume"), () => line, byHand("#resume-line"));
  Fmt.copyTick(sheet.querySelector("#copy-tmux"), () => tmuxLine, byHand("#tmux-line"));
  const run = sheet.querySelector("#run-terminal");
  if (run) {
    run.onclick = async () => {
      run.disabled = true;
      const { status, data } = await apiPost("/v1/app/sessions/open_terminal", { key: r.key });
      run.disabled = false;
      if (status === 200) {
        run.textContent = "Opened";
        setTimeout(() => { if (document.contains(run)) run.textContent = "Run in terminal"; }, 2000);
      } else {
        run.textContent = "Couldn't open";
        run.title = failText(status, data, null);
      }
    };
  }
  const act = sheet.querySelector("#sheet-act");
  if (act) {
    act.onclick = (e) => {
      e.preventDefault();
      sheet.close();
      if (rowAction(r) === "attach") attachRow(r); else detachClick(r);
    };
  }
  sheet.showModal();
}

/* --------------------------------------------------------- lifecycle */

/* The same session control routes the chat uses, one hop per write. */
function sessionControl(key, op, mode) {
  const body = mode ? { op, mode } : { op };
  return apiPost(`/v1/sessions/${encodeURIComponent(key)}/control`, body);
}

function controlErrorText(status, data, fallback) {
  if (data && data.error && data.error.message) return data.error.message;
  return status ? `The hub answered ${status}.` : fallback;
}

/* The action can start from a chat's details sheet, and a reply can
 * land after the list was left: only the visible list is repainted —
 * anything else would overwrite the open view (a chat draft included).
 * #session-search exists only in the list's own markup. */

/* A finished write leaves the row's ownership stale until the next list:
 * refetch rather than patch it, so badges and actions always agree. */
async function runControl(r, op, mode, doneNote) {
  const s = sessionsPage;
  if (s.acting.has(r.key)) return;
  s.acting.add(r.key);
  if (onSessionsList()) paintSessions();
  const { status, data } = await sessionControl(r.key, op, mode);
  s.acting.delete(r.key);
  if (status === 401) {
    if (onSessionsList()) {
      return renderGate();
    }
    return;
  }
  if (status === 200 || status === 202) {
    s.actionNote = doneNote || "";
    if (onSessionsList()) renderSessions("sessions");
    return;
  }
  s.actionNote = controlErrorText(status, data,
    `Couldn't ${op === "attach" ? "attach it" : "detach it"}. Check the connection, then try again.`);
  if (onSessionsList()) paintSessions();
}

function attachRow(r) {
  const where = r.remote && r.machine ? ` on ${r.machine}` : "";
  return runControl(r, "attach", null, `Attaching${where}…`);
}

function detachRow(r, mode) {
  const where = r.remote && r.machine ? ` on ${r.machine}` : "";
  const note = mode === "keep" ? `Detached — it keeps running in tmux${where}.`
    : mode === "when_idle" ? "It detaches when the turn ends." : "";
  return runControl(r, "detach", mode, note);
}

/* The chat's same three-way choice, stacked like the phone's: idle rows
 * just detach when_idle, busy ones ask first since `now` cuts the turn. */
function detachClick(r) {
  const busy = r.status === "active" || r.status === "approval";
  if (!busy) return detachRow(r, "when_idle");
  const sheet = detachSheetEl();
  const canKeep = r.tmux || r.mode === "tmux";
  sheet.innerHTML = `<div class="sheet-body">
    <div class="sheet-title">${Fmt.logo(r.harness)}<span>Detach ${esc(r.title || r.id || "session")}?</span></div>
    <p class="note">It's working right now. Detaching now stops the turn; keeping it lets you attach to it from a terminal with tmux.</p>
    <div class="sheet-actions detach-choices">
      <button type="button" class="tab" data-mode="when_idle">Detach when it finishes</button>
      ${canKeep ? '<button type="button" class="tab" data-mode="keep">Keep it running in tmux</button>' : ""}
      <button type="button" class="tab detach-danger" data-mode="now">Detach now</button>
      <button type="button" class="tab" data-mode="">Cancel</button>
    </div></div>`;
  for (const b of sheet.querySelectorAll("[data-mode]")) {
    b.onclick = () => {
      const mode = b.dataset.mode;
      sheet.close();
      if (mode) detachRow(r, mode);
    };
  }
  if (!sheet.open) sheet.showModal();
}

/* A route sheet: a navigation closes it, so it never sits over the next
 * page. Its choices act only on a click, so closing needs no cleanup. */
function detachSheetEl() {
  return bodySheet("detach-sheet", "sheet", "Detach session");
}

function bindSessionControls() {
  const s = sessionsPage;
  const search = document.getElementById("session-search");
  if (search) {
    // The list redraws once typing pauses, not on every keystroke (#1557).
    search.oninput = () => {
      s.query = search.value;
      scheduleTranscriptSearch();
      clearTimeout(s.searchPaint);
      s.searchPaint = setTimeout(() => {
        s.searchPaint = null;
        // Left the list (a chat opened, another page) meanwhile.
        if (s.data && document.getElementById("session-search")) repaintKeepingFocus();
      }, SEARCH_PAINT_MS);
    };
  }
  for (const [id, key] of [["filter-tool", "tool"], ["filter-window", "window"],
    ["filter-project", "project"], ["filter-model", "model"]]) {
    const el = document.getElementById(id);
    if (el) el.onchange = () => { s[key] = el.value; paintSessions(); };
  }
  for (const btn of document.querySelectorAll(".machine-chip[data-machine]")) {
    btn.onclick = () => {
      const id = btn.dataset.machine;
      const left = btn.parentElement.scrollLeft;
      s.machine = machineStrip().toggled(chosenMachine(), id);
      paintSessions();
      // The repaint replaces the strip: keep its scroll and the focus.
      const strip = document.querySelector(".machine-strip");
      if (strip) strip.scrollLeft = left;
      const again = [...document.querySelectorAll(".machine-chip[data-machine]")].find((b) => b.dataset.machine === id);
      if (again) again.focus({ preventScroll: true });
    };
  }
  const clear = document.getElementById("clear-filters");
  if (clear) clear.onclick = () => {
    Object.assign(s, { query: "", tool: "", project: "", model: "", machine: "", window: "all" });
    scheduleTranscriptSearch();
    paintSessions();
  };
  const toggle = document.getElementById("toggle-trivial");
  if (toggle) toggle.onclick = () => { s.showTrivial = !s.showTrivial; paintSessions(); };
  for (const btn of document.querySelectorAll(".fold[data-day]")) {
    btn.onclick = () => {
      const day = btn.dataset.day;
      if (s.openDays.has(day)) s.openDays.delete(day); else s.openDays.add(day);
      paintSessions();
    };
  }
  const byKey = new Map(s.data.sessions.map((r) => [r.key, r]));
  for (const row of document.querySelectorAll(".session-row")) {
    const open = () => {
      const r = byKey.get(row.dataset.key);
      if (!r) return;
      // Without the chat script (it failed to load), a row keeps its sheet.
      if (!sessionChat()) return openSheet(r);
      s.scrollY = window.scrollY;
      location.hash = `#/sessions?chat=${encodeURIComponent(r.key)}`;
    };
    row.onclick = open;
    // A right-click is the phone's long press: the row's menu.
    row.oncontextmenu = (e) => {
      const r = byKey.get(row.dataset.key);
      if (!r) return;
      e.preventDefault();
      openSessionMenu(r, row, { x: e.clientX, y: e.clientY });
    };
    row.onkeydown = (e) => {
      if (e.target !== row || (e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault();
      open();
    };
  }
  for (const btn of document.querySelectorAll(".row-act")) {
    btn.onclick = (e) => {
      e.stopPropagation();
      const r = byKey.get(btn.dataset.key);
      if (!r) return;
      if (btn.dataset.act === "attach") attachRow(r); else detachClick(r);
    };
  }
  for (const btn of document.querySelectorAll(".row-more")) {
    btn.onclick = (e) => {
      e.stopPropagation();
      const r = byKey.get(btn.dataset.key);
      if (r) openSessionMenu(r, btn);
    };
  }
}

/* Repaint without losing the search box's focus or selection. */
function repaintKeepingFocus() {
  const box = document.getElementById("session-search");
  const focused = box && document.activeElement === box;
  const sel = box ? [box.selectionStart, box.selectionEnd, box.selectionDirection] : null;
  paintSessions();
  const again = document.getElementById("session-search");
  if (again && focused) {
    again.focus();
    if (sel && sel[0] != null) again.setSelectionRange(sel[0], sel[1], sel[2] || "none");
  }
}

function sessionRowFor(key) {
  return (sessionsPage.data && sessionsPage.data.sessions.find((r) => r.key === key)) || null;
}

/* The chat's Details button: the row's sheet, read from the list's data
 * (fetched now when the chat was opened from a link). */
async function sessionDetails(key) {
  let r = sessionRowFor(key);
  if (!r) {
    const mark = pinLedger.clock;
    const { status, data } = await api("/v1/app/sessions");
    if (status === 200 && data) {
      pinOverlay(data.sessions || [], mark);
      sessionsPage.data = data;
      r = sessionRowFor(key);
    }
  }
  // The answer may land after the chat was left for another page.
  const chat = sessionChat();
  if (!r || (chat && chat.key !== key) || !document.getElementById("session-sheet")) return false;
  // The open chat's row is freshest on supervised fields (attach/detach
  // land there over SSE long before the list's data is refetched).
  if (chat && chat.row) {
    r = Object.assign({}, r, {
      ownership: chat.row.ownership, mode: chat.row.mode,
      tmux: chat.row.tmux, kept: chat.row.kept, status: chat.row.status,
    });
  }
  openSheet(r);
  return true;
}

/* -------------------------------------------- pin, rename and fork */

/* The iPhone's SessionAction.trailing: the row menu's actions. Fork only
 * where the hub says the harness's CLI can fork this session. */
function rowMenuActions(r) {
  const out = [r.pinned ? "unpin" : "pin", "rename"];
  if (r.can_fork === true && !r.mirrored) out.push("fork");
  return out;
}
const MENU_LABEL = { pin: "Pin", unpin: "Unpin", rename: "Rename", fork: "Fork" };
const MORE_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg>';

/* The iPhone's RenameRules, matching sessionmeta.parse_title on the hub:
 * one line, spaces collapsed, at most 120 characters. The hub counts
 * code points, and so does [...text]. */
const TITLE_MAX = 120;
function titleLength(text) { return [...text].length; }
function clampTitle(text) {
  const oneLine = text.replace(/[\r\n]/g, " ");
  const points = [...oneLine];
  return points.length > TITLE_MAX ? points.slice(0, TITLE_MAX).join("") : oneLine;
}
function normalTitle(text) {
  return [...text.split(/\s+/).filter(Boolean).join(" ")].slice(0, TITLE_MAX).join("").trimEnd();
}
/* Save is on once the name is non-empty and differs from the current
 * one; clearing it goes through "Use the original name". */
function canSaveTitle(text, current) {
  const name = normalTitle(text);
  return !!name && name !== (current || "");
}
/* A name set on the hub replaces the harness's title. */
function hasCustomTitle(r) {
  return r.harness_title != null && !!r.title && r.title !== r.harness_title;
}

/* The iPhone's PinLedger: pins chosen here that the hub may not reflect
 * yet. Each choice gets a version; a reply counts only for the latest
 * choice on its key, and a list fetched before the latest choice was
 * answered keeps the choice instead of the hub's older answer. */
const pinLedger = { clock: 0, choices: new Map() };

/* `base` is the row's state the hub last confirmed, which a refusal of
 * this choice goes back to; a choice made over one still unanswered
 * keeps that one's base, not its optimistic state. */
function pinChoose(key, pinned, now, base) {
  const prior = pinLedger.choices.get(key);
  pinLedger.clock += 1;
  pinLedger.choices.set(key, { pinned, pinnedAt: pinned ? now : null, version: pinLedger.clock, answeredAt: null,
    base: prior && prior.answeredAt === null ? prior.base : base });
  return pinLedger.clock;
}
/* The hub took `version`: false when a newer choice replaced it. */
function pinAnswered(key, version, meta) {
  const c = pinLedger.choices.get(key);
  if (c && c.version !== version && c.answeredAt === null && meta) {
    // An older choice reached the hub: that is the state to go back to
    // if the newer one is refused.
    c.base = { pinned: !!meta.pinned, pinned_at: meta.pinned ? meta.pinned_at ?? null : null };
  }
  if (!c || c.version !== version) return false;
  pinLedger.clock += 1;
  c.answeredAt = pinLedger.clock;
  return true;
}
/* The hub refused `version`: the state to put the row back to when it
 * was still the latest choice, else null. */
function pinRefused(key, version) {
  const c = pinLedger.choices.get(key);
  if (!c || c.version !== version) return null;
  pinLedger.choices.delete(key);
  return c.base;
}
/* Rows fetched from `mark` (the clock when the fetch started) keep each
 * choice not yet answered by then; choices the fetch reflects go. */
function pinOverlay(rows, mark) {
  for (const [key, c] of [...pinLedger.choices]) {
    if (c.answeredAt !== null && c.answeredAt <= mark) { pinLedger.choices.delete(key); continue; }
    const r = rows.find((x) => x.key === key);
    if (!r) continue;
    r.pinned = c.pinned;
    r.pinned_at = c.pinned ? (c.pinnedAt ?? r.pinned_at) : null;
  }
  return rows;
}

/* A pin or rename the hub answered (`sessionmeta.fields`), with the pin
 * a still-unanswered choice holds, onto the list's row and the open
 * chat's header. */
function patchMeta(key, meta) {
  const c = pinLedger.choices.get(key);
  const held = c && c.answeredAt === null;
  const pinned = held ? c.pinned : !!meta.pinned;
  const fields = {
    title: meta.title || meta.harness_title || "",
    harness_title: meta.harness_title || "",
    pinned,
    pinned_at: pinned ? (held ? c.pinnedAt : meta.pinned_at ?? null) : null,
  };
  const r = sessionRowFor(key);
  if (r) Object.assign(r, fields);
  const chat = sessionChat();
  if (chat && chat.key === key && chat.applyMeta) chat.applyMeta(fields);
}

function repaintList() {
  if (onSessionsList()) paintSessions();
}

/* Unread dots (#1439). The hub flags each row `unread` for this browser
 * (its `tailnet:<node>` or paired-device cursor) and moves the cursor on
 * POST /v1/sessions/{key}/read. The chat posts when it opens, again at
 * most every READ_EVERY_MS while messages arrive, and once more when it
 * closes, which covers whatever it showed. A hidden tab posts nothing:
 * the hub's cursor is the time of the post, so a read sent while hidden
 * would cover replies nobody saw. A dot clears at once through a local mark
 * (key -> the newest hub time it covers); the next list read drops marks
 * the hub has caught up with. The iPhone and Mac apps follow the same
 * rules. */
const READ_EVERY_MS = 3000;
const unread = {
  openKey: null, marks: new Map(), timer: null, due: null,
  // The newest message stamp the open chat showed with the tab visible.
  shown: 0,
  // Swapped by tests.
  visible: () => typeof document === "undefined" || document.visibilityState !== "hidden",
  setTimer: (fn, ms) => setTimeout(fn, ms), clearTimer: (t) => clearTimeout(t),
};

function unreadShown(r) {
  if (!r || !r.unread || r.key === unread.openKey) return false;
  const m = unread.marks.get(r.key);
  return !(m != null && m >= (Number(r.last_active_at) || 0));
}

/* A mark never moves back. Its times are the hub's (the row's
 * `last_active_at`, the read's answered `at`) or a shown message's own
 * stamp, from the machine whose transcript sets `last_active_at`; never
 * this browser's clock, so a clock running ahead can't hide a reply
 * that came after. */
function unreadMark(key, at) {
  const next = Math.max(Number(at) || 0, unread.marks.get(key) || 0);
  unread.marks.set(key, next);
}

/* After a list read: marks whose row is gone, read on the hub, or moved
 * past the mark have nothing left to hide. */
function unreadReconcile(rows) {
  const byKey = new Map((rows || []).map((r) => [r.key, r]));
  for (const [key, m] of [...unread.marks]) {
    const r = byKey.get(key);
    if (!r || !r.unread || (Number(r.last_active_at) || 0) > m) unread.marks.delete(key);
  }
  return rows;
}

async function unreadPost(key) {
  let res;
  try {
    res = await apiPost(`/v1/sessions/${encodeURIComponent(key)}/read`, {});
  } catch {
    return;  // offline or an older hub: the dot stays cleared locally
  }
  if (res && res.status === 200 && res.data && typeof res.data.at === "number") unreadMark(key, res.data.at);
}

function unreadCancel() {
  if (unread.timer != null) unread.clearTimer(unread.timer);
  unread.timer = null;
  unread.due = null;
}

const SessionUnread = {
  opened(key, activity) {
    if (unread.openKey && unread.openKey !== key) SessionUnread.closed(unread.openKey);
    unread.openKey = key;
    unread.shown = 0;
    unreadMark(key, activity);
    unreadCancel();
    return unreadPost(key);
  },
  /* The open chat learned its row (a chat opened from a link has none
   * before): the mark covers it even if the opening post failed. */
  seen(key, activity) {
    if (key === unread.openKey) unreadMark(key, activity);
  },
  /* A live message in the open chat: one post per READ_EVERY_MS at most,
   * so a burst (or a reply that keeps streaming) costs a few requests. */
  activity(key, at) {
    if (key !== unread.openKey) return;
    unread.shown = Math.max(unread.shown, Number(at) || 0);
    if (unread.timer != null) return;
    unread.due = key;
    unread.timer = unread.setTimer(() => {
      unread.timer = null;
      // Hidden since: the read waits for the tab to show (chat.js asks
      // again then).
      if (!unread.visible() || unread.due !== key) return;
      unread.due = null;
      unreadPost(key);
    }, READ_EVERY_MS);
  },
  closed(key) {
    if (!key || key !== unread.openKey) return Promise.resolve();
    unread.openKey = null;
    unreadCancel();
    // What it showed stays read here even if the closing post fails or
    // lands after the next list read.
    unreadMark(key, unread.shown);
    unread.shown = 0;
    return unread.visible() ? unreadPost(key) : Promise.resolve();
  },
};
globalThis.SessionUnread = SessionUnread;

/* Pins or unpins at once, then tells the hub; a refusal puts the row
 * back with a note above the filters. */
async function setPinned(row, pinned) {
  const key = row.key;
  const r = sessionRowFor(key) || row;
  const before = { pinned: !!r.pinned, pinned_at: r.pinned_at ?? null };
  const version = pinChoose(key, pinned, Date.now() / 1000, before);
  patchMeta(key, { title: r.title, harness_title: r.harness_title, pinned, pinned_at: pinLedger.choices.get(key).pinnedAt });
  repaintList();
  const { status, data } = await apiPost(`/v1/sessions/${encodeURIComponent(key)}/pin`, { pinned });
  if (status === 200 && data) {
    if (pinAnswered(key, version, data)) { patchMeta(key, data); repaintList(); }
    return;
  }
  const base = pinRefused(key, version);
  if (!base) return;
  patchMeta(key, { title: r.title, harness_title: r.harness_title, ...base });
  if (status === 401) {
    if (onSessionsList()) renderGate();
    return;
  }
  sessionsPage.actionNote = `${pinned ? "Couldn't pin it" : "Couldn't unpin it"}. ${
    controlErrorText(status, data, "Check the connection, then try again.")}`;
  repaintList();
}

/* ------------------------------------------------------- row menu */

// The open menu's close function and the button it hangs from.
let rowMenuClose = null, rowMenuOwner = null;

function closeSessionMenu() {
  if (rowMenuClose) rowMenuClose();
}

/* The row's menu, under its More button or at the pointer for a
 * right-click. Escape, a click elsewhere, a scroll or leaving the page
 * closes it; its button clicked again closes it too. */
function openSessionMenu(r, anchor, point) {
  const again = !point && rowMenuOwner === anchor;
  closeSessionMenu();
  if (again) return;
  const menu = document.createElement("div");
  menu.className = "row-menu";
  menu.setAttribute("role", "menu");
  menu.innerHTML = rowMenuActions(r).map((a) =>
    `<button type="button" class="row-menu-item" role="menuitem" data-act="${a}">${MENU_LABEL[a]}</button>`).join("")
    // Fork stays visible but off for a mirrored copy, with the reason.
    + (r.mirrored ? `<button type="button" class="row-menu-item" role="menuitem" data-act="fork" aria-disabled="true"
        title="${esc(MIRRORED_HINT)}">Fork</button>` : "");
  document.body.appendChild(menu);
  const box = point ? { left: point.x, right: point.x, bottom: point.y, top: point.y } : anchor.getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  const left = point ? box.left : box.right - w;
  const below = box.bottom + 4;
  menu.style.left = `${Math.max(8, Math.min(left, window.innerWidth - w - 8))}px`;
  menu.style.top = `${below + h > window.innerHeight - 8 ? Math.max(8, box.top - h - 4) : below}px`;
  const items = [...menu.querySelectorAll(".row-menu-item")];
  // The button's own press is left to its click, which toggles.
  const owner = point ? null : anchor;
  const outside = (e) => {
    if (!menu.contains(e.target) && !(owner && owner.contains(e.target))) close();
  };
  const close = (refocus) => {
    rowMenuClose = null;
    rowMenuOwner = null;
    if (owner) owner.setAttribute("aria-expanded", "false");
    menu.remove();
    document.removeEventListener("pointerdown", outside, true);
    window.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", onScroll);
    window.removeEventListener("hashchange", onScroll);
    if (refocus && anchor && document.contains(anchor)) anchor.focus();
  };
  const onScroll = () => close();
  rowMenuClose = close;
  rowMenuOwner = owner;
  if (owner) owner.setAttribute("aria-expanded", "true");
  for (const item of items) {
    item.onclick = (e) => {
      e.stopPropagation();
      if (item.getAttribute("aria-disabled") === "true") return;
      close();
      runSessionAction(item.dataset.act, r);
    };
  }
  menu.onkeydown = (e) => {
    const i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); close(true); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
    } else if (e.key === "Tab") close();
  };
  document.addEventListener("pointerdown", outside, true);
  window.addEventListener("scroll", onScroll, true);
  window.addEventListener("resize", onScroll);
  window.addEventListener("hashchange", onScroll);
  if (items[0]) items[0].focus();
}

function runSessionAction(action, r) {
  if (action === "pin" || action === "unpin") return setPinned(r, action === "pin");
  if (action === "rename") return openRenameSheet(r);
  if (action === "fork") return r.mirrored ? undefined : openForkSheet(r);
}

/* A route sheet (app.js bodySheet): leaving the page closes it. Any
 * close (Esc, Cancel, navigation) stops the sheet trusting replies, so a
 * fork's polls can't open its chat from another page. */
function actionSheetEl() {
  const sheet = bodySheet("session-action-sheet", "sheet");
  sheet.onclose = () => { actionSheet.seq++; };
  return sheet;
}
const actionSheet = { seq: 0 };

/* --------------------------------------------------------- rename */

/* The iPhone's RenameSheet. The name is kept on the hub, so every paired
 * device and the web app show it. */
function openRenameSheet(r) {
  const key = r.key;
  const current = r.title || "";
  const custom = hasCustomTitle(r);
  const mine = ++actionSheet.seq;
  const sheet = actionSheetEl();
  sheet.innerHTML = `<form class="sheet-body action-sheet" id="rename-form">
    <div class="sheet-title">Rename</div>
    <div class="rename-label"><span class="caption">Name</span><span class="caption" id="rename-count"></span></div>
    <input class="field" id="rename-input" type="text" autocomplete="off" spellcheck="false" aria-label="Name"
      placeholder="${esc(r.harness_title || "Name this session")}" value="${esc(clampTitle(current))}">
    <p class="caption">Every device paired with this hub shows this name.</p>
    ${custom ? `<div><button type="button" class="tab" id="rename-reset">Use the original name</button></div>
      ${r.harness_title ? `<p class="caption">Original: ${esc(r.harness_title)}</p>` : ""}` : ""}
    <p class="error" id="rename-error" hidden></p>
    <div class="sheet-actions">
      <button type="button" class="tab" id="rename-cancel">Cancel</button>
      <button type="submit" class="tab selected" id="rename-save">Save</button>
    </div></form>`;
  const input = sheet.querySelector("#rename-input");
  const count = sheet.querySelector("#rename-count");
  const save = sheet.querySelector("#rename-save");
  const reset = sheet.querySelector("#rename-reset");
  const cancel = sheet.querySelector("#rename-cancel");
  const error = sheet.querySelector("#rename-error");
  const sync = () => {
    count.textContent = `${titleLength(input.value)}/${TITLE_MAX}`;
    save.disabled = !canSaveTitle(input.value, current);
  };
  const submit = async (title) => {
    for (const b of [input, save, reset, cancel]) if (b) b.disabled = true;
    save.textContent = "Saving…";
    error.hidden = true;
    const { status, data } = await apiPost(`/v1/sessions/${encodeURIComponent(key)}/title`, { title });
    if (status === 200 && data) {
      // Saved, even when the sheet was closed meanwhile.
      patchMeta(key, data);
      repaintList();
      if (mine === actionSheet.seq) sheet.close();
      return;
    }
    if (mine !== actionSheet.seq) return;
    if (status === 401) {
      sheet.close();
      if (onSessionsList()) renderGate();
      return;
    }
    for (const b of [input, reset, cancel]) if (b) b.disabled = false;
    save.textContent = "Save";
    sync();
    error.textContent = controlErrorText(status, data, "Couldn't rename it. Check the connection, then try again.");
    error.hidden = false;
  };
  input.oninput = () => {
    const clamped = clampTitle(input.value);
    if (clamped !== input.value) input.value = clamped;
    sync();
  };
  sheet.querySelector("#rename-form").onsubmit = (e) => {
    e.preventDefault();
    if (canSaveTitle(input.value, current)) submit(normalTitle(input.value));
  };
  if (reset) reset.onclick = () => submit("");
  cancel.onclick = () => { actionSheet.seq++; sheet.close(); };
  sync();
  if (!sheet.open) sheet.showModal();
  input.focus();
  input.select();
}

/* ----------------------------------------------------------- fork */

/* Where a launch landed, polling GET /v1/launches/<id> once a second like
 * the phone's LaunchFlow: the hub's `started` (with its key),
 * `untracked` or `failed` view, `lost` when the hub forgot it, `waiting`
 * once the 90 attempts run out; null as soon as `alive()` is false. */
async function followLaunch(first, alive) {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const settled = (l) => (l.state === "started" && l.key) || l.state === "untracked" || l.state === "failed";
  let launch = first;
  for (let i = 0; i < 90; i++) {
    if (settled(launch)) return launch;
    await sleep(1000);
    if (!alive()) return null;
    const { status, data } = await api(`/v1/launches/${encodeURIComponent(first.launch_id)}`);
    if (!alive()) return null;
    if (status === 200 && data) launch = data;
    else if (status === 404) {
      return { state: "lost", note: (data && data.error && data.error.message) || "The hub lost track of this launch." };
    }
    // A dropped poll is retried until the attempts run out.
  }
  return settled(launch) ? launch : { state: "waiting" };
}

/* The note a launch that opened no chat leaves: [text, failed]. */
function launchNote(launch, queued) {
  if (launch.state === "untracked") {
    return ["Started. SWE can't read this harness's sessions yet, so there's no chat to open. It keeps running under tmux on the machine.", false];
  }
  if (launch.state === "failed") return [launch.note ? `The session didn't start: ${launch.note}` : "The session didn't start.", true];
  if (launch.state === "lost") return [launch.note, true];
  return [queued
    ? "Queued. It starts when that machine is online, then shows up in Sessions."
    : "It started but hasn't shown up yet. It'll appear in Sessions when it does.", false];
}

/* The iPhone's ForkSheet: a new session that continues from this one's
 * history, with a first message, tracked like a new-session launch; its
 * chat opens once the hub names it. Cancel stops the polling, not the
 * fork. */
function openForkSheet(r) {
  const key = r.key;
  const mine = ++actionSheet.seq;
  const sheet = actionSheetEl();
  const alive = () => mine === actionSheet.seq && sheet.open;
  const folder = (r.cwd || "").replace(/\/+$/, "").split("/").pop() || r.cwd || "its folder";
  sheet.innerHTML = `<form class="sheet-body action-sheet" id="fork-form">
    <div class="sheet-title">${Fmt.logo(r.harness)}<span class="ellipsis">Fork ${esc(r.title || r.id || "Untitled session")}</span></div>
    <div class="caption">What should the fork do?</div>
    <textarea class="field ns-prompt" id="fork-prompt" rows="4" placeholder="Its first message" aria-label="What should the fork do?"></textarea>
    <p class="caption">The fork starts from this session's history and leaves the original as it is. It needs a first message: the harness writes nothing until it gets one.</p>
    <p class="note" id="fork-note" hidden></p>
    <div class="sheet-actions">
      <button type="button" class="tab" id="fork-cancel">Cancel</button>
      <button type="submit" class="tab selected" id="fork-start" disabled>Start</button>
    </div>
    <p class="caption">Runs under tmux in ${esc(folder)}. The chat opens once the fork shows up.</p></form>`;
  const prompt = sheet.querySelector("#fork-prompt");
  const start = sheet.querySelector("#fork-start");
  const cancel = sheet.querySelector("#fork-cancel");
  const note = sheet.querySelector("#fork-note");
  // done: the fork is running or settled, so Start stays off.
  let busy = false, done = false;
  const phase = (label) => {
    busy = !!label;
    prompt.disabled = busy || done;
    start.disabled = busy || done || !prompt.value.trim();
    start.textContent = label || "Start";
    cancel.textContent = busy || done ? "Close" : "Cancel";
  };
  const stop = ([text, failed]) => {
    note.className = failed ? "error" : "note";
    note.textContent = text;
    note.hidden = false;
    if (!failed) done = true;
    phase(null);
  };
  prompt.oninput = () => { if (!busy) phase(null); };
  cancel.onclick = () => { actionSheet.seq++; sheet.close(); };
  sheet.querySelector("#fork-form").onsubmit = async (e) => {
    e.preventDefault();
    const text = prompt.value.trim();
    if (!text || busy || done) return;
    note.hidden = true;
    phase("Starting…");
    const { status, data } = await apiPost(`/v1/sessions/${encodeURIComponent(key)}/fork`, { prompt: text });
    if (!alive()) return;
    if (status === 401) {
      sheet.close();
      return renderGate();
    }
    if (!((status === 200 || status === 202) && data && data.launch_id)) {
      return stop([controlErrorText(status, data, "Couldn't start the fork. Check the connection, then try again."), true]);
    }
    phase("Waiting for the fork…");
    const launch = await followLaunch(data, alive);
    if (!launch) return;
    if (launch.state === "started") {
      phase("Opening the chat…");
      actionSheet.seq++;
      sheet.close();
      location.hash = `#/sessions?chat=${encodeURIComponent(launch.key)}`;
      return;
    }
    stop(launchNote(launch, data.queued === true));
  };
  phase(null);
  if (!sheet.open) sheet.showModal();
  prompt.focus();
}

/* ------------------------------------------------------- new session */

/* The iPhone app's NewSessionView: machine, harness, folder and a first
 * prompt, then POST /v1/sessions followed by /v1/launches/<id> polls
 * until the hub names the session key and the chat can open. */
const ns = {
  machines: null,            // /v1/machines answer, cached per sheet open
  machineId: null, harness: null, folder: "", prompt: "", query: "",
  harnessTouched: false,     // a picked harness keeps its choice across folder changes
  model: null,               // ModelMenu state, fresh per sheet open; a user's pick survives harness re-defaults
  yolo: true,                // the Autonomy checkbox; on until the user turns it off
  provider: "",              // a provider slug from providers_at_launch; "" is the harness default
  browse: null, browseSeq: 0,  // {path, parent, folders, error, loading}
  phase: "idle", message: "", seq: 0,  // idle|starting|waiting|started|failed|unconfirmed
};

/* A route sheet, so a navigation closes it. However it closes (Close,
 * Esc, Back, a link), a launch still out stops trusting its replies:
 * its poll must not open the new chat over wherever the user went. */
function nsSheetEl() {
  const sheet = bodySheet("new-session-sheet", "sheet", "New session");
  sheet.onclose = () => { ns.seq++; ns.phase = "idle"; ns.message = ""; };
  return sheet;
}

function nsBusy() {
  return ns.phase === "starting" || ns.phase === "waiting" || ns.phase === "started";
}

/* The iPhone's NewSessionOptions: a machine offers `launchable`, falling
 * back to every harness it detected on older hub versions. Machines
 * that can launch nothing are left out, online ones first. */
function nsMachines() {
  return (ns.machines || [])
    .filter((m) => (m.launchable || m.harnesses || []).length)
    .sort((a, b) => ((b.online === true) - (a.online === true))
      || String(a.name || a.id).localeCompare(String(b.name || b.id)));
}

/* What the sheet says when no machine can start a session, from every
 * row the hub listed (NewSessionOptions.noMachineMessage on the Mac). A
 * row with `launchable` answered: nothing is installed there or every
 * harness is switched off, so updating swe would change nothing. Only
 * rows without the field (an older hub), or none at all, mean swe is
 * too old. */
function nsNoMachineNote(rows) {
  if (!rows.some((m) => Array.isArray(m.launchable))) {
    return "No machine can start a session. Update swe on the hub: a machine offers the harnesses "
      + "swe lists there, minus the ones switched off in Provider visibility.";
  }
  return rows.length > 1
    ? "No harness can start on any machine: install one, or turn it on in Settings › Providers."
    : "No harness can start on the hub: install one there, or turn it on in Settings › Providers.";
}

function nsMachine() {
  return nsMachines().find((m) => m.id === ns.machineId) || null;
}

function nsHarnesses() {
  const m = nsMachine();
  return m ? (m.launchable || m.harnesses || []) : [];
}

function nsHarnessName(m, h) {
  const names = (m && m.harness_names) || {};
  const raw = names[h] || h;
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

/* A machine's `model_at_launch` names the harnesses that take a model
 * pick at launch; the key absent means an old satellite that would drop
 * it, so the field hides rather than hint at a pick that can't land. */
function nsModelOk(machine) {
  return !!(machine && ns.harness
    && Array.isArray(machine.model_at_launch)
    && machine.model_at_launch.includes(ns.harness));
}

/* `yolo_at_launch` is the Autonomy row's `model_at_launch`: the key
 * absent means a build that would drop the flag, so the row hides;
 * present, it names the harnesses whose launches take the bypass. */
function nsShowsYolo(machine) {
  return !!(machine && Array.isArray(machine.yolo_at_launch));
}

function nsYoloOk(machine) {
  return nsShowsYolo(machine) && !!ns.harness
    && machine.yolo_at_launch.includes(ns.harness);
}

function nsYoloHtml(machine, busy) {
  if (!nsShowsYolo(machine)) return "";
  const ok = nsYoloOk(machine);
  return `<div class="caption">Autonomy</div>
    <label class="ns-yolo"><input type="checkbox" id="ns-yolo"${ns.yolo && ok ? " checked" : ""}${busy || !ok ? " disabled" : ""}> YOLO mode: run without approval prompts</label>
    ${ns.harness && !ok ? `<p class="caption" style="padding:0 2px">${esc(nsHarnessName(machine, ns.harness))} can't start with approvals bypassed: it runs its usual checks.</p>` : ""}`;
}

/* The Autonomy row repaints alone, like the model row, so a typed
 * folder that flips the harness's yolo support never rebuilds the
 * folder input — and the box can never show off while sending yolo. */
function nsPaintYolo() {
  const slot = document.getElementById("ns-yolo-slot");
  if (!slot) return;
  slot.innerHTML = nsYoloHtml(nsMachine(), nsBusy());
  const box = slot.querySelector("#ns-yolo");
  if (box) box.onchange = () => { ns.yolo = box.checked; };
}

/* `providers_at_launch` lists, per harness, the providers whose key is
 * set on that machine (slugs only). The key absent means an agent that
 * would drop the pick, so the row hides; so does an empty list. */
function nsProviders(machine) {
  const all = machine && machine.providers_at_launch;
  if (!all || typeof all !== "object" || !ns.harness) return [];
  const list = all[ns.harness];
  return Array.isArray(list) ? list : [];
}

function nsProviderPick(machine) {
  return nsProviders(machine).includes(ns.provider) ? ns.provider : "";
}

function nsProviderHtml(machine, busy) {
  const slugs = nsProviders(machine);
  if (!slugs.length) return "";
  const names = (machine && machine.provider_names) || {};
  const pick = nsProviderPick(machine);
  const opts = [`<option value=""${pick ? "" : " selected"}>Harness default</option>`]
    .concat(slugs.map((s) => `<option value="${esc(s)}"${s === pick ? " selected" : ""}>${esc(names[s] || s)}</option>`));
  return `<label class="caption" for="ns-provider">Provider</label>
    <select class="field" id="ns-provider" aria-label="Provider"${busy ? " disabled" : ""}>${opts.join("")}</select>`;
}

/* Repaints alone, like the Autonomy row: a harness re-default from a
 * typed folder redraws only this slot. A pick the new harness doesn't
 * offer falls back to the harness default. */
function nsPaintProvider() {
  const slot = document.getElementById("ns-provider-slot");
  if (!slot) return;
  const machine = nsMachine();
  ns.provider = nsProviderPick(machine);
  slot.innerHTML = nsProviderHtml(machine, nsBusy());
  const menu = slot.querySelector("#ns-provider");
  if (menu) menu.onchange = () => { ns.provider = menu.value; nsRefillModel(); nsPaintModel(); };
}

/* The ledger's recent models for the picked harness on this machine,
 * newest first: the Model menu's entries, and its default while the
 * user hasn't picked. An old hub's row has only `last_models`. */
function nsRecentModels(machine) {
  const m = machine || {};
  return ModelMenu.recents((m.recent_models || {})[ns.harness], (m.last_models || {})[ns.harness]);
}

/* An untouched Model menu reads Harness default while a Provider is
 * picked: the recent models ran on other providers, and an OpenCode
 * `openai/…` model would route past the picked provider's key. */
function nsRefillModel() {
  const machine = nsMachine();
  ModelMenu.redefault(ns.model, nsModelOk(machine) ? nsRecentModels(machine) : [],
    !!nsProviderPick(machine));
}

function nsModelHtml(machine, busy) {
  return nsModelOk(machine) ? `<div class="caption">Model</div>
    ${ModelMenu.html(ns.model, nsRecentModels(machine), "ns-model", busy)}` : "";
}

/* The model row is drawn into its slot alone, by nsPaint and again when
 * a typed folder flips whether the picked harness can take a model or
 * Other… shows or drops its box: the folder input keeps focus. */
function nsPaintModel() {
  const slot = document.getElementById("ns-model-slot");
  if (!slot) return;
  slot.innerHTML = nsModelHtml(nsMachine(), nsBusy());
  ModelMenu.bind(slot, ns.model, nsRecentModels(nsMachine()), "ns-model", nsPaintModel);
}

/* Recent folders on the chosen machine, most recent first, the session
 * rows' cwd deduped; the box's words all have to match, any case. */
function nsFolders(m) {
  const mid = m ? m.id : "";
  const seen = new Set(), out = [];
  for (const r of (sessionsPage.data && sessionsPage.data.sessions) || []) {
    const cwd = r.cwd;
    if (!cwd || !cwd.startsWith("/")) continue;
    const onMachine = mid ? (r.machine_id || "") === mid || (!r.machine_id && !r.remote) : true;
    if (!onMachine || seen.has(cwd)) continue;
    seen.add(cwd);
    out.push(cwd);
  }
  const words = ns.query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return words.length ? out.filter((p) => words.every((w) => p.toLowerCase().includes(w))) : out;
}

function nsReady() {
  return !nsBusy() && ns.machineId != null && ns.harness != null
    && ns.folder.trim().startsWith("/") && !!ns.prompt.trim();
}

function nsPaint() {
  const sheet = nsSheetEl();
  const machines = nsMachines();
  const machine = nsMachine();
  const busy = nsBusy();
  const browse = ns.browse;
  const untracked = machine && ns.harness
    && (machine.untracked_harnesses || []).includes(ns.harness);
  const machineChips = machines.length > 1 ? `<div class="ns-chips">${
    machines.map((m) => `<button type="button" class="tab ns-chip${m.id === ns.machineId ? " selected" : ""}"
      data-machine="${esc(m.id)}"${busy ? " disabled" : ""}>${esc(m.name || m.id)}${m.online === false ? " · offline" : ""}</button>`).join("")
  }</div>` : "";
  const harnessChips = `<div class="ns-chips" id="ns-harness-chips">${nsHarnessChips(machine, busy)}</div>`;
  const folders = nsFolders(machine).slice(0, 6);
  const browseUi = browse ? `<div class="ns-browse">
      <div class="ns-path">${esc(browse.path || "…")}</div>
      ${browse.loading ? `<div role="status" aria-label="Loading">${[60, 44, 52].map((w) =>
        `<div class="ns-folder">${Sk.bar(`${w}%`, 12)}</div>`).join("")}</div>` : ""}
      ${browse.error ? `<p class="error">${esc(browse.error)}</p>` : ""}
      ${browse.parent ? `<button type="button" class="ns-folder" data-path="${esc(browse.parent)}">..</button>` : ""}
      ${(browse.folders || []).map((f) => `<button type="button" class="ns-folder" data-path="${esc(f.path)}">${esc(f.name)}/</button>`).join("")}
      ${!browse.loading && !browse.error ? `<div class="sheet-actions"><button type="button" class="tab selected" id="ns-browse-use" data-path="${esc(browse.path)}">Use this folder</button></div>` : ""}
    </div>` : "";
  const modelUi = '<div id="ns-model-slot"></div>';  // nsPaintModel fills it below
  const note = ns.message ? `<p class="${ns.phase === "failed" ? "error" : "note"}">${esc(ns.message)}</p>` : "";
  const startLabel = { starting: "Starting…", waiting: "Waiting for the session…", started: "Opening the chat…" }[ns.phase] || "Start";
  sheet.innerHTML = `<div class="sheet-body">
    <div class="sheet-title">New session</div>
    ${machineChips}
    ${machines.length && !machine ? '<p class="note">Pick a machine.</p>' : ""}
    ${!machines.length ? `<p class="note">${esc(nsNoMachineNote(ns.machines || []))}</p>` : ""}
    <div class="caption">Harness</div>
    ${harnessChips}
    ${untracked ? `<p class="note">SWE can't read ${esc(nsHarnessName(machine, ns.harness))} sessions yet: it starts, but no chat opens.</p>` : ""}
    ${modelUi}
    <div id="ns-provider-slot"></div>
    <div id="ns-yolo-slot"></div>
    <div class="caption">Folder</div>
    <input class="field ns-path-input" id="ns-folder" type="text" autocomplete="off" spellcheck="false"
      placeholder="/absolute/path" value="${esc(ns.folder)}"${busy ? " disabled" : ""}>
    <input class="field ns-query" id="ns-query" type="search" placeholder="Recent folders on this machine — type to filter"
      value="${esc(ns.query)}"${busy ? " disabled" : ""}>
    <div class="ns-folders" id="ns-folders">
      ${folders.map((p) => `<button type="button" class="ns-folder" data-path="${esc(p)}"${busy ? " disabled" : ""}>${esc(p)}</button>`).join("")
        || '<p class="caption" style="padding:8px 10px">No recent folders match.</p>'}
    </div>
    <button type="button" class="link" id="ns-browse"${busy ? " disabled" : ""}>Browse this machine's folders…</button>
    ${browseUi}
    <div class="caption">First prompt</div>
    <textarea class="field ns-prompt" id="ns-prompt" rows="3" placeholder="Describe the first task"${busy ? " disabled" : ""}>${esc(ns.prompt)}</textarea>
    ${note}
    <div class="sheet-actions">
      <button type="button" class="tab" id="ns-cancel"${ns.phase === "started" ? " disabled" : ""}>${nsBusy() ? "Close" : "Cancel"}</button>
      <button type="button" class="tab selected" id="ns-start"${nsReady() ? "" : " disabled"}>${esc(startLabel)}</button>
    </div></div>`;
  for (const b of sheet.querySelectorAll("[data-machine]")) {
    b.onclick = () => {
      if (b.dataset.machine === ns.machineId) return;
      ns.machineId = b.dataset.machine;
      ns.harness = null;
      ns.harnessTouched = false;
      ns.browse = null;
      nsPickHarness();
      nsRefillModel();
      nsPaint();
    };
  }
  for (const b of sheet.querySelectorAll("[data-harness]")) {
    b.onclick = () => { ns.harness = b.dataset.harness; ns.harnessTouched = true; nsRefillModel(); nsPaint(); };
  }
  for (const b of sheet.querySelectorAll(".ns-folder[data-path]")) {
    b.onclick = () => {
      if (ns.browse) nsBrowse(b.dataset.path);   // inside the browser: descend
      else { nsPickFolder(b.dataset.path); }
    };
  }
  const folderIn = sheet.querySelector("#ns-folder");
  if (folderIn) folderIn.oninput = () => {
    ns.folder = folderIn.value;
    // A typed folder re-defaults the harness too, repainted in place so
    // the field keeps its caret — even when the model row itself
    // appears or drops, only its slot is redrawn.
    if (!ns.harnessTouched) {
      nsPickHarness(); nsPaintHarness();
      nsRefillModel(); nsPaintModel(); nsPaintProvider(); nsPaintYolo();
    }
    nsPaintStart();
  };
  const queryIn = sheet.querySelector("#ns-query");
  if (queryIn) queryIn.oninput = () => { ns.query = queryIn.value; nsPaintLists(); };
  const promptIn = sheet.querySelector("#ns-prompt");
  if (promptIn) promptIn.oninput = () => { ns.prompt = promptIn.value; nsPaintStart(); };
  nsPaintProvider();
  nsPaintYolo();
  nsPaintModel();
  const browseBtn = sheet.querySelector("#ns-browse");
  if (browseBtn) browseBtn.onclick = () => nsBrowse(ns.folder.trim());
  const use = sheet.querySelector("#ns-browse-use");
  if (use) use.onclick = () => { ns.browse = null; nsPickFolder(use.dataset.path || ""); };
  // Closing drops any launch still out (nsSheetEl's onclose).
  sheet.querySelector("#ns-cancel").onclick = () => sheet.close();
  sheet.querySelector("#ns-start").onclick = nsStart;
}

/* Light repaints for keystrokes: only the folder list or the Start
 * button's enabled state, so the focused field keeps its caret. */
function nsPaintStart() {
  const start = document.getElementById("ns-start");
  if (start) start.disabled = !nsReady();
}
function nsPaintLists() {
  const box = document.getElementById("ns-folders");
  if (!box) return nsPaint();
  const busy = nsBusy();
  const folders = nsFolders(nsMachine()).slice(0, 6);
  box.innerHTML = folders.map((p) => `<button type="button" class="ns-folder"
      data-path="${esc(p)}"${busy ? " disabled" : ""}>${esc(p)}</button>`).join("")
    || '<p class="caption" style="padding:8px 10px">No recent folders match.</p>';
  for (const b of box.querySelectorAll(".ns-folder[data-path]")) {
    b.onclick = () => nsPickFolder(b.dataset.path);
  }
}

function nsHarnessChips(machine, busy) {
  return nsHarnesses().map((h) => `<button type="button"
    class="tab ns-chip${h === ns.harness ? " selected" : ""}" data-harness="${esc(h)}"${busy ? " disabled" : ""}>
    ${Fmt.logo(h)}<span>${esc(nsHarnessName(machine, h))}</span></button>`).join("");
}

/* The chips repaint alone when a typed folder re-defaults the harness. */
function nsPaintHarness() {
  const box = document.getElementById("ns-harness-chips");
  if (!box) return;
  box.innerHTML = nsHarnessChips(nsMachine(), nsBusy());
  for (const b of box.querySelectorAll("[data-harness]")) {
    b.onclick = () => { ns.harness = b.dataset.harness; ns.harnessTouched = true; nsRefillModel(); nsPaint(); };
  }
}

/* A folder pick repaints the whole sheet — and re-defaults the harness
 * to the folder's usual one unless the user picked a harness by hand. */
function nsPickFolder(path) {
  ns.folder = path;
  if (!ns.harnessTouched) nsPickHarness();
  nsRefillModel();
  nsPaint();
}

/* The iPhone's defaultHarness: the harness last used in that folder on
 * that machine when it is on offer, else the first offer. */
function nsPickHarness() {
  const offered = nsHarnesses();
  const mid = ns.machineId;
  const last = ((sessionsPage.data && sessionsPage.data.sessions) || [])
    .find((r) => (r.machine_id || "") === mid && r.cwd === ns.folder);
  ns.harness = last && offered.includes(last.harness) ? last.harness : (offered[0] || null);
}

async function nsBrowse(path) {
  ns.browse = ns.browse || {};
  ns.browse.loading = true;
  ns.browse.error = "";
  const mine = ++ns.browseSeq;   // a slower earlier listing must not win
  nsPaint();
  const m = ns.machineId || "";
  // An empty path is the hub machine's home; only absolute ones go over.
  const qp = path && path.startsWith("/") ? `path=${encodeURIComponent(path)}&` : "";
  const { status, data } = await api(`/v1/folders?${qp}machine=${encodeURIComponent(m)}`);
  if (!ns.browse || mine !== ns.browseSeq) return;
  ns.browse.loading = false;
  if (status === 200 && data) {
    ns.browse.path = data.path;
    ns.browse.parent = data.parent || null;
    ns.browse.folders = data.folders || [];
    if (data.truncated) ns.browse.error = "This folder has too many entries to list fully.";
    else ns.browse.error = "";
  } else {
    ns.browse.error = failText(status, data, null);
    if (status === 401) {
      ns.seq++;
      nsSheetEl().close();
      return renderGate();
    }
  }
  nsPaint();
}

async function openNewSession() {
  ns.machines = null;
  ns.machineId = null;
  ns.harness = null;
  ns.harnessTouched = false;
  ns.model = ModelMenu.fresh();
  ns.yolo = true;
  ns.provider = "";
  ns.folder = ns.folder || "";   // the last folder is a good next guess
  ns.prompt = "";
  ns.query = "";
  ns.browse = null;
  ns.browseSeq++;
  ns.phase = "idle";
  ns.message = "";
  ns.seq++;
  const sheet = nsSheetEl();
  nsPaint();
  if (!sheet.open) sheet.showModal();
  const { status, data } = await api("/v1/machines");
  if (status === 401) {
    sheet.close();
    return renderGate();
  }
  if (status !== 200 || !data) {
    ns.message = "Couldn't list the machines — the harness and folder pickers need it.";
    nsPaint();
    return;
  }
  ns.machines = data.machines || [];
  const first = nsMachines()[0];
  if (first && !ns.machineId) ns.machineId = first.id;
  nsPickHarness();
  nsRefillModel();
  nsPaint();
}

async function nsStart() {
  if (!nsReady()) return;
  const mine = ++ns.seq;
  // The sheet's close event (which bumps ns.seq) runs a task later, so
  // a reply landing in between checks the sheet itself too.
  const live = () => mine === ns.seq && nsSheetEl().open;
  ns.phase = "starting";
  ns.message = "";
  nsPaint();
  const body = { harness: ns.harness, cwd: ns.folder.trim(), prompt: ns.prompt.trim() };
  if (ns.machineId) body.machine = ns.machineId;
  const model = ModelMenu.value(ns.model);
  if (nsModelOk(nsMachine()) && model) body.model = model;
  if (nsYoloOk(nsMachine()) && ns.yolo) body.yolo = true;
  const provider = nsProviderPick(nsMachine());
  if (provider) body.provider = provider;
  const { status, data } = await apiPost("/v1/sessions", body);
  if (!live()) return;
  if (status === 401) {
    nsSheetEl().close();
    return renderGate();
  }
  if (!((status === 200 || status === 202) && data && data.launch_id)) {
    ns.phase = "failed";
    ns.message = controlErrorText(status, data, "Couldn't reach the hub. Check the connection, then try again.");
    return nsPaint();
  }
  ns.phase = "waiting";
  nsPaint();
  const launch = await followLaunch(data, live);
  if (!launch) return;
  if (launch.state === "started") {
    ns.phase = "started";
    nsPaint();
    const sheet = nsSheetEl();
    ns.phase = "idle";
    sheet.close();
    // The list refetches behind the chat that is about to open.
    renderSessions("sessions");
    location.hash = `#/sessions?chat=${encodeURIComponent(launch.key)}`;
    return;
  }
  const [message, failed] = launchNote(launch, data.queued === true);
  ns.phase = failed ? "failed" : "unconfirmed";
  ns.message = message;
  nsPaint();
}
