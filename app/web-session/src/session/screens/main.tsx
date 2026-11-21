// The screens page: the session view over a scripted hub. `?scene=<name>&theme=light|dark` picks
// what it shows; `window.__scene` lets a person (or a screenshot script) push more frames, cut the
// stream or go offline. Only the dev server serves it (see vite.config.ts).
import "../../index.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { createSessionStore, describeSends, MAYBE_SENT, type Frame, type OutboxEntry, type Snapshot } from "../../core";
import { MemoryStorage } from "../../core/outbox-storage";
import type { OutboxView } from "../../core/outbox";
import type { Environment, Timers } from "../../core/supervisor";
import type { StreamHandlers, Transport } from "../../core/transport";
import { SessionView } from "../SessionView";

const KEY = "claude:session-1";

interface Recording {
  connections: { frames: Frame[] }[];
  snapshots: { body: Snapshot }[];
}

const fixtures = import.meta.glob<Recording>("../../../../../tests/fixtures/session-view/*.json", { eager: true, import: "default" });
const fixture = (name: string): Recording => {
  const found = Object.entries(fixtures).find(([path]) => path.endsWith(`/${name}.json`));
  if (found === undefined) throw new Error(`no fixture ${name}`);
  return found[1];
};

class Script implements Transport {
  handlers: StreamHandlers | null = null;
  open(_path: string, handlers: StreamHandlers) {
    this.handlers = handlers;
    return { close() {} };
  }
  async json() {
    return { status: 404, body: undefined };
  }
  frames(...frames: Frame[]): void {
    this.handlers?.onOpen();
    for (const frame of frames) this.handlers?.onFrame(frame);
  }
  end(end: Parameters<StreamHandlers["onEnd"]>[0]): void {
    this.handlers?.onEnd(end);
  }
}

class Env implements Environment {
  isOnline = true;
  private listeners = new Set<{ online(): void; offline(): void; visible(): void }>();
  online() {
    return this.isOnline;
  }
  subscribe(listener: { online(): void; offline(): void; visible(): void }) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  set(online: boolean): void {
    this.isOnline = online;
    for (const listener of this.listeners) (online ? listener.online : listener.offline)();
  }
}

// Timers that fire only when asked: nothing reconnects, retries or ages while a screen is photographed.
class Manual implements Timers {
  private next = 1;
  private waiting = new Map<number, () => void>();
  now = () => Date.now();
  setTimeout(callback: () => void): unknown {
    this.waiting.set(this.next, callback);
    return this.next++;
  }
  clearTimeout(handle: unknown): void {
    this.waiting.delete(handle as number);
  }
  /** Run every timer that is waiting now (the ones they arm wait for the next call). */
  fire(): void {
    const run = Array.from(this.waiting.values());
    this.waiting.clear();
    for (const callback of run) callback();
  }
}
const frozen = new Manual();

const params = new URLSearchParams(window.location.search);
const scene = params.get("scene") ?? "default";
const theme = params.get("theme");
if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;

const transport = new Script();
const env = new Env();
const store = createSessionStore({
  sessionKey: KEY,
  transport,
  timers: frozen,
  random: () => 0,
  environment: env,
  openStorage: async () => new MemoryStorage(scene === "sends-storage-down" ? "IndexedDB failed (connection lost)" : ""),
  newId: () => `w-${Math.random().toString(16).slice(2, 10)}`,
});

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function entry(over: Partial<OutboxEntry> & Pick<OutboxEntry, "commandId" | "text" | "status">): OutboxEntry {
  return { session: KEY, attachments: [], mode: "queue", createdAt: Date.now(), attempts: 1, ...over };
}

function showSends(entries: OutboxEntry[], over: Partial<OutboxView> = {}): void {
  const outbox: OutboxView = {
    entries,
    persistent: true,
    note: "",
    paused: null,
    inFlight: null,
    owner: "screens",
    loading: false,
    storageDown: false,
    ...over,
  };
  store.setState({
    outbox,
    sends: describeSends({ outbox, connection: store.getState().connection, loaded: true, online: env.isOnline }),
  });
}

async function run(): Promise<void> {
  store.getState().start();
  const base = fixture("default");
  const all = base.connections[0]?.frames ?? [];
  await sleep(30);
  switch (scene) {
    case "loading":
      break;
    case "empty": {
      const body = base.snapshots[base.snapshots.length - 1]?.body as Snapshot;
      transport.frames(
        { v: 2, type: "session.snapshot", session: KEY, ts: 1, data: { ...body, messages: [], state: "idle", open_turn: null, plan: null, tasks: [], checkpoints: [], changes: null, usage: null } } as Frame,
        { v: 2, type: "session.state", session: KEY, seq: 0, ts: 1, data: { state: "idle", live: [], pending: [], head: body.head, epoch: body.epoch } } as Frame,
      );
      break;
    }
    case "streaming": {
      const streaming = fixture("streaming").connections[0]?.frames ?? [];
      const third = streaming.findIndex((frame) => frame.type === "message.delta" && frame.data.offset === 6);
      transport.frames(...streaming.slice(0, third + 1));
      break;
    }
    case "gone":
      transport.end({ kind: "http", status: 404 });
      break;
    case "hub-down":
      // A session that loaded, and then three attempts to get any answer failed.
      transport.frames(...all);
      for (let attempt = 0; attempt < 3; attempt++) {
        transport.end({ kind: "error", message: "Failed to fetch" });
        frozen.fire();
      }
      break;
    default:
      if (!scene.startsWith("sends")) transport.frames(...all);
  }
  if (scene.startsWith("sends")) {
    // The sends sit on an empty session, so every one of them is in the picture.
    const body = base.snapshots[base.snapshots.length - 1]?.body as Snapshot;
    transport.frames(
      { v: 2, type: "session.snapshot", session: KEY, ts: 1, data: { ...body, messages: [], state: "idle", open_turn: null, plan: null, tasks: [], checkpoints: [], changes: null, usage: null } } as Frame,
      { v: 2, type: "session.state", session: KEY, seq: 0, ts: 1, data: { state: "idle", live: [], pending: [], head: body.head, epoch: body.epoch } } as Frame,
    );
  }
  if (scene === "offline" || scene === "sends-offline") env.set(false);
  if (scene === "reconnecting") transport.end({ kind: "closed" });
  if (scene.startsWith("sends")) {
    await sleep(30);
    const now = Date.now();
    if (scene === "sends-loading") {
      showSends([entry({ commandId: "w-a", text: "Run the tests again", status: "failed", error: "Not sent." })], { loading: true });
    } else if (scene === "sends-storage-down") {
      showSends([entry({ commandId: "w-a", text: "Fix the failing test in src/app.py", status: "waiting", attempts: 0 })], { storageDown: true, persistent: false, note: "IndexedDB failed (connection lost). Sends saved before are not sent until it answers again, and new ones are kept in memory and will not survive a reload until then" });
    } else {
      showSends(
        [
          entry({ commandId: "w-1", text: "Fix the failing test in src/app.py", status: "waiting", attempts: 0, createdAt: now - 5000 }),
          entry({ commandId: "w-2", text: "Then run the linter", status: "waiting", attempts: 1, createdAt: now - 4000 }),
          entry({ commandId: "w-3", text: "And update the changelog", status: "sent", path: "keeper", receipt: "queued", createdAt: now - 3000 }),
          entry({ commandId: "w-4", text: "Delete the build folder", status: "failed", error: "the session cannot take a message now", createdAt: now - 2000 }),
          entry({ commandId: "w-5", text: "Push the branch", status: "failed", ambiguous: true, error: MAYBE_SENT, createdAt: now - 1500 }),
          entry({ commandId: "w-6", text: "Open the pull request", status: "unconfirmed", error: "The hub took this message, but its result was not seen on this device. Check the session before sending it again.", createdAt: now - 1000 }),
        ],
        { inFlight: "w-2" },
      );
    }
  }
}

/** `?click=Worked for|Edit src` presses, in order, the first button whose text starts with each part. */
async function clicks(): Promise<void> {
  for (const label of (params.get("click") ?? "").split("|").filter(Boolean)) {
    await sleep(120);
    const found = Array.from(document.querySelectorAll("button")).find((b) => (b.textContent ?? "").trim().startsWith(label));
    found?.click();
  }
}

Object.assign(window, { __scene: { store, transport, env, fixture, showSends, entry, timers: frozen } });

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <SessionView sessionKey={KEY} store={store} />
  </StrictMode>,
);
void run().then(clicks).then(() => Object.assign(window, { __ready: true }));
