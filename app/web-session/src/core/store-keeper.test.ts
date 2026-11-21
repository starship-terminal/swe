// The store's part of G31: reading the hub's features while the stream is live, and the lookups after a catch-up
// (docs/specs/G11-client-core.md, "Keeper-only posts and command lookups").
import { describe, expect, it } from "vitest";

import { MAYBE_SENT, REPEATING } from "./outbox";
import { MemoryStorage, openOutboxStorage } from "./outbox-storage";
import { createSessionStore } from "./store";
import { FakeEnvironment, FakeTimers, FakeTransport, drain, snapshotFrame } from "./test-support";
import type { JsonRequest } from "./transport";
import type { Frame, Snapshot } from "./types";
import { IDBFactory } from "fake-indexeddb";

const T = "local:term-1";
const LOOKUP = "/v1/sessions/local%3Aterm-1/commands/";
const ALL = ["sync_v2", "snapshot", "command_id", "require_keeper", "command_lookup", "session_runtime"];
const OLD = ["sync_v2", "snapshot", "command_id", "session_runtime"];

const env = (type: string, data: unknown, extra: Record<string, unknown> = {}): Frame =>
  ({ v: 2, type, session: T, ts: 1, ...extra, data }) as unknown as Frame;
const snap = (head: number, row: Record<string, unknown> = { mode: "keeper" }): Snapshot =>
  ({
    session: T, epoch: 0, head, boot_id: "b1", row: { key: T, ...row }, state: "idle", caps: null, open_turn: null,
    pending: [], queue: [], live: [], plan: null, tasks: [], checkpoints: [], changes: null, usage: null,
    messages: [], has_more: false, next_before: null,
  }) as Snapshot;

function setup(over: { features?: string[] | null; factory?: IDBFactory } = {}) {
  const timers = new FakeTimers();
  const transport = new FakeTransport();
  const hub = { features: over.features === undefined ? ALL : over.features, statusCalls: 0, posts: [] as Record<string, unknown>[] };
  transport.on("/v1/status", () => {
    hub.statusCalls++;
    return hub.features === null ? { status: 500, body: {} } : { status: 200, body: { ok: true, v: 1, protocols: [1, 2], features: hub.features, boot_id: "b1" } };
  });
  // By default every post is taken by a keeper. A test replaces this for other answers.
  const answer: { next: (body: Record<string, unknown>) => { status: number; body: unknown } } = {
    next: (body) => ({ status: 202, body: { ok: true, receipt: "accepted", command_id: body.command_id } }),
  };
  transport.on(
    "/message",
    async (_path, request: JsonRequest) => {
      const body = request.body as Record<string, unknown>;
      hub.posts.push(body);
      return answer.next(body);
    },
    "POST",
  );
  let n = 0;
  const factory = over.factory;
  const store = createSessionStore({
    sessionKey: T, transport, timers, random: () => 0, environment: new FakeEnvironment(),
    openStorage: factory === undefined ? async () => new MemoryStorage("mem") : () => openOutboxStorage(factory),
    newId: () => `id${++n}`,
    // No pings in these tests: a stream stays live until a test drops it.
    silenceMs: 48 * 60 * 60 * 1000,
  });
  const state = () => store.getState();
  /** A catch-up by snapshot, ending in its state frame: the stream is live and caught up after it. */
  const catchUp = async (head = 5, row?: Record<string, unknown>) => {
    transport.last.send(snapshotFrame(snap(head, row)), env("session.state", { state: "idle", live: [], pending: [], head, epoch: 0 }, { seq: 0 }));
    await drain();
  };
  /** The stream drops after it lasted (the retry is at once). */
  const drop = async () => {
    timers.advance(10_000);
    transport.last.end({ kind: "closed" });
    await drain();
  };
  const lookups = () => transport.calls.filter((c) => c.path.startsWith(LOOKUP)).map((c) => c.path.slice(LOOKUP.length));
  return { timers, transport, hub, answer, store, state, catchUp, drop, lookups };
}

describe("the hub's features", () => {
  it("are read from /v1/status when the stream goes live, and a keeper row's send is posted keeper-only", async () => {
    const t = setup();
    t.state().start();
    expect(t.hub.statusCalls).toBe(0);
    await t.catchUp();
    expect(t.hub.statusCalls).toBe(1);
    await t.state().send({ text: "hi" });
    await drain();
    expect(t.hub.posts).toEqual([expect.objectContaining({ command_id: "id1", require_keeper: true })]);
  });

  it("a row that does not say keeper is posted plain", async () => {
    const t = setup();
    t.answer.next = () => ({ status: 202, body: { ok: true, queued: true } });
    t.state().start();
    await t.catchUp(5, {});
    await t.state().send({ text: "hi" });
    await drain();
    expect(t.hub.posts[0]).not.toHaveProperty("require_keeper");
  });

  it("a hub that does not list require_keeper gets the merged behaviour: plain posts, and a lost answer ends it", async () => {
    const t = setup({ features: OLD });
    t.answer.next = () => {
      throw new TypeError("dropped");
    };
    t.state().start();
    await t.catchUp();
    await t.state().send({ text: "hi" });
    await drain();
    t.timers.advance(60_000);
    await drain();
    expect(t.hub.posts).toHaveLength(1);
    expect(t.hub.posts[0]).not.toHaveProperty("require_keeper");
    expect(t.state().outbox.entries).toMatchObject([{ status: "failed", error: MAYBE_SENT }]);
    expect(t.lookups()).toEqual([]);
  });

  it("are forgotten when the stream drops: a send in doubt waits through the reconnect and is repeated keeper-only after it", async () => {
    const t = setup();
    let first = true;
    t.answer.next = (body) => {
      if (first) {
        first = false;
        throw new TypeError("dropped");
      }
      return { status: 202, body: { ok: true, duplicate: true, receipt: "accepted", command_id: body.command_id } };
    };
    t.state().start();
    await t.catchUp();
    await t.state().send({ text: "hi" });
    await drain();
    expect(t.state().outbox.entries).toMatchObject([{ status: "waiting", ambiguous: true, keeperOnly: true, lastError: REPEATING }]);
    // The stream drops before the backoff runs out: the features are not known while it reconnects.
    t.hub.features = null; // and /v1/status fails for a while
    await t.drop();
    t.timers.advance(60_000);
    await drain();
    expect(t.hub.posts).toHaveLength(1);
    await t.catchUp(6);
    t.timers.advance(60_000);
    await drain();
    expect(t.hub.posts).toHaveLength(1); // /v1/status still failing: not known, still held
    t.hub.features = ALL;
    t.timers.advance(60_000); // the store asks /v1/status again on its backoff
    await drain();
    expect(t.hub.posts.map((p) => p.require_keeper)).toEqual([true, true]);
    expect(t.state().outbox.entries).toMatchObject([{ status: "sent", path: "keeper" }]);
  });

  it("a hub back as an older version: the send in doubt is never posted plain, and ends may-or-may-not", async () => {
    const t = setup();
    t.answer.next = () => {
      throw new TypeError("dropped");
    };
    t.state().start();
    await t.catchUp();
    await t.state().send({ text: "hi" });
    await drain();
    t.hub.features = OLD;
    await t.drop();
    await t.catchUp(6);
    t.timers.advance(60_000);
    await drain();
    expect(t.hub.posts).toHaveLength(1);
    expect(t.state().outbox.entries).toMatchObject([{ status: "failed", ambiguous: true, error: MAYBE_SENT }]);
  });

  it("a /v1/status answer that comes after the stream left live is not taken", async () => {
    const t = setup();
    let release: (value: { status: number; body: unknown }) => void = () => {};
    t.transport.on("/v1/status", () => new Promise((resolve) => (release = resolve)));
    t.state().start();
    await t.catchUp();
    await t.drop();
    release({ status: 200, body: { features: ALL } });
    await drain();
    await t.state().send({ text: "hi" });
    t.timers.advance(1000);
    await drain();
    // Posted while reconnecting, with the features not known: plain.
    expect(t.hub.posts[0]).not.toHaveProperty("require_keeper");
  });

  it("stop() forgets them; start() reads them again", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    t.state().stop();
    t.state().start();
    expect(t.hub.statusCalls).toBe(1);
    await t.catchUp();
    expect(t.hub.statusCalls).toBe(2);
  });
});

describe("lookups", () => {
  it("a catch-up by snapshot carried no result: the lookup settles the send, with no second post", async () => {
    const t = setup();
    const results = new Map<string, unknown>();
    t.transport.on(LOOKUP, (path) => {
      const id = path.slice(LOOKUP.length);
      const result = results.get(id);
      return { status: 200, body: { ok: true, session: T, command_id: id, op: "send", receipt: "accepted", received_at: 1, updated_at: 1, ...(result ? { result } : {}) } };
    });
    t.state().start();
    await t.catchUp();
    await t.state().send({ text: "hi" });
    await drain();
    expect(t.state().outbox.entries).toMatchObject([{ status: "sent", path: "keeper" }]);
    results.set("id1", { op: "send", ok: true });
    await t.drop();
    await t.catchUp(900); // a wide gap: the snapshot carries no command.result
    expect(t.lookups()).toContain("id1");
    expect(t.state().outbox.entries).toEqual([]);
    expect(t.hub.posts).toHaveLength(1);
  });

  it("run when the stored sends load, once the features are known", async () => {
    const factory = new IDBFactory();
    const storage = await openOutboxStorage(factory);
    await storage.put({
      commandId: "w-1", session: T, text: "x", attachments: [], mode: "queue", createdAt: 1, status: "failed", attempts: 1,
      ambiguous: true, error: MAYBE_SENT, endedAt: 1,
    });
    storage.close();
    const t = setup({ factory });
    t.transport.on(LOOKUP, (path) => ({
      status: 200,
      body: { ok: true, session: T, command_id: path.slice(LOOKUP.length), op: "send", receipt: "accepted", received_at: 1, updated_at: 1, result: { ok: true } },
    }));
    t.state().start();
    await drain();
    expect(t.lookups()).toEqual([]); // not live yet: not known
    await t.catchUp();
    expect(t.lookups()).toContain("w-1");
    expect(t.state().outbox.entries).toEqual([]);
  });
});
