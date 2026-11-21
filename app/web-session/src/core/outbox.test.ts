import { IDBFactory } from "fake-indexeddb";
import { describe, expect, it } from "vitest";

import { Outbox, OUTBOX_TTL_MS, type OutboxView } from "./outbox";
import {
  DeferredStorage,
  FallbackStorage,
  MemoryStorage,
  openOutboxStorage,
  type OutboxEntry,
  type OutboxStorage,
} from "./outbox-storage";
import { FakeTimers, FakeTransport } from "./test-support";
import type { JsonRequest, JsonResponse } from "./transport";

const KEY = "claude:t1";
const MESSAGE = "/v1/sessions/claude%3At1/message";

/** A hub that applies a command once and answers a repeat with the stored receipt. */
class FakeHub {
  applied: string[] = [];
  received: string[] = [];
  results = new Map<string, { ok: boolean; note?: string; cancelled?: boolean }>();
  /** Lose the response of the next n POSTs after applying them. */
  loseResponses = 0;
  status: number | null = null;

  handler = (_path: string, request: JsonRequest): JsonResponse => {
    const body = request.body as { command_id: string; text: string };
    this.received.push(body.command_id);
    if (this.status !== null) return { status: this.status, body: { error: { code: "x", message: `refused ${this.status}` } } };
    const duplicate = this.applied.includes(body.command_id);
    if (!duplicate) this.applied.push(body.command_id);
    if (this.loseResponses > 0) {
      this.loseResponses--;
      throw new TypeError("the response never arrived");
    }
    const result = this.results.get(body.command_id);
    return {
      status: 202,
      body: {
        ok: true,
        queued: true,
        command_id: body.command_id,
        receipt: duplicate ? "queued" : "accepted",
        waiting: false,
        ...(result === undefined ? {} : { result }),
      },
    };
  };
}

function setup(over: { factory?: IDBFactory; storage?: OutboxStorage; hub?: FakeHub; random?: () => number } = {}) {
  const timers = new FakeTimers();
  const transport = new FakeTransport();
  const hub = over.hub ?? new FakeHub();
  transport.on("/message", async (path, request) => hub.handler(path, request), "POST");
  const factory = over.factory ?? new IDBFactory();
  let ids = 0;
  const views: OutboxView[] = [];
  const make = async (storage?: OutboxStorage, newId?: () => string) => {
    const outbox = new Outbox({
      session: KEY,
      storage: storage ?? over.storage ?? (await openOutboxStorage(factory)),
      transport,
      timers,
      random: over.random ?? (() => 0.5),
      newId: newId ?? (() => `c${++ids}`),
      onChange: (view) => views.push(view),
    });
    await outbox.init();
    return outbox;
  };
  return { timers, transport, hub, factory, views, make, view: () => views[views.length - 1] as OutboxView };
}

const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
/** Let the outbox's promises run: nothing waits on a clock here, only on the microtask queue. */
async function drain(_outbox?: Outbox) {
  for (let i = 0; i < 4; i++) await settle();
}

const stored = async (factory: IDBFactory): Promise<OutboxEntry[]> => (await openOutboxStorage(factory)).load(KEY);

describe("sending", () => {
  it("gives a send its command id once, posts it with that id, and keeps it as sent after the receipt", async () => {
    const t = setup();
    const outbox = await t.make();
    const entry = await outbox.send({ text: "hello", attachments: ["a.png"], mode: "steer" });
    await drain(outbox);
    expect(t.transport.posts("/message")).toEqual([
      { text: "hello", mode: "steer", attachments: ["a.png"], command_id: entry.commandId },
    ]);
    expect(t.transport.calls[0]?.path).toBe(MESSAGE);
    expect(t.view().entries).toMatchObject([{ commandId: entry.commandId, status: "sent", receipt: "accepted", attempts: 1 }]);
    expect(await stored(t.factory)).toMatchObject([{ commandId: entry.commandId, status: "sent" }]);
  });

  it("writes the entry to the database before the first attempt", async () => {
    const t = setup();
    const outbox = await t.make();
    // The hub never answers: the entry is on disk anyway.
    t.transport.on("/message", () => new Promise<JsonResponse>(() => {}), "POST");
    const entry = await outbox.send({ text: "typed" });
    expect(await stored(t.factory)).toMatchObject([{ commandId: entry.commandId, status: "pending", text: "typed" }]);
  });

  it("sends in the order typed", async () => {
    const t = setup();
    const outbox = await t.make();
    await outbox.send({ text: "one" });
    await outbox.send({ text: "two" });
    await outbox.send({ text: "three" });
    await drain(outbox);
    expect(t.transport.posts("/message").map((b) => b["text"])).toEqual(["one", "two", "three"]);
  });

  it("folds a second flush into the one running: one post per send", async () => {
    const t = setup();
    const outbox = await t.make();
    await outbox.send({ text: "one" });
    await Promise.all([outbox.flush(), outbox.flush(), outbox.flush()]);
    await settle();
    expect(t.hub.received).toEqual(["c1"]);
  });
});

describe("retrying until a receipt arrives", () => {
  it("retries with backoff under the same command id", async () => {
    const t = setup({ random: () => 0 });
    const outbox = await t.make();
    t.transport.down = true;
    const entry = await outbox.send({ text: "hello" });
    await drain(outbox);
    expect(t.view().entries[0]).toMatchObject({ status: "pending", attempts: 1 });
    // First wait 500 ms, then 1 s, then 2 s.
    for (const wait of [500, 1000, 2000]) {
      t.timers.advance(wait - 1);
      await settle();
      const before = t.transport.calls.length;
      t.timers.advance(1);
      await settle();
      expect(t.transport.calls.length).toBe(before + 1);
    }
    expect(t.view().entries[0]?.attempts).toBe(4);
    t.transport.down = false;
    t.timers.advance(4000);
    await settle();
    await settle();
    expect(t.view().entries[0]).toMatchObject({ status: "sent", receipt: "accepted", attempts: 5 });
    expect(new Set(t.hub.received)).toEqual(new Set([entry.commandId]));
    expect(t.timers.pending).toBe(0);
  });

  it("retries a server error and a throttle, and stops at once on a refusal", async () => {
    for (const status of [500, 502, 503, 429, 408]) {
      const t = setup();
      const outbox = await t.make();
      t.hub.status = status;
      await outbox.send({ text: "x" });
      await drain(outbox);
      expect(t.view().entries[0]?.status, String(status)).toBe("pending");
      expect(t.timers.pending, String(status)).toBe(1);
    }
    for (const status of [400, 409, 422]) {
      const t = setup();
      const outbox = await t.make();
      t.hub.status = status;
      await outbox.send({ text: "x" });
      await drain(outbox);
      expect(t.view().entries[0], String(status)).toMatchObject({ status: "failed", error: `refused ${status}` });
      expect(t.timers.pending).toBe(0);
    }
  });

  it("fails a send to a session that is gone", async () => {
    const t = setup();
    const outbox = await t.make();
    t.hub.status = 404;
    await outbox.send({ text: "x" });
    await drain(outbox);
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: "This session no longer exists" });
  });

  it("does not let a send behind it jump the queue while the first cannot go", async () => {
    const t = setup();
    const outbox = await t.make();
    t.transport.down = true;
    await outbox.send({ text: "first" });
    await outbox.send({ text: "second" });
    await drain(outbox);
    expect(t.transport.calls.map((c) => (c.request.body as { text: string }).text)).toEqual(["first"]);
    t.transport.down = false;
    t.timers.advance(60_000);
    await settle();
    await settle();
    expect(t.transport.posts("/message").map((b) => b["text"])).toEqual(["first", "first", "second"]);
  });

  it("pauses on 401 and sends nothing until wake()", async () => {
    const t = setup();
    const outbox = await t.make();
    t.hub.status = 401;
    await outbox.send({ text: "x" });
    await drain(outbox);
    expect(t.view().paused).toBe("unauthorized");
    expect(t.view().entries[0]?.status).toBe("pending");
    expect(t.timers.pending).toBe(0);
    await outbox.send({ text: "y" });
    await drain(outbox);
    expect(t.hub.received).toEqual(["c1"]);
    t.hub.status = null;
    outbox.wake();
    await settle();
    await settle();
    expect(t.view().paused).toBeNull();
    expect(t.view().entries.map((e) => e.status)).toEqual(["sent", "sent"]);
  });

  it("wake() skips the wait", async () => {
    const t = setup();
    const outbox = await t.make();
    t.transport.down = true;
    await outbox.send({ text: "x" });
    await drain(outbox);
    t.transport.down = false;
    outbox.wake();
    await settle();
    await settle();
    expect(t.view().entries[0]?.status).toBe("sent");
    expect(t.timers.pending).toBe(0);
  });
});

describe("never applied twice", () => {
  it("answers a retry with the stored receipt when the first response was lost", async () => {
    const t = setup({ random: () => 0 });
    const outbox = await t.make();
    t.hub.loseResponses = 1;
    const entry = await outbox.send({ text: "once" });
    await drain(outbox);
    expect(t.view().entries[0]?.status).toBe("pending");
    t.timers.advance(500);
    await settle();
    await settle();
    // Two posts reached the hub; it applied the command once and answered the second with its receipt.
    expect(t.hub.received).toEqual([entry.commandId, entry.commandId]);
    expect(t.hub.applied).toEqual([entry.commandId]);
    expect(t.view().entries[0]).toMatchObject({ status: "sent", receipt: "queued" });
  });

  it("takes a duplicate answer that carries the stored result as the end of the send", async () => {
    const t = setup({ random: () => 0 });
    const outbox = await t.make();
    t.hub.loseResponses = 1;
    t.hub.results.set("c1", { ok: true });
    await outbox.send({ text: "done already" });
    await drain(outbox);
    t.timers.advance(500);
    await settle();
    await settle();
    expect(t.view().entries).toEqual([]);
    expect(await stored(t.factory)).toEqual([]);
  });

  it("fails the send when the stored result says the hub could not deliver it", async () => {
    const t = setup({ random: () => 0 });
    const outbox = await t.make();
    t.hub.results.set("c1", { ok: false, note: "the session is not running" });
    await outbox.send({ text: "x" });
    await drain(outbox);
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: "the session is not running" });
  });
});

describe("surviving a reload", () => {
  it("sends what an earlier page left in IndexedDB, under the id it had", async () => {
    const t = setup();
    const before = await t.make();
    t.transport.down = true;
    const entry = await before.send({ text: "typed offline" });
    await drain(before);
    before.close(); // the page goes away

    t.transport.down = false;
    t.transport.calls.length = 0;
    const after = await t.make();
    await settle();
    await settle();
    expect(t.transport.posts("/message")).toEqual([
      { text: "typed offline", mode: "queue", attachments: [], command_id: entry.commandId },
    ]);
    expect(t.view().entries[0]).toMatchObject({ commandId: entry.commandId, status: "sent" });
    after.close();
  });

  it("asks again about a send the hub had acknowledged, and takes the stored result", async () => {
    const t = setup();
    const before = await t.make();
    const entry = await before.send({ text: "acknowledged" });
    await drain(before);
    expect(t.view().entries[0]?.status).toBe("sent");
    before.close();

    t.hub.results.set(entry.commandId, { ok: true });
    await t.make();
    await settle();
    await settle();
    expect(t.hub.received).toEqual([entry.commandId, entry.commandId]);
    expect(t.hub.applied).toEqual([entry.commandId]);
    expect(t.view().entries).toEqual([]);
    expect(await stored(t.factory)).toEqual([]);
  });

  it("keeps another session's sends out of this one", async () => {
    const t = setup();
    const storage = await openOutboxStorage(t.factory);
    await storage.put({ commandId: "other", session: "codex:x", text: "no", attachments: [], mode: "queue", createdAt: 1, status: "pending", attempts: 0 });
    await t.make();
    expect(t.view().entries).toEqual([]);
  });
});

describe("results", () => {
  const sent = async () => {
    const t = setup();
    const outbox = await t.make();
    const entry = await outbox.send({ text: "x" });
    await drain(outbox);
    return { t, outbox, entry };
  };

  it("removes a send whose result is ok, from memory and from disk", async () => {
    const { t, outbox, entry } = await sent();
    outbox.noteResult(entry.commandId, { ok: true });
    await settle();
    expect(t.view().entries).toEqual([]);
    expect(await stored(t.factory)).toEqual([]);
  });

  it("removes a send that was cancelled (taken out of the queue, or steered)", async () => {
    const { t, outbox, entry } = await sent();
    outbox.noteResult(entry.commandId, { ok: false, cancelled: true, note: "removed from the queue" });
    await settle();
    expect(t.view().entries).toEqual([]);
  });

  it("keeps a send the hub could not deliver, with the reason", async () => {
    const { t, outbox, entry } = await sent();
    outbox.noteResult(entry.commandId, { ok: false, note: "the hub restarted before this was delivered" });
    await settle();
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: "the hub restarted before this was delivered" });
    expect(await stored(t.factory)).toMatchObject([{ status: "failed" }]);
  });

  it("ignores a result for a command it does not hold", async () => {
    const { t, outbox } = await sent();
    outbox.noteResult("someone-else", { ok: false });
    expect(t.view().entries).toHaveLength(1);
  });

  it("when the result beats the response, the entry is not written back", async () => {
    const t = setup();
    const outbox = await t.make();
    let answer: (r: JsonResponse) => void = () => {};
    t.transport.on("/message", () => new Promise<JsonResponse>((r) => (answer = r)), "POST");
    const entry = await outbox.send({ text: "x" });
    await settle();
    outbox.noteResult(entry.commandId, { ok: true });
    await settle();
    answer({ status: 202, body: { receipt: "accepted" } });
    await settle();
    await settle();
    expect(t.view().entries).toEqual([]);
    expect(await stored(t.factory)).toEqual([]);
  });

  it("asks again about every acknowledged send when the hub restarts, under the same ids", async () => {
    const { t, outbox, entry } = await sent();
    const second = await outbox.send({ text: "y" });
    await drain(outbox);
    t.hub.received.length = 0;
    outbox.hubRestarted();
    await settle();
    await settle();
    expect(t.hub.received).toEqual([entry.commandId, second.commandId]);
    expect(t.hub.applied).toEqual([entry.commandId, second.commandId]);
  });
});

describe("failed sends", () => {
  it("can be dismissed, or sent again as a new message", async () => {
    const t = setup();
    const outbox = await t.make();
    t.hub.status = 409;
    const entry = await outbox.send({ text: "again please", attachments: ["f"], mode: "steer" });
    await drain(outbox);
    expect(t.view().entries[0]?.status).toBe("failed");
    t.hub.status = null;
    const again = await outbox.retry(entry.commandId);
    await drain(outbox);
    expect(again?.commandId).not.toBe(entry.commandId);
    expect(t.hub.applied).toEqual([again?.commandId]);
    expect(t.view().entries.map((e) => [e.text, e.mode, e.attachments])).toEqual([["again please", "steer", ["f"]]]);
    expect(await outbox.retry("nope")).toBeUndefined();
    await outbox.discard(again?.commandId ?? "");
    expect(t.view().entries).toEqual([]);
    expect(await stored(t.factory)).toEqual([]);
  });
});

describe("a send the hub never took", () => {
  it("is failed after 24 hours, the time the hub holds a message for a sleeping machine", async () => {
    const t = setup();
    const outbox = await t.make();
    t.transport.down = true;
    await outbox.send({ text: "old" });
    await drain(outbox);
    t.timers.skip(OUTBOX_TTL_MS + 1000);
    outbox.wake();
    await settle();
    await settle();
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: expect.stringContaining("24 hours") });
  });

  it("forgets an acknowledged send whose result never came, after 24 hours", async () => {
    const t = setup();
    const outbox = await t.make();
    await outbox.send({ text: "old" });
    await drain(outbox);
    t.timers.skip(OUTBOX_TTL_MS + 1000);
    outbox.wake();
    await settle();
    await settle();
    expect(t.view().entries).toEqual([]);
  });
});

describe("without IndexedDB", () => {
  it("works in memory and says so when it is missing", async () => {
    const notes: string[] = [];
    const storage = await openOutboxStorage(undefined, (note) => notes.push(note));
    expect(storage.kind).toBe("memory");
    expect(storage.note).toMatch(/not available.*memory.*reload/);
    expect(notes).toEqual([storage.note]);

    const t = setup({ storage });
    const outbox = await t.make();
    const entry = await outbox.send({ text: "hi" });
    await drain(outbox);
    expect(t.view()).toMatchObject({ persistent: false, note: storage.note });
    expect(t.view().entries[0]).toMatchObject({ commandId: entry.commandId, status: "sent" });
    // And a reload has nothing: that is what the note says.
    expect(await storage.load(KEY)).toEqual([]);
  });

  it("works in memory when opening the database throws", async () => {
    const throwing = {
      open() {
        throw new DOMException("The operation is insecure.", "SecurityError");
      },
    } as unknown as IDBFactory;
    const storage = await openOutboxStorage(throwing);
    expect(storage.kind).toBe("memory");
    expect(storage.note).toContain("The operation is insecure.");
    const t = setup({ storage });
    const outbox = await t.make();
    await outbox.send({ text: "hi" });
    await drain(outbox);
    expect(t.view().entries[0]?.status).toBe("sent");
    expect(t.view().persistent).toBe(false);
  });

  it("works in memory when opening the database fails", async () => {
    const failing = {
      open() {
        const request = {} as { error: Error; onerror?: (event: Event) => void };
        queueMicrotask(() => {
          request.error = new Error("QuotaExceededError");
          request.onerror?.(new Event("error"));
        });
        return request as unknown as IDBOpenDBRequest;
      },
    } as unknown as IDBFactory;
    const storage = await openOutboxStorage(failing);
    expect(storage.kind).toBe("memory");
    expect(storage.note).toContain("QuotaExceededError");
  });

  it("turns to memory when the database fails after it opened, and says why", async () => {
    const broken: OutboxStorage = {
      kind: "indexeddb",
      note: "",
      async load() {
        return [];
      },
      async put() {
        throw new Error("the disk is full");
      },
      async remove() {},
      close() {},
    };
    const notes: string[] = [];
    const storage = new FallbackStorage(broken, (note) => notes.push(note));
    const t = setup({ storage });
    const outbox = await t.make();
    expect(t.view().persistent).toBe(true);
    await outbox.send({ text: "hi" });
    await drain(outbox);
    expect(storage.kind).toBe("memory");
    expect(notes).toHaveLength(1);
    expect(notes[0]).toContain("the disk is full");
    expect(t.view()).toMatchObject({ persistent: false, note: notes[0] });
    // The send still went.
    expect(t.view().entries[0]?.status).toBe("sent");
  });

  it("a deferred storage reports memory until the database is open, then IndexedDB", async () => {
    let open: (s: OutboxStorage) => void = () => {};
    const storage = new DeferredStorage(new Promise<OutboxStorage>((resolve) => (open = resolve)));
    expect(storage.kind).toBe("memory");
    const loading = storage.load(KEY);
    open(await openOutboxStorage(new IDBFactory()));
    expect(await loading).toEqual([]);
    expect(storage.kind).toBe("indexeddb");
    storage.close();
  });

  it("MemoryStorage keeps nothing", async () => {
    const memory = new MemoryStorage("because");
    await memory.put();
    expect(await memory.load()).toEqual([]);
    expect(memory.note).toBe("because");
  });
});
