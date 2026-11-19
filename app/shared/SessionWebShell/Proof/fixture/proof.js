// The proof page: a 2,000-row timeline, a composer, a streamed event
// consumer (fetch + ReadableStream, the way the session view reads SSE),
// an EventSource probe, an IndexedDB read and write, a cached snapshot and
// a POST. window.proofState() reports everything for the UI tests.
(() => {
  "use strict";
  const query = new URLSearchParams(location.search);
  const timeline = document.getElementById("timeline");
  const banner = document.getElementById("banner");
  const input = document.getElementById("input");
  const state = {
    origin: location.origin,
    href: location.href,
    loadedAt: Date.now(),
    rows: 0,
    maxWindowScroll: 0,
    maxVisualOffset: 0,
    stream: { state: "idle", reads: 0, arrivals: [] },
    eventSource: { state: "idle", arrivals: [] },
    idb: { state: "idle" },
    snapshot: { state: "idle" },
    post: { state: "idle" },
    bridgeDefined: typeof window.sweNative === "object",
    errors: [],
  };
  window.addEventListener("error", (e) => state.errors.push(String(e.message)));
  window.addEventListener("unhandledrejection", (e) => state.errors.push(String(e.reason)));

  // Pinned to the end: new rows and a shorter timeline (keyboard up) keep
  // the newest row in view while the user hasn't scrolled away.
  // Only the user unpins: a scroll that moves up from where the page last
  // put the timeline. (Judging by distance alone unpinned on the first
  // streamed row: the scroll event from our own jump can land after the
  // next row has grown the timeline.)
  let pinned = true;
  let placedAt = 0;
  const distanceFromEnd = () => timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight;
  const toEnd = () => { timeline.scrollTop = timeline.scrollHeight; placedAt = timeline.scrollTop; };
  timeline.addEventListener("scroll", () => {
    if (distanceFromEnd() < 4) {
      pinned = true;
      placedAt = timeline.scrollTop;
    } else if (timeline.scrollTop < placedAt - 4) {
      pinned = false;
      placedAt = timeline.scrollTop;
    }
  }, { passive: true });
  new ResizeObserver(() => { if (pinned) toEnd(); }).observe(timeline);
  const track = () => {
    state.maxWindowScroll = Math.max(state.maxWindowScroll, Math.abs(window.scrollY), Math.abs(window.scrollX),
      document.scrollingElement ? Math.abs(document.scrollingElement.scrollTop) : 0);
    if (window.visualViewport) {
      state.maxVisualOffset = Math.max(state.maxVisualOffset, Math.abs(visualViewport.offsetTop));
    }
  };
  window.addEventListener("scroll", track, { passive: true });
  if (window.visualViewport) {
    visualViewport.addEventListener("scroll", track);
    visualViewport.addEventListener("resize", track);
  }

  function row(html, cls) {
    const div = document.createElement("div");
    div.className = "row" + (cls ? " " + cls : "");
    div.innerHTML = html;
    return div;
  }
  function append(html, cls) {
    const keep = pinned;
    timeline.appendChild(row(html, cls));
    state.rows++;
    if (keep) toEnd();
  }

  const words = "the agent read the file then ran the tests and wrote a short summary of what changed".split(" ");
  const frag = document.createDocumentFragment();
  for (let i = 1; i <= 2000; i++) {
    const n = 4 + (i * 7) % 18;
    const text = Array.from({ length: n }, (_, k) => words[(i + k) % words.length]).join(" ");
    frag.appendChild(row(`<b>Turn ${i}</b> ${text}`));
  }
  timeline.appendChild(frag);
  state.rows = 2000;
  toEnd();

  document.getElementById("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!input.value.trim()) return;
    append(`<b>You</b> ${input.value.replace(/[<&]/g, (c) => (c === "<" ? "&lt;" : "&amp;"))}`, "mine");
    input.value = "";
  });

  const time = (iso) => { try { return new Date(iso).toLocaleTimeString(); } catch (_) { return iso; } };

  async function snapshot() {
    state.snapshot = { state: "loading" };
    try {
      const r = await fetch("/v1/snapshot");
      const source = r.headers.get("X-Swe-Shell-Source");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const body = await r.json();
      state.snapshot = { state: "ok", source, token: body.token, savedAt: body.saved_at };
      for (const line of body.rows || []) append(`<b>Snapshot</b> ${line}`);
      if (source === "cache") {
        banner.textContent = `Offline: showing the copy saved at ${time(body.saved_at)}`;
        banner.className = "banner offline";
      } else {
        banner.textContent = `Live: snapshot ${body.token} at ${time(body.saved_at)}`;
        banner.className = "banner";
      }
      const names = [];
      r.headers.forEach((value, name) => names.push(name + ": " + value));
      state.snapshot.headers = names;
    } catch (e) {
      state.snapshot = { state: "error", error: String(e) };
      banner.textContent = "Offline, and nothing saved yet";
      banner.className = "banner offline";
    }
  }

  async function stream(count, interval) {
    state.stream.state = "open";
    state.stream.startedAt = Date.now();
    try {
      const r = await fetch(`/v1/events?count=${count}&interval=${interval}`,
        { headers: { Accept: "text/event-stream" } });
      state.stream.status = r.status;
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        state.stream.reads++;
        buffer += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          const data = block.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n");
          if (!data) continue;
          const event = JSON.parse(data);
          const arrived = Date.now();
          state.stream.arrivals.push({ i: event.i, sent: event.sent, arrived, read: state.stream.reads });
          append(`<b>Event ${event.i}</b> arrived ${arrived - event.sent} ms after the hub sent it`, "live");
        }
      }
      state.stream.state = "done";
    } catch (e) {
      state.stream.state = "error";
      state.stream.error = String(e);
    }
  }

  // Not the session view's transport (that reads fetch bodies), recorded
  // so the report can say whether EventSource works on the scheme too.
  function eventSource(count) {
    try {
      const es = new EventSource(`/v1/events?count=${count}&interval=200`);
      state.eventSource.state = "open";
      es.onmessage = (m) => {
        const event = JSON.parse(m.data);
        state.eventSource.arrivals.push({ i: event.i, sent: event.sent, arrived: Date.now() });
        if (state.eventSource.arrivals.length >= count) { es.close(); state.eventSource.state = "done"; }
      };
      es.onerror = () => {
        if (state.eventSource.state !== "done") {
          state.eventSource.state = "error";
          state.eventSource.readyState = es.readyState;
          es.close();
        }
      };
    } catch (e) {
      state.eventSource = { state: "throw", error: String(e), arrivals: [] };
    }
  }

  function idb(token) {
    state.idb = { state: "opening" };
    let open;
    try { open = indexedDB.open("shell-proof", 1); } catch (e) {
      state.idb = { state: "error", error: "open threw: " + e };
      return;
    }
    open.onupgradeneeded = () => open.result.createObjectStore("kv");
    open.onerror = () => { state.idb = { state: "error", error: String(open.error) }; };
    open.onsuccess = () => {
      const db = open.result;
      const tx = db.transaction("kv", "readwrite");
      const store = tx.objectStore("kv");
      const get = store.get("launch");
      get.onsuccess = () => {
        const previous = get.result || null;
        const current = {
          token: token || (previous && previous.token) || null,
          launches: ((previous && previous.launches) || 0) + 1,
          at: Date.now(),
        };
        store.put(current, "launch");
        tx.oncomplete = () => { state.idb = { state: "ok", previous, current }; };
      };
      tx.onerror = () => { state.idb = { state: "error", error: String(tx.error) }; };
    };
  }

  async function post() {
    state.post = { state: "sending" };
    try {
      const r = await fetch("/v1/echo", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "hello from the page", n: 42 }),
      });
      const body = await r.json();
      state.post = { state: "ok", status: r.status, echoed: body.body, authOK: body.auth_ok,
        pairOK: body.pair_ok, originOK: body.origin_ok };
    } catch (e) {
      state.post = { state: "error", error: String(e) };
    }
  }

  window.proofState = () => {
    const composer = document.getElementById("composer").getBoundingClientRect();
    const css = getComputedStyle(document.documentElement);
    return Object.assign({}, state, {
      timeline: {
        scrollTop: timeline.scrollTop, scrollHeight: timeline.scrollHeight,
        clientHeight: timeline.clientHeight, distanceFromEnd: distanceFromEnd(), pinned,
      },
      composer: { top: composer.top, bottom: composer.bottom, height: composer.height },
      innerHeight: window.innerHeight,
      innerWidth: window.innerWidth,
      windowScrollY: window.scrollY,
      visual: window.visualViewport ? {
        height: visualViewport.height, offsetTop: visualViewport.offsetTop,
        pageTop: visualViewport.pageTop, scale: visualViewport.scale,
      } : null,
      keyboardInset: css.getPropertyValue("--keyboard-inset").trim(),
      theme: document.documentElement.dataset.theme || null,
      prefersDark: matchMedia("(prefers-color-scheme: dark)").matches,
      background: getComputedStyle(document.body).backgroundColor,
      activeElement: document.activeElement ? document.activeElement.id : null,
      banner: banner.textContent,
      cookie: document.cookie,
      resources: performance.getEntriesByType("resource").map((e) => e.name),
    });
  };

  if (window.sweNative) window.sweNative.wake();
  idb(query.get("token"));
  snapshot();
  post();
  if (query.has("es")) eventSource(Number(query.get("es")) || 3);
  if (query.has("stream")) stream(Number(query.get("stream")), Number(query.get("interval")) || 200);
})();
