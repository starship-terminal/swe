import { IDBFactory } from "fake-indexeddb";
import { describe, expect, it } from "vitest";

import { createSessionStore, type SessionStore } from "./store";
import { openOutboxStorage, MemoryStorage } from "./outbox-storage";
import { MAYBE_SENT, NOT_SENT_IN_TIME, SESSION_GONE, TERMINAL_KEPT_MS } from "./outbox";
import { CLAIM_STALE_MS } from "./outbox-storage";
import { FakeEnvironment, FakeTimers, FakeTransport, loadFixture, snapshotFrame, type Fixture } from "./test-support";
import type { Frame, Snapshot } from "./types";

const KEY = "claude:session-1";
const BASE = "/v1/sessions/claude%3Asession-1";

const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
const drain = async () => {
  for (let i = 0; i < 5; i++) await settle();
};

function boot(
  over: { factory?: IDBFactory; online?: boolean; memory?: boolean; transport?: FakeTransport; timers?: FakeTimers } = {},
) {
  const timers = over.timers ?? new FakeTimers();
  const transport = over.transport ?? new FakeTransport();
  const environment = new FakeEnvironment();
  environment.isOnline = over.online ?? true;
  const factory = over.factory ?? new IDBFactory();
  let ids = 0;
  const store: SessionStore = createSessionStore({
    sessionKey: KEY,
    transport,
    timers,
    random: () => 0,
    environment,
    openStorage: over.memory ? async () => new MemoryStorage("no database in tests") : () => openOutboxStorage(factory),
    newId: () => `id${++ids}`,
  });
  return { store, transport, timers, environment, factory, state: () => store.getState() };
}

/** The calls the store made to the hub. */
const api = (t: { transport: FakeTransport }) => t.transport.calls;

const frames = (fixture: Fixture, connection = 0, from = 0, to?: number): Frame[] =>
  (fixture.connections[connection] as Fixture["connections"][number]).frames.slice(from, to);

/** Start a store and play a fixture's first connection into it. */
function playing(name: string, over: Parameters<typeof boot>[0] = {}) {
  const t = boot(over);
  const fixture = loadFixture(name);
  t.state().start();
  return { ...t, fixture };
}

describe("a store over a recorded stream", () => {
  it("opens the stream, goes live on the first frame and builds the session from the frames", async () => {
    const t = playing("default");
    expect(t.transport.last.path).toBe(`${BASE}/events?v=2`);
    expect(t.state().connection.state).toBe("connecting");
    t.transport.last.send(...frames(t.fixture));
    await drain();
    expect(t.state().connection.state).toBe("live");
    const { session } = t.state();
    expect(session.caughtUp).toBe(true);
    expect(session.loaded).toBe(true);
    expect(session.phase).toBe("idle");
    expect(session.items.length).toBeGreaterThan(5);
    expect(session.cursor).toBe(t.fixture.snapshots[0]?.body.head);
  });

  it("reconnects asking with the cursor and epoch, and takes the replay", async () => {
    const t = playing("reconnect");
    t.transport.last.send(...frames(t.fixture, 0));
    await drain();
    expect(t.state().session.live.map((l) => l.text)).toEqual(["Hel"]);
    t.timers.advance(10_000); // the stream stayed open: its drop is retried at once
    t.transport.last.end({ kind: "closed" });
    expect(t.state().connection.state).toBe("reconnecting");
    const second = t.fixture.connections[1] as Fixture["connections"][number];
    expect(t.transport.last.query).toEqual({ v: "2", last: String(second.request.last), epoch: String(second.request.epoch) });
    // Not caught up again until the state frame.
    expect(t.state().session.caughtUp).toBe(false);
    t.transport.last.send(...frames(t.fixture, 1));
    await drain();
    expect(t.state().session.items.find((i) => i.kind === "message" && i.role === "assistant")).toMatchObject({ text: "Hello world" });
    expect(t.state().session.live).toEqual([]);
    expect(t.state().session.caughtUp).toBe(true);
  });

  it("recovers a lost delta by fetching the snapshot and resuming from its live[]", async () => {
    const t = playing("streaming");
    const all = frames(t.fixture);
    const lost = all.findIndex((f) => f.type === "message.delta" && f.data.offset === 3);
    const hole = t.fixture.snapshots[0] as Fixture["snapshots"][number];
    t.transport.on("/snapshot", () => ({ status: 200, body: hole.body }));
    // Everything but the third chunk, up to the one after it.
    const upTo = all.findIndex((f) => f.type === "message.delta" && f.data.offset === 6);
    t.transport.last.send(...all.slice(0, upTo + 1).filter((_f, i) => i !== lost));
    await drain();
    expect(api(t).map((c) => c.path)).toEqual([`${BASE}/snapshot`]);
    expect(t.state().session.live.map((l) => l.text)).toEqual(["Hello wor"]);
    expect(t.state().session.stopped).toEqual([]);
    // The rest arrives as usual.
    t.transport.last.send(...all.slice(upTo + 1));
    await drain();
    expect(t.state().session.live).toEqual([]);
    expect(t.state().session.items.at(-1)).toMatchObject({ kind: "message", text: "Hello world" });
    expect(api(t)).toHaveLength(1);
  });

  it("does not bring back a reply the stream finished while the snapshot was on its way", async () => {
    const t = playing("streaming");
    const all = frames(t.fixture);
    const hole = t.fixture.snapshots[0] as Fixture["snapshots"][number];
    let answer: (r: { status: number; body: unknown }) => void = () => {};
    t.transport.on("/snapshot", () => new Promise((resolve) => (answer = resolve)));
    const lost = all.findIndex((f) => f.type === "message.delta" && f.data.offset === 3);
    const upTo = all.findIndex((f) => f.type === "message.delta" && f.data.offset === 6);
    t.transport.last.send(...all.slice(0, upTo + 1).filter((_f, i) => i !== lost));
    await drain();
    // The rest of the stream, the completion included, arrives before the response does.
    t.transport.last.send(...all.slice(upTo + 1));
    expect(t.state().session.live).toEqual([]);
    answer({ status: 200, body: hole.body }); // older: it still has "Hello wor" in progress
    await drain();
    expect(t.state().session.live).toEqual([]);
    expect(t.state().session.stopped).toEqual([]);
    expect(t.state().session.items.filter((i) => i.kind === "message" && i.role === "assistant")).toHaveLength(1);
  });

  it("keeps the text of a message the hole did not stop when the snapshot is older than the stream", async () => {
    const t = playing("streaming");
    const all = frames(t.fixture);
    const hole = t.fixture.snapshots[0] as Fixture["snapshots"][number];
    t.transport.on("/snapshot", () => ({ status: 200, body: { ...hole.body, live: [{ id: "a1", kind: "assistant", text: "Hel" }, ...hole.body.live] } }));
    t.transport.last.send(...all.slice(0, 3)); // snapshot, state, "Hel"
    expect(t.state().session.live.map((l) => l.text)).toEqual(["Hel"]);
    t.transport.last.send(all[3] as Frame); // "lo "
    // A stale snapshot (answering an earlier hole) must not roll "Hello " back to "Hel".
    const { applyLive } = await import("./reducer");
    const next = applyLive(t.state().session, [{ id: "a1", kind: "assistant", text: "Hel" }]);
    expect(next.live.map((l) => l.text)).toEqual(["Hello "]);
  });

  it("does not replace newer state with an older snapshot of the same epoch", async () => {
    const t = playing("epoch_change");
    const first = frames(t.fixture, 0);
    const body = (first[0] as Extract<Frame, { type: "session.snapshot" }>).data;
    t.transport.on("/snapshot", () => ({ status: 200, body: { ...body, head: 1 } }));
    t.transport.last.send(...first.slice(0, 2));
    const cursor = t.state().session.cursor;
    // A state frame on another epoch asks for the snapshot; the hub's answer is on the epoch held and behind the cursor.
    t.transport.last.send({ v: 2, type: "session.state", session: KEY, seq: 0, ts: 1, data: { state: "idle", live: [], pending: [], head: cursor, epoch: 9 } });
    await drain();
    expect(t.state().session.cursor).toBe(cursor);
    expect(t.state().session.items.length).toBeGreaterThan(0);
  });

  it("asks again, with backoff, when the snapshot cannot be fetched", async () => {
    const t = playing("streaming");
    const all = frames(t.fixture);
    const hole = t.fixture.snapshots[0] as Fixture["snapshots"][number];
    let answered = 0;
    t.transport.on("/snapshot", () => {
      answered++;
      if (answered < 3) throw new TypeError("network down");
      return { status: 200, body: hole.body };
    });
    const lost = all.findIndex((f) => f.type === "message.delta" && f.data.offset === 3);
    const upTo = all.findIndex((f) => f.type === "message.delta" && f.data.offset === 6);
    t.transport.last.send(...all.slice(0, upTo + 1).filter((_f, i) => i !== lost));
    await drain();
    expect(answered).toBe(1);
    t.timers.advance(500);
    await drain();
    expect(answered).toBe(2);
    t.timers.advance(1000);
    await drain();
    expect(answered).toBe(3);
    expect(t.state().session.live.map((l) => l.text)).toEqual(["Hello wor"]);
  });

  it("takes a whole snapshot when a state frame is on another epoch", async () => {
    const t = playing("epoch_change");
    const first = frames(t.fixture, 0)[0] as Frame;
    const body = (frames(t.fixture, 0)[2] as Extract<Frame, { type: "session.snapshot" }>).data;
    t.transport.on("/snapshot", () => ({ status: 200, body }));
    t.transport.last.send(first);
    await drain();
    expect(t.state().session.epoch).toBe(0);
    t.transport.last.send({
      v: 2,
      type: "session.state",
      session: KEY,
      seq: 0,
      ts: 1,
      data: { state: "idle", live: [], pending: [], head: body.head, epoch: 1 },
    });
    await drain();
    expect(t.state().session.epoch).toBe(1);
  });
});

describe("what needs a live connection", () => {
  const live = async (name = "approval") => {
    const t = playing(name);
    return t;
  };

  it("refuses an approval, Stop, Restore and a queue action while not live, with a reason", async () => {
    const t = await live();
    for (const run of [
      () => t.state().respond("r1", "allow"),
      () => t.state().interrupt(),
      () => t.state().restore("ck-1"),
      () => t.state().queueAction("steer", "q1"),
    ]) {
      expect(await run()).toMatchObject({ ok: false, reason: "not_live", message: expect.stringContaining("Connecting") });
    }
    expect(t.transport.calls).toEqual([]);
    expect(t.state().refusal()?.reason).toBe("not_live");
  });

  it("says why for each state of the connection", async () => {
    const t = await live();
    t.transport.last.send(...frames(t.fixture, 0, 0, 2));
    t.environment.goOffline();
    expect(await t.state().interrupt()).toMatchObject({ ok: false, reason: "not_live", message: expect.stringContaining("offline") });
    t.environment.goOnline();
    expect(t.state().connection.state).toBe("reconnecting");
    expect(await t.state().interrupt()).toMatchObject({ ok: false, message: expect.stringContaining("Reconnecting") });
    t.transport.last.end({ kind: "http", status: 401 });
    expect(await t.state().interrupt()).toMatchObject({ ok: false, reason: "unauthorized" });
    t.state().wake();
    t.transport.last.end({ kind: "http", status: 404 });
    expect(await t.state().interrupt()).toMatchObject({ ok: false, reason: "gone" });
    t.state().wake();
    t.transport.last.send({ type: "session.status", data: {} });
    expect(await t.state().interrupt()).toMatchObject({ ok: false, reason: "outdated" });
  });

  it("refuses while the catch-up is not finished: the snapshot has come but not the state frame", async () => {
    const t = await live();
    t.transport.last.send(frames(t.fixture, 0)[0] as Frame);
    expect(t.state().connection.state).toBe("live");
    expect(await t.state().respond("r1", "allow")).toMatchObject({ ok: false, reason: "catching_up" });
    t.transport.last.send(frames(t.fixture, 0)[1] as Frame);
    expect(t.state().refusal()).toBeNull();
  });

  it("answers an approval once it is live and caught up, with a command id", async () => {
    const t = await live();
    t.transport.on("/approval", () => ({ status: 200, body: { ok: true, receipt: "accepted", command_id: "id1" } }), "POST");
    t.transport.last.send(...frames(t.fixture, 0, 0, 2));
    expect(await t.state().respond("r1", "allow")).toEqual({ ok: true, commandId: "id1", receipt: "accepted" });
    expect(t.transport.posts("/approval")).toEqual([{ request_id: "r1", decision: "allow", command_id: "id1" }]);
  });

  it("posts Stop, Restore and queue actions to their routes", async () => {
    const t = await live();
    for (const tail of ["control", "restore", "queue"]) t.transport.on(`/${tail}`, () => ({ status: 200, body: { ok: true } }), "POST");
    t.transport.last.send(...frames(t.fixture, 0, 0, 2));
    await t.state().interrupt();
    await t.state().restore("ck-1");
    await t.state().queueAction("edit", "q1", "new words");
    await t.state().queueAction("steer", "q2");
    expect(t.transport.posts("/control")).toEqual([{ op: "interrupt", command_id: "id1" }]);
    expect(t.transport.posts("/restore")).toEqual([{ checkpoint_id: "ck-1", command_id: "id2" }]);
    expect(t.transport.posts("/queue")).toEqual([
      { client_id: "q1", op: "edit", command_id: "id3", text: "new words" },
      { client_id: "q2", op: "steer", command_id: "id4" },
    ]);
  });

  it("reports what the hub says when it refuses, and a network failure, and never retries", async () => {
    const t = await live();
    t.transport.last.send(...frames(t.fixture, 0, 0, 2));
    t.transport.on("/approval", () => ({ status: 409, body: { error: { code: "approval_failed", message: "that request is no longer open" } } }), "POST");
    expect(await t.state().respond("r1", "allow")).toEqual({
      ok: false,
      reason: "refused",
      status: 409,
      message: "that request is no longer open",
    });
    t.transport.on("/approval", () => ({ status: 403, body: undefined }), "POST");
    expect(await t.state().respond("r1", "allow")).toMatchObject({ ok: false, reason: "unauthorized" });
    t.transport.down = true;
    expect(await t.state().respond("r1", "allow")).toMatchObject({ ok: false, reason: "network" });
    expect(api(t)).toHaveLength(3);
  });

  it("refuses everything on a read-only session, with the reason the row gives", async () => {
    const t = await live("default");
    const first = frames(t.fixture)[0] as Extract<Frame, { type: "session.snapshot" }>;
    t.transport.last.send(
      { ...first, data: { ...first.data, row: { ...first.data.row, control: "read_only", control_reason: "Started in a terminal" } } },
      frames(t.fixture)[1] as Frame,
    );
    expect(await t.state().interrupt()).toMatchObject({ ok: false, reason: "read_only", message: "Started in a terminal" });
    expect(await t.state().send({ text: "hi" })).toMatchObject({ ok: false, reason: "read_only" });
  });
});

describe("sends", () => {
  it("are accepted while offline, kept, and sent once the connection is live", async () => {
    const t = boot({ online: false });
    t.transport.on("/message", () => ({ status: 202, body: { receipt: "accepted" } }), "POST");
    t.state().start();
    expect(t.state().connection.state).toBe("offline");
    const sent = await t.state().send({ text: "typed on the train" });
    expect(sent).toMatchObject({ ok: true, entry: { commandId: "id1", status: "waiting" } });
    expect(t.state().outbox.entries).toHaveLength(1);
    t.transport.down = true;
    await drain();
    t.transport.down = false;
    t.environment.goOnline();
    expect(t.transport.posts("/message")).toEqual([]); // not before the session is known
    t.transport.last.send(...frames(loadFixture("default"), 0, 0, 2));
    await drain();
    expect(t.transport.posts("/message").map((b) => b["command_id"])).toContain("id1");
    expect(t.state().outbox.entries[0]).toMatchObject({ status: "sent" });
  });

  it("are dropped from the outbox when their command.result comes down the stream", async () => {
    const t = playing("queued");
    t.transport.on("/message", () => ({ status: 202, body: { receipt: "queued" } }), "POST");
    t.transport.last.send(...frames(t.fixture, 0, 0, 2));
    await t.state().send({ text: "then lint it" });
    await drain();
    expect(t.state().outbox.entries[0]).toMatchObject({ commandId: "id1", status: "sent" });
    t.transport.last.send({
      v: 2,
      type: "command.result",
      session: KEY,
      seq: 0,
      ts: 2,
      command_id: "id1",
      data: { op: "send", ok: true },
    });
    await drain();
    expect(t.state().outbox.entries).toEqual([]);
    expect(t.state().session.commands["id1"]?.result).toMatchObject({ ok: true });
  });

  it("are asked about again after every reconnect, whether or not the hub restarted", async () => {
    const t = playing("default");
    t.transport.on("/message", () => ({ status: 202, body: { receipt: "accepted" } }), "POST");
    const [snapshot, state] = frames(t.fixture) as [Extract<Frame, { type: "session.snapshot" }>, Frame];
    t.transport.last.send(snapshot, state);
    await t.state().send({ text: "x" });
    await drain();
    expect(t.transport.posts("/message")).toHaveLength(1);
    // The hub restarts: the reconnect gets a snapshot with a new boot id.
    t.timers.advance(10_000);
    t.transport.last.end({ kind: "closed" });
    t.transport.last.send({ ...snapshot, data: { ...snapshot.data, boot_id: "boot-2" } }, state);
    await drain();
    expect(t.transport.posts("/message").map((b) => b["command_id"])).toEqual(["id1", "id1"]);
    expect(t.state().session.bootId).toBe("boot-2");
  });

  it("are asked about again after a reconnect whose catch-up was a snapshot, which carries no results", async () => {
    const t = boot();
    t.transport.on("/message", () => ({ status: 202, body: { receipt: "accepted" } }), "POST");
    t.state().start();
    const snap = (head: number): Frame[] => {
      const body = loadFixture("default").snapshots[0]?.body as Snapshot;
      return [
        snapshotFrame({ ...body, head, boot_id: "b1" }),
        { v: 2, type: "session.state", session: KEY, seq: 0, ts: 1, data: { state: "idle", live: [], pending: [], head, epoch: 0 } },
      ];
    };
    t.transport.last.send(...snap(5));
    await t.state().send({ text: "hello" });
    await drain();
    expect(t.transport.posts("/message")).toHaveLength(1);
    expect(t.state().outbox.entries[0]?.status).toBe("sent");
    t.timers.advance(10_000);
    t.transport.last.end({ kind: "closed" });
    t.transport.last.send(...snap(900)); // the same boot, a wide gap: a snapshot with no command.result
    await drain();
    // Asked again under the same id; the hub's answer carries the stored result.
    expect(t.transport.posts("/message").map((b) => b["command_id"])).toEqual(["id1", "id1"]);
    t.transport.on("/message", () => ({ status: 202, body: { receipt: "queued", result: { ok: false, note: "the session is not running" } } }), "POST");
    t.timers.advance(10_000);
    t.transport.last.end({ kind: "closed" });
    t.transport.last.send(...snap(901));
    await drain();
    expect(t.state().outbox.entries[0]).toMatchObject({ status: "failed", error: "the session is not running" });
  });

  it("to a session no keeper holds are posted once, with client_id, and not again after any reconnect", async () => {
    const t = boot();
    // What the hub's other path answers: 202, no receipt, no de-duplication.
    t.transport.on("/message", () => ({ status: 202, body: { ok: true, queued: true, client_id: "id1", waiting: false } }), "POST");
    t.state().start();
    const body = loadFixture("default").snapshots[0]?.body as Snapshot;
    const plain: Snapshot = { ...body, caps: null, row: { key: KEY, control: "full" } };
    const catchUp = (head: number) =>
      t.transport.last.send(snapshotFrame({ ...plain, head }), { v: 2, type: "session.state", session: KEY, seq: 0, ts: 1, data: { state: "idle", live: [], pending: [], head, epoch: 0 } });
    catchUp(5);
    await drain();
    await t.state().send({ text: "rm -rf build" });
    await drain();
    for (let i = 0; i < 3; i++) {
      t.timers.advance(11_000);
      t.transport.last.end({ kind: "closed" });
      catchUp(5);
      await drain();
    }
    expect(t.transport.posts("/message")).toEqual([
      { text: "rm -rf build", mode: "queue", attachments: [], command_id: "id1", client_id: "id1" },
    ]);
    // Kept until its result: the 2xx only says the hub took it.
    expect(t.state().outbox.entries).toMatchObject([{ status: "sent", path: "legacy" }]);
  });

  it("a send acknowledged by a keeper is not asked about again once the session is no longer keeper-held", async () => {
    const t = boot();
    t.transport.on("/message", () => ({ status: 202, body: { receipt: "accepted" } }), "POST");
    t.state().start();
    const body = loadFixture("default").snapshots[0]?.body as Snapshot;
    const catchUp = (snap: Snapshot) =>
      t.transport.last.send(snapshotFrame(snap), { v: 2, type: "session.state", session: KEY, seq: 0, ts: 1, data: { state: "idle", live: [], pending: [], head: snap.head, epoch: 0 } });
    catchUp(body);
    await drain();
    await t.state().send({ text: "x" });
    await drain();
    expect(t.state().outbox.entries[0]).toMatchObject({ status: "sent", path: "keeper" });
    // Detached: the next catch-up says no keeper holds it.
    t.timers.advance(11_000);
    t.transport.last.end({ kind: "closed" });
    catchUp({ ...body, caps: null, row: { ...body.row, mode: "tmux" } as Snapshot["row"] });
    await drain();
    expect(t.transport.posts("/message")).toHaveLength(1);
    expect(t.state().outbox.entries[0]?.status).toBe("sent");
  });

  it("an empty or unknown command id in a result changes nothing", async () => {
    const t = boot();
    t.transport.on("/message", () => ({ status: 202, body: { receipt: "accepted" } }), "POST");
    t.state().start();
    t.transport.last.send(...frames(loadFixture("default"), 0, 0, 2));
    await t.state().send({ text: "x" });
    await drain();
    const result = (id: string) => ({ v: 2, type: "command.result", session: KEY, seq: 0, ts: 1, command_id: id, data: { op: "send", ok: true } });
    t.transport.last.send(result(""), result("someone-else"), { ...result("x"), command_id: undefined });
    await drain();
    expect(t.state().outbox.entries).toHaveLength(1);
    expect(t.state().session.commands[""]).toBeUndefined();
  });

  it("a send followed at once by stop() is still delivered after start()", async () => {
    const t = boot({ memory: true });
    t.transport.on("/message", () => ({ status: 202, body: { receipt: "accepted" } }), "POST");
    t.state().start();
    t.transport.last.send(...frames(loadFixture("default"), 0, 0, 2));
    await drain();
    const sent = t.state().send({ text: "hello" });
    t.state().stop();
    expect(await sent).toMatchObject({ ok: true });
    t.state().start();
    await drain();
    expect(t.transport.posts("/message").map((b) => b["text"])).toEqual(["hello"]);
    t.state().stop();
  });

  it("are refused on a session that is gone", async () => {
    const t = boot();
    t.state().start();
    t.transport.last.end({ kind: "http", status: 404 });
    expect(await t.state().send({ text: "x" })).toMatchObject({ ok: false, reason: "gone" });
  });

  it("report that they will not survive a reload when there is no IndexedDB", async () => {
    const t = boot({ memory: true });
    t.state().start();
    await drain();
    expect(t.state().outbox).toMatchObject({ persistent: false, note: "no database in tests" });
    const real = boot();
    real.state().start();
    await drain();
    expect(real.state().outbox).toMatchObject({ persistent: true, note: "" });
  });

  it("are kept when the store is stopped and started again with no database to read them from", async () => {
    const t = boot({ memory: true, online: false });
    t.transport.down = true;
    t.state().start();
    await t.state().send({ text: "typed offline" });
    expect(t.state().outbox.entries).toHaveLength(1);
    t.state().stop();
    t.state().start();
    await drain();
    expect(t.state().outbox.entries).toMatchObject([{ text: "typed offline", status: "waiting" }]);
  });

  it("a failed send is not sent again once the session is read-only", async () => {
    const t = playing("default");
    t.transport.on("/message", () => ({ status: 409, body: { error: { message: "no" } } }), "POST");
    const [snapshot, state] = frames(t.fixture) as [Extract<Frame, { type: "session.snapshot" }>, Frame];
    t.transport.last.send(snapshot, state);
    await t.state().send({ text: "x" });
    await drain();
    expect(t.state().outbox.entries[0]?.status).toBe("failed");
    t.transport.last.send({ v: 2, type: "session.row", session: KEY, ts: 1, data: { row: { key: KEY, control: "read_only", control_reason: "Started in a terminal" } } });
    expect(await t.state().retrySend("id1")).toMatchObject({ ok: false, reason: "read_only" });
    expect(t.state().outbox.entries).toHaveLength(1);
  });

  it("survive a reload of the page: a new store over the same database sends them", async () => {
    const factory = new IDBFactory();
    const transport = new FakeTransport();
    transport.down = true;
    const first = boot({ factory, transport });
    first.state().start();
    await first.state().send({ text: "before the reload" });
    await drain();
    first.state().stop();

    transport.down = false;
    transport.on("/message", () => ({ status: 202, body: { receipt: "accepted" } }), "POST");
    const second = boot({ factory, transport });
    second.state().start();
    transport.last.send(...frames(loadFixture("default"), 0, 0, 2));
    await drain();
    expect(transport.posts("/message").map((b) => [b["text"], b["command_id"]])).toContainEqual(["before the reload", "id1"]);
  });
});

describe("the send lifecycle through the store (review 3)", () => {
  const T = "local:term-1";
  const env = (type: string, data: unknown, extra: Record<string, unknown> = {}): Frame =>
    ({ v: 2, type, session: T, ts: 1, ...extra, data }) as unknown as Frame;
  const snap = (head: number, row: Record<string, unknown>, caps: unknown): Snapshot =>
    ({
      session: T, epoch: 0, head, boot_id: "b1", row: { key: T, ...row }, state: "idle", caps, open_turn: null,
      pending: [], queue: [], live: [], plan: null, tasks: [], checkpoints: [], changes: null, usage: null,
      messages: [], has_more: false, next_before: null,
    }) as Snapshot;
  const start = (timers = new FakeTimers()) => {
    const transport = new FakeTransport();
    let n = 0;
    const store = createSessionStore({
      sessionKey: T, transport, timers, random: () => 0, environment: new FakeEnvironment(),
      openStorage: async () => new MemoryStorage("mem"), newId: () => `id${++n}`,
    });
    store.getState().start();
    return { store, transport, timers };
  };
  const catchUp = (transport: FakeTransport, snapshot: Snapshot, state = "idle") =>
    transport.last.send(
      snapshotFrame(snapshot),
      env("session.state", { state, live: [], pending: [], head: snapshot.head, epoch: 0 }, { seq: 0 }),
    );

  it("legacy path: a failure the hub reports later, under the client_id, is shown on the send", async () => {
    const { store, transport } = start();
    transport.on("/message", () => ({ status: 202, body: { ok: true, queued: true, client_id: "id1", waiting: true } }), "POST");
    catchUp(transport, snap(5, {}, null), "running");
    await drain();
    await store.getState().send({ text: "hello" });
    await drain();
    expect(transport.posts("/message")).toHaveLength(1);
    expect(store.getState().outbox.entries).toMatchObject([{ status: "sent", path: "legacy" }]);
    // hub.py _queue_job: filed under client_id, which the v2 stream gives as command_id.
    transport.last.send(env("command.result", { op: "send", ok: false, note: "session stayed busy for 30 min; message not sent" }, { seq: 6, command_id: "id1" }));
    await drain();
    expect(store.getState().outbox.entries).toMatchObject([{ status: "failed", error: "session stayed busy for 30 min; message not sent" }]);
    expect(store.getState().sends).toMatchObject([{ shown: "failed", reason: "session stayed busy for 30 min; message not sent", canRetry: true }]);
    store.getState().stop();
  });

  it("a keeper-held session whose caps are still null: the receipt makes it the keeper path, kept until its result", async () => {
    const { store, transport } = start();
    transport.on("/message", () => ({ status: 202, body: { ok: true, queued: true, command_id: "id1", receipt: "accepted", waiting: false } }), "POST");
    catchUp(transport, snap(5, { mode: "keeper" }, null), "starting");
    await drain();
    await store.getState().send({ text: "hello" });
    await drain();
    expect(store.getState().outbox.entries).toMatchObject([{ status: "sent", path: "keeper", receipt: "accepted" }]);
    store.getState().stop();
  });

  it("a keeper-held session whose caps are still null: a lost answer is tried again, because the row says keeper", async () => {
    const { store, transport, timers } = start();
    let answered = 0;
    transport.on("/message", () => {
      answered++;
      if (answered === 1) throw new TypeError("lost");
      return { status: 202, body: { receipt: "duplicate" } };
    }, "POST");
    catchUp(transport, snap(5, { mode: "keeper" }, null), "starting");
    await drain();
    await store.getState().send({ text: "hello" });
    await drain();
    expect(store.getState().outbox.entries).toMatchObject([{ status: "waiting", ambiguous: true }]);
    timers.advance(500);
    await drain();
    expect(store.getState().outbox.entries).toMatchObject([{ status: "sent", path: "keeper" }]);
    store.getState().stop();
  });

  it("a send made before the row arrives waits, visibly, and fails after 24 hours if the hub keeps refusing", async () => {
    const timers = new FakeTimers();
    const { store, transport } = start(timers);
    transport.on("/message", () => ({ status: 202, body: { receipt: "accepted" } }), "POST");
    expect(await store.getState().send({ text: "hello" })).toMatchObject({ ok: true });
    await drain();
    expect(store.getState().sends).toMatchObject([{ shown: "waiting", reason: expect.stringContaining("Waiting for the hub") }]);
    transport.last.end({ kind: "http", status: 401 });
    await drain();
    expect(store.getState().connection.state).toBe("unauthorized");
    expect(store.getState().sends).toMatchObject([{ shown: "waiting", reason: expect.stringContaining("not signed in") }]);
    timers.advance(25 * 3600 * 1000);
    store.getState().wake();
    transport.last.end({ kind: "http", status: 401 });
    await drain();
    expect(transport.posts("/message")).toHaveLength(0);
    expect(store.getState().outbox.entries).toMatchObject([{ status: "failed", error: NOT_SENT_IN_TIME }]);
    store.getState().stop();
  });

  it("a session that is gone fails every open send, and a reloaded entry does not come back for ever", async () => {
    const timers = new FakeTimers();
    const factory = new IDBFactory();
    const run = () => {
      const transport = new FakeTransport();
      const store = createSessionStore({
        sessionKey: T, transport, timers, random: () => 0, environment: new FakeEnvironment(),
        openStorage: () => openOutboxStorage(factory), newId: () => "id1",
      });
      store.getState().start();
      return { store, transport };
    };
    const first = run();
    await first.store.getState().send({ text: "hello" });
    await drain();
    first.transport.last.end({ kind: "http", status: 404 });
    await drain();
    expect(first.store.getState().outbox.entries).toMatchObject([{ status: "failed", error: SESSION_GONE }]);
    first.store.getState().stop();
    // Reloaded: still shown as failed, until it is dismissed or a week has passed.
    timers.advance(TERMINAL_KEPT_MS);
    const second = run();
    await drain();
    expect(second.store.getState().outbox.entries).toEqual([]);
    second.store.getState().stop();
  });

  it("a hub too old for this view fails every open send", async () => {
    const { store, transport } = start();
    await store.getState().send({ text: "hello" });
    await drain();
    transport.last.send({ type: "session.status", data: {} });
    await drain();
    expect(store.getState().outbox.entries).toMatchObject([{ status: "failed", error: expect.stringContaining("too old") }]);
  });

  it("the sends selector gives every state a reason", async () => {
    const { store, transport } = start();
    let answer: (r: { status: number; body: unknown }) => void = () => {};
    transport.on("/message", () => new Promise((resolve) => (answer = resolve)), "POST");
    catchUp(transport, snap(5, { mode: "keeper" }, null));
    await drain();
    await store.getState().send({ text: "hello" });
    await drain();
    expect(store.getState().sends).toMatchObject([{ shown: "sending", reason: "Sending.", canDiscard: false }]);
    answer({ status: 202, body: { receipt: "queued" } });
    await drain();
    expect(store.getState().sends).toMatchObject([{ shown: "sent", reason: expect.stringContaining("Queued") }]);
    store.getState().stop();
  });
});

describe("reload or a second tab while a post is on its way (review 4)", () => {
  const T = "local:term-1";
  const env = (type: string, data: unknown, extra: Record<string, unknown> = {}): Frame =>
    ({ v: 2, type, session: T, ts: 1, ...extra, data }) as unknown as Frame;
  const snap = (head: number, row: Record<string, unknown>): Snapshot =>
    ({
      session: T, epoch: 0, head, boot_id: "b1", row: { key: T, ...row }, state: "idle", caps: null, open_turn: null,
      pending: [], queue: [], live: [], plan: null, tasks: [], checkpoints: [], changes: null, usage: null,
      messages: [], has_more: false, next_before: null,
    }) as Snapshot;
  const page = (factory: IDBFactory | null, timers: FakeTimers) => {
    const transport = new FakeTransport();
    const store = createSessionStore({
      sessionKey: T, transport, timers, random: () => 0, environment: new FakeEnvironment(),
      openStorage: factory === null ? async () => new MemoryStorage("m") : () => openOutboxStorage(factory), newId: () => "id1",
    });
    return { store, transport };
  };
  const catchUp = (transport: FakeTransport, row: Record<string, unknown>) =>
    transport.last.send(snapshotFrame(snap(5, row)), env("session.state", { state: "idle", live: [], pending: [], head: 5, epoch: 0 }, { seq: 0 }));

  for (const row of [{}, { mode: "keeper" }]) {
    const keeper = "mode" in row;
    it(`${keeper ? "a keeper" : "a legacy"} row: exactly one post per message, and a visible state`, async () => {
      const factory = new IDBFactory();
      const timers = new FakeTimers();
      const posted: Record<string, unknown>[] = [];
      const a = page(factory, timers);
      a.transport.on("/message", (_p, r) => {
        posted.push(r.body as Record<string, unknown>);
        return new Promise(() => {}); // the first page goes away before the answer
      }, "POST");
      a.store.getState().start();
      catchUp(a.transport, row);
      await drain();
      await a.store.getState().send({ text: "rm -rf build" });
      await drain();
      expect(posted).toHaveLength(1);
      // The page reloads (no stop()), or a second tab opens the same session.
      const b = page(factory, timers);
      b.transport.on("/message", (_p, r) => {
        posted.push(r.body as Record<string, unknown>);
        return { status: 202, body: keeper ? { receipt: "duplicate" } : { ok: true, queued: true } };
      }, "POST");
      b.store.getState().start();
      catchUp(b.transport, row);
      await drain();
      expect(posted).toHaveLength(1);
      expect(b.store.getState().sends[0]).toMatchObject({ shown: "waiting", reason: expect.stringContaining("another tab") });
      timers.advance(CLAIM_STALE_MS);
      await drain();
      if (keeper) {
        // The hub de-duplicates: posted once more under the same id, and it answers with the stored receipt.
        expect(posted.map((p) => p["command_id"])).toEqual(["id1", "id1"]);
        expect(b.store.getState().outbox.entries[0]).toMatchObject({ status: "sent", path: "keeper", receipt: "duplicate" });
      } else {
        expect(posted).toHaveLength(1);
        expect(b.store.getState().sends[0]).toMatchObject({ shown: "failed", reason: MAYBE_SENT });
      }
    });
  }

  it("stop() while a post is out: the answer reaches the restarted store, and nothing is posted twice", async () => {
    for (const factory of [new IDBFactory(), null]) {
      const timers = new FakeTimers();
      const a = page(factory, timers);
      let answer: (r: { status: number; body: unknown }) => void = () => {};
      let posts = 0;
      a.transport.on("/message", () => {
        posts++;
        return new Promise((resolve) => (answer = resolve));
      }, "POST");
      a.store.getState().start();
      catchUp(a.transport, {});
      await drain();
      await a.store.getState().send({ text: "hello" });
      await drain();
      a.store.getState().stop();
      a.store.getState().start();
      catchUp(a.transport, {});
      await drain();
      answer({ status: 202, body: { ok: true, queued: true } });
      await drain();
      expect(posts).toBe(1);
      expect(a.store.getState().outbox.entries, String(factory)).toMatchObject([{ status: "sent", path: "legacy" }]);
      a.store.getState().stop();
    }
  });

  it("a 404 that comes before the stored entries are loaded fails them", async () => {
    const factory = new IDBFactory();
    const timers = new FakeTimers();
    const a = page(factory, timers);
    a.store.getState().start();
    await a.store.getState().send({ text: "hello" });
    await drain();
    const b = page(factory, timers);
    b.store.getState().start();
    b.transport.last.end({ kind: "http", status: 404 });
    await drain();
    expect(b.store.getState().connection.state).toBe("gone");
    expect(b.store.getState().sends).toMatchObject([{ shown: "failed", reason: SESSION_GONE }]);
  });

  it("503 keeper_failed and then a detach: the retry is not posted to the legacy path", async () => {
    const timers = new FakeTimers();
    const a = page(null, timers);
    let n = 0;
    a.transport.on("/message", () => (++n === 1
      ? { status: 503, body: { error: { code: "keeper_failed", message: "the session's keeper did not answer" } } }
      : { status: 202, body: { ok: true, queued: true } }), "POST");
    a.store.getState().start();
    catchUp(a.transport, { mode: "keeper" });
    await drain();
    await a.store.getState().send({ text: "hello" });
    await drain();
    expect(a.store.getState().outbox.entries[0]).toMatchObject({ status: "waiting", ambiguous: true });
    a.transport.last.send(env("session.row", { row: { key: T } })); // detached: no mode
    await drain();
    timers.advance(1000);
    await drain();
    expect(n).toBe(1);
    expect(a.store.getState().outbox.entries[0]).toMatchObject({ status: "failed", error: MAYBE_SENT });
  });
});

describe("history", () => {
  it("loads the page before the oldest held, and stops when there is no more", async () => {
    const t = playing("default");
    const [snapshot, state] = frames(t.fixture) as [Extract<Frame, { type: "session.snapshot" }>, Frame];
    t.transport.last.send({ ...snapshot, data: { ...snapshot.data, has_more: true, next_before: 5 } } as Frame, state);
    const older = {
      session: KEY,
      messages: [{ v: 2, type: "message.completed", session: KEY, seq: 2, ts: 1, turn_id: "t0", data: { id: "o", kind: "user", text: "older" } }],
      has_more: false,
      next_before: null,
    };
    t.transport.on("/messages", () => ({ status: 200, body: older }));
    await t.state().loadOlder();
    expect(api(t)[0]?.path).toBe(`${BASE}/messages?v=2&before=5&limit=100`);
    expect(t.state().session.items[0]).toMatchObject({ seq: 2, text: "older" });
    expect(t.state().session.hasMore).toBe(false);
    await t.state().loadOlder();
    expect(api(t)).toHaveLength(1);
  });
});

describe("start, stop and wake", () => {
  it("stop() closes the stream and ignores what comes after; start() begins again", async () => {
    const t = playing("default");
    t.transport.last.send(...frames(t.fixture).slice(0, 2));
    const before = t.state().session.applied;
    const stream = t.transport.last;
    t.state().stop();
    expect(stream.closed).toBe(true);
    expect(t.state().started).toBe(false);
    stream.send(frames(t.fixture)[2] as Frame);
    expect(t.state().session.applied).toBe(before);
    expect(t.timers.pending).toBe(0);
    t.state().start();
    expect(t.transport.streams).toHaveLength(2);
    expect(t.transport.last.query).toEqual({ v: "2", last: String(t.state().session.cursor), epoch: "0" });
  });

  it("start() twice is one connection", () => {
    const t = boot();
    t.state().start();
    t.state().start();
    expect(t.transport.streams).toHaveLength(1);
  });

  it("wake() makes a connection that is backing off try now", async () => {
    const t = boot();
    t.state().start();
    t.transport.last.end({ kind: "error", message: "refused" });
    expect(t.transport.streams).toHaveLength(1);
    t.state().wake();
    expect(t.transport.streams).toHaveLength(2);
  });

  it("applies a snapshot frame like any other, and exposes the hook's selector input", () => {
    const t = boot();
    t.state().start();
    const body = loadFixture("default").snapshots[0]?.body as Snapshot;
    t.transport.last.send(snapshotFrame(body));
    expect(t.state().session.session).toBe(KEY);
  });
});
