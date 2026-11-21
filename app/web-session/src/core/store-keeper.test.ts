// The store's part of G31: reading the hub's features while the stream is live, and the lookups after a catch-up
// (docs/specs/G11-client-core.md, "Keeper-only posts and command lookups").
import { describe, expect, it } from "vitest";

import { MAYBE_SENT, REPEATING } from "./outbox";
import { MemoryStorage, openOutboxStorage, type OutboxEntry } from "./outbox-storage";
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
    expect(t.state().outbox.entries).toMatchObject([{ status: "waiting", ambiguous: true, attempts: 1, keeperOnlyAt: 1, lastError: REPEATING }]);
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

  it("a hub that restarted (another boot_id) between the features and the post: not posted keeper-only until read again", async () => {
    const t = setup();
    let boot = "b1";
    t.transport.on("/v1/status", () => {
      t.hub.statusCalls++;
      return { status: 200, body: { ok: true, v: 1, protocols: [1, 2], features: t.hub.features, boot_id: boot } };
    });
    t.state().start();
    await t.catchUp();
    expect(t.hub.statusCalls).toBe(1);
    boot = "b2";
    t.hub.features = OLD; // an older version, the stream not dropped yet
    await t.state().send({ text: "hi" });
    await drain();
    expect(t.hub.posts).toEqual([]);
    t.timers.advance(60_000); // the send goes again on the outbox's backoff
    await drain();
    // Asked before the post: another process. The features are read again (it no longer lists require_keeper),
    // and the send goes plain, as merged.
    expect(t.hub.statusCalls).toBeGreaterThanOrEqual(3);
    expect(t.hub.posts).toHaveLength(1);
    expect(t.hub.posts[0]).not.toHaveProperty("require_keeper");
  });

  it("a changed boot_id alone, the features still listed, blocks the post until they are read again", async () => {
    const t = setup();
    let boot = "b1";
    t.transport.on("/v1/status", () => {
      t.hub.statusCalls++;
      return { status: 200, body: { ok: true, v: 1, protocols: [1, 2], features: ALL, boot_id: boot } };
    });
    t.state().start();
    await t.catchUp();
    boot = "b2"; // another process, which happens to list the same features
    await t.state().send({ text: "hi" });
    await drain();
    expect(t.hub.posts).toEqual([]);
    t.timers.advance(60_000);
    await drain();
    // Read again under b2: confirmed now, and posted keeper-only.
    expect(t.hub.posts).toEqual([expect.objectContaining({ require_keeper: true })]);
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
  it("a held send the page ended unconfirmed, then expired at the hub: the lookup's failed result makes it failed (review of G31)", async () => {
    const factory = new IDBFactory();
    const t = setup({ factory });
    const stored: { body: Record<string, unknown> } = { body: { ok: true, session: T, command_id: "id1", op: "send", receipt: "held", received_at: 1, updated_at: 1 } };
    t.transport.on(LOOKUP, () => ({ status: 200, body: stored.body }), "GET");
    t.answer.next = (body) => ({ status: 202, body: { ok: true, queued: true, command_id: body.command_id, receipt: "held" } });
    t.state().start();
    await t.catchUp();
    await t.state().send({ text: "run the migration" });
    await drain();
    expect(t.state().outbox.entries).toMatchObject([{ status: "sent", receipt: "held" }]);
    t.timers.advance(24 * 60 * 60 * 1000 + 1000); // the page's own 24 h end comes first
    await drain();
    expect(t.state().outbox.entries).toMatchObject([{ status: "unconfirmed" }]);
    const note = "expired: the hub stopped before this was sent, and it is too old to send now";
    stored.body = { ...stored.body, receipt: "rejected", reason: note, result: { ok: false, note } };
    await t.drop();
    await t.catchUp(6);
    expect(t.lookups()).toContain("id1");
    expect(t.state().outbox.entries).toMatchObject([{ status: "failed", error: note, stated: true }]);
    expect(await (await openOutboxStorage(factory)).load(T)).toMatchObject([{ status: "failed", error: note, stated: true }]);
    // Settled: not asked about again.
    const asked = t.lookups().length;
    await t.drop();
    await t.catchUp(7);
    expect(t.lookups()).toHaveLength(asked);
  });

  it("a snapshot taken on an epoch change inside one connection runs the lookups again", async () => {
    const t = setup();
    t.transport.on(LOOKUP, (path) => ({
      status: 200,
      body: { ok: true, session: T, command_id: path.slice(LOOKUP.length), op: "send", receipt: "accepted", received_at: 1, updated_at: 1 },
    }));
    t.state().start();
    await t.catchUp();
    await t.state().send({ text: "hi" });
    await drain();
    const before = t.lookups().length;
    // The journal starts over while the stream stays up: the state frame names another epoch, and the store takes
    // the whole snapshot.
    t.transport.on("/snapshot", () => ({ status: 200, body: { ...snap(3), epoch: 1 } }));
    t.transport.last.send(env("session.state", { state: "idle", live: [], pending: [], head: 3, epoch: 1 }, { seq: 0 }));
    await drain();
    expect(t.lookups().length).toBeGreaterThan(before);
  });

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

describe("stated failures (final review 2 of G31)", () => {
  const base: OutboxEntry = {
    commandId: "id1", session: T, text: "deploy", attachments: [], mode: "queue", createdAt: 0, status: "failed", attempts: 1,
  };

  it("a definite failure this page made (24 h, never reached the hub) gives way on disk to another tab's stated failure", async () => {
    const factory = new IDBFactory();
    const disk = await openOutboxStorage(factory);
    await disk.put({ ...base, error: "Not sent: the hub could not be reached for 24 hours.", endedAt: 10 });
    const standing = await disk.reconcile({ ...base, error: "expired", stated: true, endedAt: 20 });
    expect(standing).toMatchObject({ stated: true, error: "expired" });
    expect(await disk.load(T)).toMatchObject([{ stated: true, error: "expired" }]);
    // And never the other way.
    expect(await disk.reconcile({ ...base, error: "Not sent: the hub could not be reached for 24 hours.", endedAt: 30 })).toMatchObject({ stated: true });
    disk.close();
  });

  it("a rejected receipt on the stream ends a sent keeper send failed and stated, on disk", async () => {
    const factory = new IDBFactory();
    const t = setup({ factory });
    t.state().start();
    await t.catchUp();
    await t.state().send({ text: "deploy" });
    await drain();
    expect(t.state().outbox.entries).toMatchObject([{ status: "sent" }]);
    t.transport.last.send(env("command.receipt", { status: "rejected", reason: "not_recorded", op: "send" }, { command_id: "id1", seq: 7 }));
    await drain();
    const probe = await openOutboxStorage(factory);
    expect(await probe.load(T)).toMatchObject([{ status: "failed", stated: true, error: "not_recorded" }]);
    probe.close();
  });

  it("late() after stop(): a 2xx whose body says receipt rejected is stored failed and stated (G11 stored it sent)", async () => {
    const factory = new IDBFactory();
    const t = setup({ factory });
    let release: (r: { status: number; body: unknown }) => void = () => {};
    t.answer.next = () => new Promise((r) => { release = r; }) as never;
    t.state().start();
    await t.catchUp();
    void t.state().send({ text: "deploy" });
    await drain();
    t.state().stop();
    t.state().start();
    await t.catchUp(6);
    release({ status: 202, body: { ok: true, duplicate: true, command_id: "id1", receipt: "rejected", reason: "machine is too far behind" } });
    await drain();
    const probe = await openOutboxStorage(factory);
    expect(await probe.load(T)).toMatchObject([{ status: "failed", stated: true, error: "machine is too far behind" }]);
    probe.close();
    expect(t.hub.posts).toHaveLength(1);
  });

  it("a definite refusal the hub answered (409 send_rejected) is stated too", async () => {
    const factory = new IDBFactory();
    const t = setup({ factory });
    t.answer.next = () => ({ status: 409, body: { error: { code: "send_rejected", message: "the keeper refused it" } } });
    t.state().start();
    await t.catchUp();
    await t.state().send({ text: "deploy" });
    await drain();
    expect(t.state().outbox.entries).toMatchObject([{ status: "failed", stated: true, error: "the keeper refused it" }]);
  });
});
