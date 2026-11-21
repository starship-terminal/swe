// The send lifecycle, row by row (docs/specs/G11-client-core.md, "The send lifecycle").
import { IDBFactory } from "fake-indexeddb";
import { describe, expect, it } from "vitest";

import {
  KEEPER_UNCONFIRMED,
  LEGACY_CONFIRM_MS,
  LEGACY_UNCONFIRMED,
  MAYBE_SENT,
  NOT_SENT_IN_TIME,
  Outbox,
  OUTBOX_TTL_MS,
  SESSION_GONE,
  TERMINAL_KEPT_MS,
  type OutboxView,
} from "./outbox";
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

/**
 * A hub with both of its paths. `keeper`: applies a command once, answers a repeat with the stored
 * receipt and (once there is one) the result. `legacy`: answers 202 with no receipt and types the
 * message in again for every post (hub.py `_send_message`).
 */
class FakeHub {
  path: "keeper" | "legacy" = "keeper";
  applied: string[] = [];
  received: Record<string, unknown>[] = [];
  results = new Map<string, { ok: boolean; note?: string; cancelled?: boolean }>();
  /** Apply the next n posts and lose their answer. */
  loseAnswers = 0;
  status: number | null = null;

  handler = (_path: string, request: JsonRequest): JsonResponse => {
    const body = request.body as { command_id: string; client_id: string };
    this.received.push(body);
    if (this.status !== null) return { status: this.status, body: { error: { code: "x", message: `refused ${this.status}` } } };
    const repeat = this.applied.includes(body.command_id);
    if (this.path === "legacy" || !repeat) this.applied.push(body.client_id);
    if (this.loseAnswers > 0) {
      this.loseAnswers--;
      throw new TypeError("the answer never arrived");
    }
    if (this.path === "legacy") return { status: 202, body: { ok: true, queued: true, client_id: body.client_id, waiting: false } };
    const result = this.results.get(body.command_id);
    return {
      status: 202,
      body: { ok: true, queued: true, command_id: body.command_id, receipt: repeat ? "queued" : "accepted", ...(result ? { result } : {}) },
    };
  };
}

function setup(over: { factory?: IDBFactory; storage?: OutboxStorage; random?: () => number } = {}) {
  const timers = new FakeTimers();
  const transport = new FakeTransport();
  const hub = new FakeHub();
  transport.on("/message", async (path, request) => hub.handler(path, request), "POST");
  const factory = over.factory ?? new IDBFactory();
  const world = { keeper: true, canPost: true };
  let ids = 0;
  const views: OutboxView[] = [];
  const make = async (storage?: OutboxStorage, initial?: OutboxEntry[]) => {
    const outbox = new Outbox({
      session: KEY,
      storage: storage ?? over.storage ?? (await openOutboxStorage(factory)),
      transport,
      timers,
      random: over.random ?? (() => 0.5),
      newId: () => `c${++ids}`,
      canPost: () => world.canPost,
      keeperHeld: () => world.keeper,
      ...(initial ? { initial } : {}),
      onChange: (view) => views.push(view),
    });
    await outbox.init();
    return outbox;
  };
  return { timers, transport, hub, factory, world, views, make, view: () => views[views.length - 1] as OutboxView };
}

const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
async function drain() {
  for (let i = 0; i < 5; i++) await settle();
}
const stored = async (factory: IDBFactory): Promise<OutboxEntry[]> => (await openOutboxStorage(factory)).load(KEY);
const statuses = (t: ReturnType<typeof setup>) => t.view().entries.map((e) => e.status);

/** A legacy hub: no receipt, no de-duplication, and a row that says no keeper holds the session. */
function legacy(t: ReturnType<typeof setup>) {
  t.hub.path = "legacy";
  t.world.keeper = false;
}

describe("a new send", () => {
  it("is waiting, on disk, before anything is posted", async () => {
    const t = setup();
    t.world.canPost = false;
    const outbox = await t.make();
    const entry = await outbox.send({ text: "typed" });
    expect(entry).toMatchObject({ status: "waiting", attempts: 0 });
    expect(await stored(t.factory)).toMatchObject([{ commandId: entry.commandId, status: "waiting" }]);
    expect(t.transport.calls).toEqual([]);
  });

  it("is posted once, with one id as command_id and as client_id", async () => {
    const t = setup();
    const outbox = await t.make();
    const entry = await outbox.send({ text: "hello", attachments: ["a.png"], mode: "steer" });
    await drain();
    expect(t.transport.posts("/message")).toEqual([
      { text: "hello", mode: "steer", attachments: ["a.png"], command_id: entry.commandId, client_id: entry.commandId },
    ]);
    expect(t.transport.calls[0]?.path).toBe(MESSAGE);
  });

  it("is not posted until its first write has finished", async () => {
    const t = setup();
    const inner = await openOutboxStorage(t.factory);
    let finish: () => void = () => {};
    const slow: OutboxStorage = {
      get kind() {
        return inner.kind;
      },
      get note() {
        return inner.note;
      },
      load: (session) => inner.load(session),
      put: (entry) => new Promise<void>((resolve) => (finish = () => void inner.put(entry).then(resolve))),
      remove: (id) => inner.remove(id),
      close: () => inner.close(),
    };
    const outbox = await t.make(slow);
    const sending = outbox.send({ text: "x" });
    outbox.wake();
    await drain();
    expect(t.transport.calls).toEqual([]);
    finish();
    await sending;
    await drain();
    expect(t.hub.received).toHaveLength(1);
  });

  it("goes in the order typed, one at a time", async () => {
    const t = setup();
    const outbox = await t.make();
    await outbox.send({ text: "one" });
    await outbox.send({ text: "two" });
    await outbox.send({ text: "three" });
    await drain();
    expect(t.transport.posts("/message").map((b) => b["text"])).toEqual(["one", "two", "three"]);
    await Promise.all([outbox.flush(), outbox.flush()]);
    expect(t.hub.received).toHaveLength(3);
  });
});

describe("the hub's answer decides the path", () => {
  it("a 2xx with a receipt: sent on the keeper path", async () => {
    const t = setup();
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "sent", path: "keeper", receipt: "accepted", sentAt: t.timers.now() });
  });

  it("a 2xx without a receipt: sent on the legacy path, and not removed", async () => {
    const t = setup();
    legacy(t);
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "sent", path: "legacy" });
    expect(await stored(t.factory)).toMatchObject([{ status: "sent", path: "legacy" }]);
  });

  it("a keeper receipt on a session whose caps are not known yet is still the keeper path", async () => {
    // The route is the hub's answer, not a guess: here the row says nothing about a keeper.
    const t = setup();
    t.world.keeper = false;
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "sent", path: "keeper" });
  });

  it("a 2xx carrying the stored result settles it at once", async () => {
    const t = setup();
    t.hub.results.set("c1", { ok: true });
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    expect(t.view().entries).toEqual([]);
    t.hub.results.set("c2", { ok: false, note: "the session is not running" });
    await outbox.send({ text: "y" });
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: "the session is not running" });
  });
});

describe("results", () => {
  const sentOn = async (path: "keeper" | "legacy") => {
    const t = setup();
    if (path === "legacy") legacy(t);
    const outbox = await t.make();
    const entry = await outbox.send({ text: "x" });
    await drain();
    return { t, outbox, entry };
  };

  for (const path of ["keeper", "legacy"] as const) {
    it(`${path}: an ok result removes it, from memory and disk`, async () => {
      const { t, outbox, entry } = await sentOn(path);
      outbox.noteResult(entry.commandId, { ok: true });
      await drain();
      expect(t.view().entries).toEqual([]);
      expect(await stored(t.factory)).toEqual([]);
    });

    it(`${path}: a failed result fails it with the hub's note`, async () => {
      const { t, outbox, entry } = await sentOn(path);
      outbox.noteResult(entry.commandId, { ok: false, note: "session stayed busy for 30 min; message not sent" });
      await drain();
      expect(t.view().entries[0]).toMatchObject({ status: "failed", error: "session stayed busy for 30 min; message not sent" });
    });
  }

  it("a result cancelled by a queue action (removed, or steered) removes it", async () => {
    const { t, outbox, entry } = await sentOn("keeper");
    outbox.noteResult(entry.commandId, { ok: false, cancelled: true, note: "removed from the queue" });
    await drain();
    expect(t.view().entries).toEqual([]);
  });

  it("an empty or unknown id changes nothing", async () => {
    const { t, outbox } = await sentOn("keeper");
    outbox.noteResult("", { ok: true });
    outbox.noteResult("someone-else", { ok: false });
    await drain();
    expect(statuses(t)).toEqual(["sent"]);
  });

  it("a result that comes before the answer wins over it", async () => {
    for (const result of [{ ok: true }, { ok: false, note: "no" }]) {
      const t = setup();
      let answer: (r: JsonResponse) => void = () => {};
      t.transport.on("/message", () => new Promise<JsonResponse>((r) => (answer = r)), "POST");
      const outbox = await t.make();
      const entry = await outbox.send({ text: "x" });
      await settle();
      outbox.noteResult(entry.commandId, result);
      await drain();
      answer({ status: 202, body: { receipt: "accepted" } });
      await drain();
      if (result.ok) expect(t.view().entries).toEqual([]);
      else expect(t.view().entries[0]).toMatchObject({ status: "failed", error: "no" });
      expect((await stored(t.factory)).length).toBe(result.ok ? 0 : 1);
    }
  });

  it("an ok result for a send that ended unconfirmed or may-not-have-been-sent removes it: it did arrive", async () => {
    const { t, outbox, entry } = await sentOn("legacy");
    t.timers.advance(LEGACY_CONFIRM_MS);
    await drain();
    expect(statuses(t)).toEqual(["unconfirmed"]);
    outbox.noteResult(entry.commandId, { ok: true });
    await drain();
    expect(t.view().entries).toEqual([]);
  });
});

describe("answers that are not 2xx", () => {
  it("no answer, keeper session: tried again under the same id, and the hub applies it once", async () => {
    const t = setup({ random: () => 0 });
    t.hub.loseAnswers = 1;
    const outbox = await t.make();
    const entry = await outbox.send({ text: "once" });
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "waiting", ambiguous: true, attempts: 1 });
    t.timers.advance(500);
    await drain();
    expect(t.hub.received.map((b) => b["command_id"])).toEqual([entry.commandId, entry.commandId]);
    expect(t.hub.applied).toEqual([entry.commandId]);
    expect(t.view().entries[0]).toMatchObject({ status: "sent", receipt: "queued" });
  });

  it("no answer, any other session: failed, may or may not have been sent, never tried again", async () => {
    const t = setup();
    legacy(t);
    t.transport.down = true;
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: MAYBE_SENT });
    t.transport.down = false;
    t.timers.advance(60_000);
    await drain();
    expect(t.transport.calls).toHaveLength(1);
  });

  it("408 and a 5xx that may have been applied: the same as no answer", async () => {
    for (const status of [408, 500, 502, 504]) {
      const t = setup();
      legacy(t);
      t.hub.status = status;
      const outbox = await t.make();
      await outbox.send({ text: "x" });
      await drain();
      expect(t.view().entries[0], String(status)).toMatchObject({ status: "failed", error: MAYBE_SENT });

      const k = setup();
      k.hub.status = status;
      const kept = await k.make();
      await kept.send({ text: "x" });
      await drain();
      expect(k.view().entries[0], String(status)).toMatchObject({ status: "waiting", ambiguous: true });
    }
  });

  it("429, 425 and 503 say not now before anything is queued: tried again on any path", async () => {
    for (const status of [429, 425, 503]) {
      const t = setup();
      legacy(t);
      t.hub.status = status;
      const outbox = await t.make();
      await outbox.send({ text: "x" });
      await drain();
      expect(t.view().entries[0], String(status)).toMatchObject({ status: "waiting", lastError: `refused ${status}` });
      expect(t.view().entries[0]?.ambiguous).toBeUndefined();
      expect(t.timers.pending).toBeGreaterThan(0);
    }
  });

  it("a repeat after an unanswered post fails instead once the row no longer says keeper", async () => {
    const t = setup({ random: () => 0 });
    t.transport.down = true;
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "waiting", ambiguous: true });
    t.world.keeper = false; // detached meanwhile
    t.transport.down = false;
    t.timers.advance(500);
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: MAYBE_SENT });
    expect(t.transport.calls).toHaveLength(1);
  });

  it("401 and 403: kept waiting and paused until wake()", async () => {
    for (const status of [401, 403]) {
      const t = setup();
      t.hub.status = status;
      const outbox = await t.make();
      await outbox.send({ text: "x" });
      await outbox.send({ text: "y" });
      await drain();
      expect(t.view().paused).toBe("unauthorized");
      expect(statuses(t)).toEqual(["waiting", "waiting"]);
      expect(t.hub.received).toHaveLength(1);
      t.hub.status = null;
      outbox.wake();
      await drain();
      expect(statuses(t)).toEqual(["sent", "sent"]);
    }
  });

  it("404: failed, the session no longer exists; another 4xx: failed with the hub's words", async () => {
    for (const [status, error] of [
      [404, SESSION_GONE],
      [409, "refused 409"],
      [422, "refused 422"],
    ] as const) {
      const t = setup();
      t.hub.status = status;
      const outbox = await t.make();
      await outbox.send({ text: "x" });
      await drain();
      expect(t.view().entries[0], String(status)).toMatchObject({ status: "failed", error });
      expect(t.timers.pending).toBe(1); // only the deadline timer
    }
  });

  it("backs off between repeats, and holds the sends behind the one that cannot go", async () => {
    const t = setup({ random: () => 0 });
    t.hub.status = 503;
    const outbox = await t.make();
    await outbox.send({ text: "first" });
    await outbox.send({ text: "second" });
    await drain();
    for (const wait of [500, 1000, 2000]) {
      const before = t.hub.received.length;
      t.timers.advance(wait - 1);
      await drain();
      expect(t.hub.received.length).toBe(before);
      t.timers.advance(1);
      await drain();
      expect(t.hub.received.length).toBe(before + 1);
    }
    expect(new Set(t.hub.received.map((b) => b["text"]))).toEqual(new Set(["first"]));
    t.hub.status = null;
    t.timers.advance(4000);
    await drain();
    expect(statuses(t)).toEqual(["sent", "sent"]);
  });
});

describe("asking again after a catch-up", () => {
  it("re-posts every keeper-path send while the row says keeper, and the stored result settles it", async () => {
    const t = setup();
    const outbox = await t.make();
    const entry = await outbox.send({ text: "x" });
    await drain();
    outbox.recheckSent();
    await drain();
    expect(t.hub.received.map((b) => b["command_id"])).toEqual([entry.commandId, entry.commandId]);
    expect(t.hub.applied).toEqual([entry.commandId]);
    t.hub.results.set(entry.commandId, { ok: false, note: "the hub restarted before this was delivered" });
    outbox.recheckSent();
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: "the hub restarted before this was delivered" });
  });

  it("never re-posts a legacy send", async () => {
    const t = setup();
    legacy(t);
    const outbox = await t.make();
    await outbox.send({ text: "rm -rf build" });
    await drain();
    for (let i = 0; i < 3; i++) {
      outbox.recheckSent();
      outbox.wake();
      await drain();
    }
    expect(t.hub.applied).toHaveLength(1);
    expect(statuses(t)).toEqual(["sent"]);
  });

  it("does not re-post a keeper send once the session is no longer keeper-held", async () => {
    const t = setup();
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    t.world.keeper = false;
    outbox.recheckSent();
    await drain();
    expect(t.hub.received).toHaveLength(1);
    expect(statuses(t)).toEqual(["sent"]);
  });

  it("asks again after sign-in when the ask was refused", async () => {
    const t = setup();
    const outbox = await t.make();
    const entry = await outbox.send({ text: "x" });
    await drain();
    t.hub.status = 401;
    outbox.recheckSent();
    await drain();
    expect(t.view().paused).toBe("unauthorized");
    t.hub.status = null;
    t.hub.results.set(entry.commandId, { ok: true });
    outbox.wake();
    await drain();
    expect(t.view().entries).toEqual([]);
  });
});

describe("deadlines run whether or not anything can be posted", () => {
  it("waiting for 24 hours: failed, not sent", async () => {
    const t = setup();
    t.world.canPost = false; // the row never arrived, or the device stayed offline
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    t.timers.advance(OUTBOX_TTL_MS);
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: NOT_SENT_IN_TIME });
    expect(t.transport.calls).toEqual([]);
  });

  it("paused by a 401 for 24 hours: failed", async () => {
    const t = setup();
    t.hub.status = 401;
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    t.timers.advance(OUTBOX_TTL_MS);
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: NOT_SENT_IN_TIME });
  });

  it("legacy sent and no result in 45 minutes: unconfirmed, with a reason", async () => {
    const t = setup();
    legacy(t);
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    t.timers.advance(LEGACY_CONFIRM_MS - 1);
    await drain();
    expect(statuses(t)).toEqual(["sent"]);
    t.timers.advance(1);
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "unconfirmed", error: LEGACY_UNCONFIRMED });
    expect(LEGACY_CONFIRM_MS).toBeGreaterThan(30 * 60 * 1000); // longer than the hub's own busy limit
  });

  it("keeper sent and no result in 24 hours: unconfirmed", async () => {
    const t = setup();
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    t.timers.advance(OUTBOX_TTL_MS);
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "unconfirmed", error: KEEPER_UNCONFIRMED });
  });

  it("a terminal entry nobody dismisses is kept 7 days, then removed", async () => {
    const t = setup();
    t.hub.status = 422;
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    expect(statuses(t)).toEqual(["failed"]);
    t.timers.advance(TERMINAL_KEPT_MS - 1);
    await drain();
    expect(statuses(t)).toEqual(["failed"]);
    t.timers.advance(1);
    await drain();
    expect(t.view().entries).toEqual([]);
    expect(await stored(t.factory)).toEqual([]);
  });

  it("an entry reloaded long after it was made is aged at once", async () => {
    const t = setup();
    const storage = await openOutboxStorage(t.factory);
    const old = (status: OutboxEntry["status"], extra: Partial<OutboxEntry> = {}): OutboxEntry => ({
      commandId: `old-${status}`, session: KEY, text: status, attachments: [], mode: "queue",
      createdAt: t.timers.now() - OUTBOX_TTL_MS - 1, status, attempts: 0, ...extra,
    });
    await storage.put(old("waiting"));
    await storage.put(old("sent", { path: "legacy", sentAt: t.timers.now() - LEGACY_CONFIRM_MS }));
    t.world.canPost = false;
    await t.make();
    await drain();
    expect(t.view().entries.map((e) => [e.text, e.status]).sort()).toEqual([
      ["sent", "unconfirmed"],
      ["waiting", "failed"],
    ]);
  });

  it("a session that is gone ends every open send at once", async () => {
    const t = setup();
    t.world.canPost = false;
    const outbox = await t.make();
    await outbox.send({ text: "waiting" });
    t.world.canPost = true;
    outbox.sessionGone();
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: SESSION_GONE });
  });
});

describe("waiting", () => {
  it("posts nothing while it may not, and everything once it may", async () => {
    const t = setup();
    t.world.canPost = false;
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    expect(t.transport.calls).toEqual([]);
    t.world.canPost = true;
    outbox.wake();
    await drain();
    expect(statuses(t)).toEqual(["sent"]);
  });
});

describe("surviving a reload, a restart and a broken database", () => {
  it("sends what an earlier page left, under the id it had", async () => {
    const t = setup();
    t.world.canPost = false;
    const before = await t.make();
    const entry = await before.send({ text: "typed offline" });
    before.close();
    t.world.canPost = true;
    await t.make();
    await drain();
    expect(t.transport.posts("/message").map((b) => b["command_id"])).toEqual([entry.commandId]);
  });

  it("reads an entry an earlier build stored as pending as waiting", async () => {
    const t = setup();
    const storage = await openOutboxStorage(t.factory);
    await storage.put({ commandId: "old", session: KEY, text: "x", attachments: [], mode: "queue", createdAt: t.timers.now(), status: "pending" as never, attempts: 0 });
    await t.make();
    await drain();
    expect(t.view().entries[0]).toMatchObject({ commandId: "old", status: "sent" });
  });

  it("asks again about an acknowledged keeper send after a reload", async () => {
    const t = setup();
    const before = await t.make();
    const entry = await before.send({ text: "x" });
    await drain();
    before.close();
    t.hub.results.set(entry.commandId, { ok: true });
    await t.make();
    await drain();
    expect(t.hub.applied).toEqual([entry.commandId]);
    expect(t.view().entries).toEqual([]);
  });

  it("hands a send that is still being written to the outbox that replaces it", async () => {
    const t = setup();
    const inner = await openOutboxStorage(t.factory);
    let finish: () => void = () => {};
    const slow: OutboxStorage = {
      get kind() {
        return inner.kind;
      },
      get note() {
        return inner.note;
      },
      load: (session) => inner.load(session),
      put: (entry) => new Promise<void>((resolve) => (finish = () => void inner.put(entry).then(resolve))),
      remove: (id) => inner.remove(id),
      close: () => {},
    };
    const first = await t.make(slow);
    const sending = first.send({ text: "mid-write" });
    await drain();
    const carried = first.carry();
    first.close();
    finish();
    await sending;
    const second = await t.make(undefined, carried);
    await drain();
    expect(t.hub.received).toHaveLength(1);
    expect(second.view.entries.map((e) => [e.text, e.status])).toEqual([["mid-write", "sent"]]);
  });

  it("keeps another session's sends out of this one", async () => {
    const t = setup();
    const storage = await openOutboxStorage(t.factory);
    await storage.put({ commandId: "other", session: "codex:x", text: "no", attachments: [], mode: "queue", createdAt: 1, status: "waiting", attempts: 0 });
    await t.make();
    expect(t.view().entries).toEqual([]);
  });

  it("works in memory, and says so, when IndexedDB is missing, throws, fails to open or fails later", async () => {
    const missing = await openOutboxStorage(undefined);
    expect([missing.kind, missing.note]).toEqual(["memory", expect.stringMatching(/not available.*memory.*reload/)]);
    const throwing = await openOutboxStorage({
      open() {
        throw new DOMException("The operation is insecure.", "SecurityError");
      },
    } as unknown as IDBFactory);
    expect(throwing.note).toContain("The operation is insecure.");
    const failing = await openOutboxStorage({
      open() {
        const request = {} as { error: Error; onerror?: (event: Event) => void };
        queueMicrotask(() => {
          request.error = new Error("QuotaExceededError");
          request.onerror?.(new Event("error"));
        });
        return request as unknown as IDBOpenDBRequest;
      },
    } as unknown as IDBFactory);
    expect(failing.note).toContain("QuotaExceededError");

    const notes: string[] = [];
    const broken: OutboxStorage = {
      kind: "indexeddb",
      note: "",
      load: async () => [],
      put: async () => {
        throw new Error("the disk is full");
      },
      remove: async () => {},
      close: () => {},
    };
    const t = setup({ storage: new FallbackStorage(broken, (note) => notes.push(note)) });
    const outbox = await t.make();
    expect(t.view().persistent).toBe(true);
    await outbox.send({ text: "hi" });
    await drain();
    expect(notes[0]).toContain("the disk is full");
    expect(t.view()).toMatchObject({ persistent: false, note: notes[0] });
    expect(statuses(t)).toEqual(["sent"]);
  });

  it("a deferred storage reports memory until the database is open", async () => {
    let open: (s: OutboxStorage) => void = () => {};
    const storage = new DeferredStorage(new Promise<OutboxStorage>((resolve) => (open = resolve)));
    expect(storage.kind).toBe("memory");
    const loading = storage.load(KEY);
    open(await openOutboxStorage(new IDBFactory()));
    expect(await loading).toEqual([]);
    expect(storage.kind).toBe("indexeddb");
    storage.close();
    expect(await new MemoryStorage("because").load()).toEqual([]);
  });
});

describe("the person's actions", () => {
  it("a failed or unconfirmed send can be sent again as a new message, or dismissed", async () => {
    const t = setup();
    t.hub.status = 409;
    const outbox = await t.make();
    const entry = await outbox.send({ text: "again please", attachments: ["f"], mode: "steer" });
    await drain();
    t.hub.status = null;
    const again = await outbox.retry(entry.commandId);
    await drain();
    expect(again?.commandId).not.toBe(entry.commandId);
    expect(t.view().entries.map((e) => [e.text, e.mode, e.attachments, e.status])).toEqual([["again please", "steer", ["f"], "sent"]]);
    expect(await outbox.retry(again?.commandId ?? "")).toBeUndefined(); // not terminal
    await outbox.discard(again?.commandId ?? "");
    expect(t.view().entries).toEqual([]);
    expect(await stored(t.factory)).toEqual([]);
  });
});
