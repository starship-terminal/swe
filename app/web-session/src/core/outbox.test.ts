// The send lifecycle, row by row (docs/specs/G11-client-core.md, "The send lifecycle").
import { IDBFactory } from "fake-indexeddb";
import { describe, expect, it } from "vitest";

import {
  KEEPER_UNCONFIRMED,
  LEGACY_CONFIRM_MS,
  LEGACY_UNCONFIRMED,
  MAYBE_SENT,
  NOT_SENT_IN_TIME,
  furtherAlong,
  Outbox,
  OUTBOX_TTL_MS,
  SESSION_GONE,
  STORAGE_DOWN,
  TERMINAL_KEPT_MAX,
  TERMINAL_KEPT_MS,
  type OutboxView,
} from "./outbox";
import {
  CLAIM_STALE_MS,
  decideClaim,
  DeferredStorage,
  FallbackStorage,
  MemoryStorage,
  openOutboxStorage,
  type OutboxEntry,
  type OutboxStorage,
} from "./outbox-storage";
import { describeSends } from "./sends";
import { FakeTimers, FakeTransport, drain } from "./test-support";
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
  /** An outbox whose `init()` the test runs itself. */
  const build = (storage: OutboxStorage, more: Partial<ConstructorParameters<typeof Outbox>[0]> = {}) =>
    new Outbox({
      session: KEY,
      storage,
      transport,
      timers,
      random: over.random ?? (() => 0.5),
      newId: () => `c${++ids}`,
      canPost: () => world.canPost,
      onChange: (view) => views.push(view),
      ...more,
    });
  const make = async (storage?: OutboxStorage, initial?: OutboxEntry[]) => {
    const outbox = build(storage ?? over.storage ?? (await openOutboxStorage(factory)), initial ? { initial } : {});
    await outbox.init();
    return outbox;
  };
  return { timers, transport, hub, factory, world, views, build, make, view: () => views[views.length - 1] as OutboxView };
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
      claim: (...args) => inner.claim(...args),
      abandon: (...args) => inner.abandon(...args),
      reconcile: (entry) => inner.reconcile(entry),
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

  /** A legacy send an earlier page left `sent` on disk, and an outbox whose storage has not opened yet. */
  const leftSent = async (more: Partial<ConstructorParameters<typeof Outbox>[0]> = {}) => {
    const t = setup();
    legacy(t);
    const left: OutboxEntry = {
      commandId: "c-left",
      session: KEY,
      text: "x",
      attachments: [],
      mode: "queue",
      createdAt: 0,
      status: "sent",
      attempts: 1,
      path: "legacy",
      sentAt: 0,
    };
    const disk = await openOutboxStorage(t.factory);
    await disk.put(left);
    let open: (s: OutboxStorage) => void = () => {};
    const outbox = t.build(new DeferredStorage(new Promise<OutboxStorage>((resolve) => (open = resolve))), more);
    const loading = outbox.init();
    return { t, outbox, left, load: async () => (open(await openOutboxStorage(t.factory)), loading) };
  };

  it("a result that comes before the stored sends are loaded is applied once they are", async () => {
    for (const result of [{ ok: true }, { ok: false, note: "session stayed busy for 30 min; message not sent" }]) {
      const { t, outbox, left, load } = await leftSent();
      outbox.noteResult(left.commandId, result);
      await load();
      await drain();
      // At once: not after the 45 minutes a legacy send waits for its result.
      if (result.ok) expect(t.view().entries).toEqual([]);
      else expect(t.view().entries).toMatchObject([{ commandId: left.commandId, status: "failed", error: result.note }]);
      expect(t.transport.calls).toEqual([]);
      expect(await stored(t.factory)).toHaveLength(result.ok ? 0 : 1);
    }
  });

  it("a result the session already holds is applied to the sends loaded later", async () => {
    const { t, load } = await leftSent({ knownResult: (id) => (id === "c-left" ? { ok: true } : undefined) });
    await load();
    await drain();
    expect(t.view().entries).toEqual([]);
    expect(await stored(t.factory)).toEqual([]);
  });

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
      await drain();
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

  it("no answer, on any row: failed, may or may not have been sent, never tried again", async () => {
    for (const path of ["keeper", "legacy"] as const) {
      const t = setup();
      if (path === "legacy") legacy(t);
      t.transport.down = true;
      const outbox = await t.make();
      await outbox.send({ text: "x" });
      await drain();
      expect(t.view().entries[0], path).toMatchObject({ status: "failed", error: MAYBE_SENT });
      t.transport.down = false;
      t.timers.advance(60_000);
      await drain();
      expect(t.transport.calls, path).toHaveLength(1);
    }
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

      const k = setup(); // a keeper row: no repeat there either
      k.hub.status = status;
      const kept = await k.make();
      await kept.send({ text: "x" });
      await drain();
      expect(k.view().entries[0], String(status)).toMatchObject({ status: "failed", error: MAYBE_SENT });
    }
  });

  it("429 and 425 say not now before anything is stored: tried again on any path", async () => {
    for (const status of [429, 425]) {
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

  it("backs off between retries, and holds the sends behind the one that cannot go", async () => {
    const t = setup({ random: () => 0 });
    t.hub.status = 429; // nothing was applied: the only answer still tried again
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

describe("a sent send is never posted again", () => {
  // Rechecks are gone: a re-post goes to the route the hub picks when it arrives, and the one that is not the keeper
  // does not de-duplicate. (These pinned review 1's "ask again after a catch-up" and review 2's "never re-post a legacy
  // send" and "not once the session is no longer keeper-held".)
  for (const path of ["keeper", "legacy"] as const) {
    it(`${path}: however often the page wakes or catches up, one post`, async () => {
      const t = setup();
      if (path === "legacy") legacy(t);
      const outbox = await t.make();
      await outbox.send({ text: "rm -rf build" });
      await drain();
      for (let i = 0; i < 3; i++) {
        outbox.wake();
        await drain();
      }
      expect(t.hub.received).toHaveLength(1);
      expect(statuses(t)).toEqual(["sent"]);
    });
  }

  it("keeper: a result that never comes ends it at 24 hours as taken by the hub, its result not seen", async () => {
    const t = setup();
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    t.timers.advance(OUTBOX_TTL_MS + 1);
    await drain();
    expect(t.view().entries).toMatchObject([{ status: "unconfirmed", error: KEEPER_UNCONFIRMED }]);
    // It claims only what a receipt proves: the hub took it. Not that it was delivered (a held send can expire).
    expect(KEEPER_UNCONFIRMED).toBe("The hub took this message, but its result was not seen on this device. Check the session before sending it again.");
    expect(t.hub.received).toHaveLength(1);
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

  it("an acknowledged keeper send after a reload: not asked about again, settled by a result that arrives", async () => {
    const t = setup();
    const before = await t.make();
    const entry = await before.send({ text: "x" });
    await drain();
    before.close();
    const after = await t.make();
    await drain();
    expect(t.hub.received).toHaveLength(1);
    expect(t.view().entries).toMatchObject([{ status: "sent", path: "keeper" }]);
    after.noteResult(entry.commandId, { ok: true });
    await drain();
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
      claim: (...args) => inner.claim(...args),
      abandon: (...args) => inner.abandon(...args),
      reconcile: (entry) => inner.reconcile(entry),
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
      claim: (entry, owner, now, staleMs) => new MemoryStorage().claim(entry, owner, now, staleMs),
      abandon: (entry, now, staleMs) => new MemoryStorage().abandon(entry, now, staleMs),
      reconcile: async () => {
        throw new Error("the disk is full");
      },
      close: () => {},
    };
    const t = setup({ storage: new FallbackStorage(broken, (note) => notes.push(note)) });
    const outbox = await t.make();
    expect(t.view().persistent).toBe(true);
    await outbox.send({ text: "hi" });
    await drain();
    expect(notes[0]).toContain("the disk is full");
    expect(t.view()).toMatchObject({ persistent: false, note: notes[0], storageDown: true });
    // Its first write may have landed, so nothing is posted under a claim the database did not record.
    expect(t.view().entries).toMatchObject([{ status: "waiting", lastError: STORAGE_DOWN }]);
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

describe("one page posts an entry (claims)", () => {
  it("two outboxes over one database, an entry both loaded: exactly one post", async () => {
    const t = setup();
    legacy(t);
    t.world.canPost = false;
    const a = await t.make();
    await a.send({ text: "made offline" });
    const b = await t.make(); // a second tab, which loads the same entry
    let answer: (r: JsonResponse) => void = () => {};
    t.transport.on("/message", (_path, request) => {
      t.hub.received.push(request.body as Record<string, unknown>);
      return new Promise<JsonResponse>((r) => (answer = r));
    }, "POST");
    t.world.canPost = true;
    a.wake();
    b.wake();
    await drain();
    expect(t.hub.received).toHaveLength(1);
    answer({ status: 202, body: { ok: true, queued: true } });
    await drain();
    b.wake();
    await drain();
    expect(t.hub.received).toHaveLength(1);
    expect(b.view.entries[0]).toMatchObject({ status: "sent", path: "legacy" });
  });

  it("an entry another page claimed moments ago is left alone, then treated as abandoned after the limit", async () => {
    const t = setup();
    legacy(t);
    const storage = await openOutboxStorage(t.factory);
    const now = t.timers.now();
    await storage.put({
      commandId: "x1", session: KEY, text: "x", attachments: [], mode: "queue", createdAt: now,
      status: "waiting", attempts: 1, claim: { owner: "another-page", at: now },
    });
    await t.make();
    await drain();
    expect(t.transport.calls).toEqual([]);
    expect(t.view().entries[0]).toMatchObject({ status: "waiting", claim: { owner: "another-page" } });
    t.timers.advance(CLAIM_STALE_MS);
    await drain();
    // Its post may have reached the hub, and this path does not de-duplicate: it is not posted again.
    expect(t.transport.calls).toEqual([]);
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: MAYBE_SENT });
  });

  it("an abandoned claim, on a keeper row too, is not posted again: it may have been applied", async () => {
    const t = setup();
    t.hub.applied.push("x1"); // the abandoned post did reach the hub
    const storage = await openOutboxStorage(t.factory);
    const now = t.timers.now();
    await storage.put({
      commandId: "x1", session: KEY, text: "x", attachments: [], mode: "queue", createdAt: now,
      status: "waiting", attempts: 1, claim: { owner: "a-closed-page", at: now - CLAIM_STALE_MS },
    });
    await t.make();
    await drain();
    expect(t.hub.received).toEqual([]);
    // Resolved by the deadline pass (no claim, `ambiguous`), and ended there: it is never posted again.
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: MAYBE_SENT, ambiguous: true, attempts: 1 });
    expect((await stored(t.factory))[0]?.claim).toBeUndefined();
  });

  it("writes the counted attempt and the claim before the request leaves", async () => {
    const t = setup();
    t.transport.on("/message", () => new Promise<JsonResponse>(() => {}), "POST");
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    expect(await stored(t.factory)).toMatchObject([{ status: "waiting", attempts: 1, claim: { owner: t.view().owner } }]);
  });

  it("in memory there is no other page: the outbox's own copy decides", async () => {
    const memory = new MemoryStorage();
    const entry: OutboxEntry = { commandId: "m", session: KEY, text: "x", attachments: [], mode: "queue", createdAt: 0, status: "waiting", attempts: 0 };
    expect(await memory.claim(entry, "me", 10, CLAIM_STALE_MS)).toMatchObject({ kind: "claimed", entry: { attempts: 1, claim: { owner: "me", at: 10 } } });
  });
});

describe("the deadline timer never spins", () => {
  it("a stale claim is resolved by the deadline pass itself while this page may not post", async () => {
    const t = setup();
    legacy(t);
    const storage = await openOutboxStorage(t.factory);
    const now = t.timers.now();
    await storage.put({
      commandId: "x1", session: KEY, text: "x", attachments: [], mode: "queue", createdAt: now,
      status: "waiting", attempts: 1, claim: { owner: "a-page-that-closed", at: now },
    });
    t.world.canPost = false; // offline, or the row never came
    await t.make();
    await drain();
    t.timers.advance(CLAIM_STALE_MS); // FakeTimers throws on any zero-delay timer
    await drain();
    for (let i = 0; i < 20; i++) {
      t.timers.advance(1);
      await drain();
    }
    expect(t.timers.zeroDelay).toBe(0);
    // Resolved by the pass itself, and ended: its post may have reached the hub and it is never posted again.
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: MAYBE_SENT, ambiguous: true });
    expect(t.view().entries[0]?.claim).toBeUndefined();
    expect((await stored(t.factory))[0]).toMatchObject({ status: "failed", ambiguous: true });
    // Its own deadline (how long an ended send is kept) is the next timer, in the future.
    expect(t.timers.nextIn).toBe(TERMINAL_KEPT_MS - 20);
    // Once it may post: still nothing, it ended.
    t.world.canPost = true;
    await t.make();
    await drain();
    expect(t.transport.calls).toEqual([]);
    expect(t.view().entries[0]).toMatchObject({ status: "failed", error: MAYBE_SENT });
  });

  it("a deadline the pass cannot act on (this page's own post still out) is not re-armed at 0 ms", async () => {
    const t = setup();
    t.transport.on("/message", () => new Promise<JsonResponse>(() => {}), "POST");
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    t.timers.advance(2 * CLAIM_STALE_MS);
    await drain();
    outbox.wake();
    await drain();
    expect(t.timers.zeroDelay).toBe(0);
    expect(t.view().inFlight).toBe("c1");
  });
});

describe("stop() while a post is out", () => {
  it("the hub's answer is still written, and the outbox that replaces this one takes it at once", async () => {
    for (const legacyPath of [false, true]) {
      const t = setup();
      if (legacyPath) legacy(t);
      let answer: (r: JsonResponse) => void = () => {};
      t.transport.on("/message", () => new Promise<JsonResponse>((r) => (answer = r)), "POST");
      const late: [OutboxEntry, boolean][] = [];
      const first = new Outbox({
        session: KEY, storage: await openOutboxStorage(t.factory), transport: t.transport, timers: t.timers, random: () => 0.5,
        newId: () => "c1", owner: "page", onLate: (e, settled) => late.push([e, settled]), onChange: () => {},
      });
      await first.init();
      await first.send({ text: "x" });
      await drain();
      const carried = first.carry();
      first.close();
      const second = new Outbox({
        session: KEY, storage: await openOutboxStorage(t.factory), transport: t.transport, timers: t.timers, random: () => 0.5,
        newId: () => "c2", owner: "page", initial: carried, onChange: (view) => t.views.push(view),
      });
      await second.init();
      await drain();
      expect(t.transport.calls, String(legacyPath)).toHaveLength(1); // the claim is fresh: not posted again
      answer({ status: 202, body: legacyPath ? { ok: true, queued: true } : { receipt: "accepted" } });
      await drain();
      expect(late).toHaveLength(1);
      second.adoptLate(...(late[0] as [OutboxEntry, boolean]));
      await drain();
      expect(second.view.entries[0], String(legacyPath)).toMatchObject({ status: "sent", path: legacyPath ? "legacy" : "keeper" });
      expect((await stored(t.factory))[0]).toMatchObject({ status: "sent" });
      expect((await stored(t.factory))[0]?.claim).toBeUndefined();
      expect(t.transport.calls).toHaveLength(1);
    }
  });
});

describe("what a 503 means", () => {
  it("every 503 (keeper_failed, store_failed, a code this client does not know) may have been applied: failed, never posted again", async () => {
    for (const code of ["keeper_failed", "store_failed", "something_new"]) {
      for (const keeper of [true, false]) {
        const t = setup({ random: () => 0 });
        if (!keeper) legacy(t);
        let n = 0;
        t.transport.on("/message", () => (++n === 1 ? { status: 503, body: { error: { code, message: "no" } } } : { status: 202, body: { ok: true, queued: true } }), "POST");
        const outbox = await t.make();
        await outbox.send({ text: "hello" });
        await drain();
        t.timers.advance(60_000);
        await drain();
        expect(n, `${code} ${keeper}`).toBe(1);
        expect(t.view().entries[0], `${code} ${keeper}`).toMatchObject({ status: "failed", error: MAYBE_SENT });
      }
    }
  });

  it("409 duplicate_client_id: the legacy path already holds this send, so it is sent", async () => {
    const t = setup();
    legacy(t);
    t.transport.on("/message", () => ({ status: 409, body: { error: { code: "duplicate_client_id", message: "a send with this client_id is still queued" } } }), "POST");
    const outbox = await t.make();
    await outbox.send({ text: "hello" });
    await drain();
    expect(t.view().entries[0]).toMatchObject({ status: "sent", path: "legacy" });
  });
});

describe("what is kept", () => {
  it("at most TERMINAL_KEPT_MAX ended entries, the oldest dropped first, never an open one", async () => {
    const t = setup();
    t.world.canPost = false;
    const storage = await openOutboxStorage(t.factory);
    for (let n = 0; n < TERMINAL_KEPT_MAX + 3; n++) {
      await storage.put({ commandId: `f${String(n).padStart(3, "0")}`, session: KEY, text: String(n), attachments: [], mode: "queue", createdAt: n, status: "failed", attempts: 1, error: "no", endedAt: t.timers.now() - 1000 + n });
    }
    await storage.put({ commandId: "open", session: KEY, text: "open", attachments: [], mode: "queue", createdAt: 0, status: "waiting", attempts: 0 });
    // The open one is older than every ended one, and stays.
    await t.make();
    await drain();
    const kept = t.view().entries;
    expect(kept.filter((e) => e.status === "failed")).toHaveLength(TERMINAL_KEPT_MAX);
    expect(kept.some((e) => e.commandId === "open")).toBe(true);
    expect(kept.some((e) => e.commandId === "f000" || e.commandId === "f001" || e.commandId === "f002")).toBe(false);
  });

  it("entries loaded after the session was found gone end at once", async () => {
    const t = setup();
    const storage = await openOutboxStorage(t.factory);
    await storage.put({ commandId: "w", session: KEY, text: "x", attachments: [], mode: "queue", createdAt: t.timers.now(), status: "waiting", attempts: 0 });
    t.world.canPost = false;
    let open: (s: OutboxStorage) => void = () => {};
    const deferred = new DeferredStorage(new Promise<OutboxStorage>((resolve) => (open = resolve)));
    const outbox = new Outbox({
      session: KEY, storage: deferred, transport: t.transport, timers: t.timers, random: () => 0.5, newId: () => "n",
      canPost: () => false, onChange: (view) => t.views.push(view),
    });
    const loading = outbox.init();
    outbox.sessionGone(); // the 404 came first
    open(storage);
    await loading;
    await drain();
    expect(t.view().entries).toMatchObject([{ commandId: "w", status: "failed", error: SESSION_GONE }]);
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

  /**
   * A storage whose claims answer only when the test says. `eager`: the claim's transaction starts at once
   * and only its answer waits (a discard lands during it). Otherwise the transaction starts on release (a
   * discard lands just before it).
   */
  async function heldClaims(t: ReturnType<typeof setup>, eager: boolean) {
    const inner = await openOutboxStorage(t.factory);
    let release: () => void = () => {};
    const storage: OutboxStorage = {
      get kind() {
        return inner.kind;
      },
      get note() {
        return inner.note;
      },
      load: (session) => inner.load(session),
      put: (entry) => inner.put(entry),
      remove: (id) => inner.remove(id),
      claim: (...args) => {
        const started = eager ? inner.claim(...args) : null;
        return new Promise((resolve) => (release = () => void (started ?? inner.claim(...args)).then(resolve)));
      },
      abandon: (...args) => inner.abandon(...args),
      reconcile: (entry) => inner.reconcile(entry),
      close: () => inner.close(),
    };
    return { storage, release: () => release() };
  }

  const shown = (view: OutboxView) =>
    describeSends({
      outbox: view,
      connection: { state: "live", attempt: 0, since: 0, retryAt: null, detail: "" },
      loaded: true,
      online: true,
    });

  for (const eager of [true, false]) {
    it(`a send being claimed offers no Discard, and one discarded anyway is never posted (discard ${eager ? "during" : "just before"} the claim)`, async () => {
      const t = setup();
      const held = await heldClaims(t, eager);
      const outbox = await t.make(held.storage);
      const entry = await outbox.send({ text: "never mind" });
      await drain();
      expect(t.view().inFlight).toBe(entry.commandId);
      expect(shown(t.view())).toMatchObject([{ shown: "sending", canDiscard: false, canRetry: false }]);
      await outbox.discard(entry.commandId); // a caller that does not read `canDiscard`
      held.release();
      await drain();
      expect(t.hub.received).toEqual([]);
      expect(t.view().entries).toEqual([]);
      expect(t.view().inFlight).toBeNull();
      expect(await stored(t.factory)).toEqual([]);
    });
  }

  it("a send that ends while it is being claimed is never posted", async () => {
    const t = setup();
    const held = await heldClaims(t, true);
    const outbox = await t.make(held.storage);
    const entry = await outbox.send({ text: "x" });
    await drain();
    outbox.sessionGone();
    await drain();
    held.release();
    await drain();
    expect(t.hub.received).toEqual([]);
    expect(t.view().entries).toMatchObject([{ commandId: entry.commandId, status: "failed", error: SESSION_GONE }]);
    expect((await stored(t.factory))[0]).not.toHaveProperty("claim");
  });
});

describe("init(): storage is the truth at start", () => {
  /** A storage over the test's database whose `load` or `put` answers only when the test says. */
  async function holding(t: ReturnType<typeof setup>, what: "load" | "put") {
    const inner = await openOutboxStorage(t.factory);
    let release: () => void = () => {};
    let puts = 0;
    const storage: OutboxStorage = {
      get kind() {
        return inner.kind;
      },
      get note() {
        return inner.note;
      },
      // The transaction starts at once; only its answer waits.
      load: (session) => {
        const started = inner.load(session);
        return what === "load" ? new Promise((resolve) => (release = () => void started.then(resolve))) : started;
      },
      // Only the first write is held: the ones after it go as usual.
      put: (entry) => {
        puts++;
        const started = inner.put(entry);
        return what === "put" && puts === 1 ? new Promise((resolve) => (release = () => void started.then(resolve))) : started;
      },
      remove: (id) => inner.remove(id),
      claim: (...args) => inner.claim(...args),
      abandon: (...args) => inner.abandon(...args),
      reconcile: (entry) => {
        puts++; // a write too: every write after a send's first one compares first
        return inner.reconcile(entry);
      },
      close: () => inner.close(),
    };
    return { storage, release: () => release(), puts: () => puts };
  }

  const handedOver = (id: string, over: Partial<OutboxEntry> = {}): OutboxEntry => ({
    commandId: id, session: KEY, text: id, attachments: [], mode: "queue", createdAt: 1, status: "waiting", attempts: 0, ...over,
  });

  it("a handed-over send discarded while the write-back runs is not written again, posted or left on disk", async () => {
    const t = setup();
    t.world.canPost = false; // y may wait; x must not come back
    const held = await holding(t, "put");
    const outbox = t.build(held.storage, { initial: [handedOver("y"), handedOver("x", { createdAt: 2 })] });
    const loading = outbox.init();
    await drain(); // the write-back of y is out; x is next
    await outbox.discard("x");
    held.release();
    await loading;
    await drain();
    t.world.canPost = true;
    outbox.wake();
    await drain();
    expect(t.hub.received.map((body) => body["command_id"])).toEqual(["y"]);
    expect(t.view().entries.map((e) => e.commandId)).toEqual(["y"]);
    expect((await stored(t.factory)).map((e) => e.commandId)).toEqual(["y"]);
  });

  it("a send discarded after the load's read has run is not taken back from the load", async () => {
    const t = setup();
    t.world.canPost = false; // nothing is posted: what the load hands back stays in view
    await (await openOutboxStorage(t.factory)).put(handedOver("x"));
    // Every call waits for the open, in call order (as IndexedDB's own open does): the load reads first, the
    // discard's removal after it, so the load's answer still holds x.
    let open: (s: OutboxStorage) => void = () => {};
    const outbox = t.build(new DeferredStorage(new Promise<OutboxStorage>((resolve) => (open = resolve))), { initial: [handedOver("x")] });
    const loading = outbox.init();
    const discarding = outbox.discard("x");
    open(await openOutboxStorage(t.factory));
    await Promise.all([loading, discarding]);
    await drain();
    expect(t.view().entries).toEqual([]);
    expect(await stored(t.factory)).toEqual([]);
    t.world.canPost = true;
    outbox.wake();
    await drain();
    expect(t.hub.received).toEqual([]);
  });

  it("loading that fails still applies what came while loading: results, what the session holds, a gone session", async () => {
    const t = setup();
    t.world.canPost = false;
    let fail: () => void = () => {};
    const broken: OutboxStorage = {
      kind: "memory",
      note: "",
      load: () => new Promise((_resolve, reject) => (fail = () => reject(new Error("the database went away")))),
      put: async () => {},
      remove: async () => {},
      claim: async (entry, owner, now) => ({ kind: "claimed", entry: { ...entry, attempts: entry.attempts + 1, claim: { owner, at: now } } }),
      abandon: async (entry) => entry,
      reconcile: async (entry) => entry,
      close: () => {},
    };
    const outbox = t.build(broken, {
      initial: [handedOver("x", { status: "sent", path: "legacy", attempts: 1 }), handedOver("y", { status: "sent", path: "legacy", attempts: 1 }), handedOver("z")],
      knownResult: (id) => (id === "y" ? { ok: false, note: "session stayed busy for 30 min; message not sent" } : undefined),
    });
    const loading = outbox.init();
    outbox.noteResult("x", { ok: true });
    outbox.sessionGone();
    fail();
    await loading;
    await drain();
    expect(t.view().loading).toBe(false);
    expect(t.view().entries.map((e) => [e.commandId, e.status, e.error])).toEqual([
      ["y", "failed", "session stayed busy for 30 min; message not sent"],
      ["z", "failed", SESSION_GONE],
    ]);
  });

  it("loading that fails ends loading, keeps what was handed over, and sends it", async () => {
    const t = setup();
    const broken: OutboxStorage = {
      kind: "memory",
      note: "",
      load: async () => {
        throw new Error("the database went away");
      },
      put: async () => {},
      remove: async () => {},
      claim: async (entry, owner, now) => ({ kind: "claimed", entry: { ...entry, attempts: entry.attempts + 1, claim: { owner, at: now } } }),
      abandon: async (entry) => entry,
      reconcile: async (entry) => entry,
      close: () => {},
    };
    const outbox = t.build(broken, { initial: [handedOver("x")] });
    await outbox.init();
    await drain();
    expect(t.view().loading).toBe(false);
    expect(t.hub.received.map((b) => b["command_id"])).toEqual(["x"]);
  });

  it("where storage has the entry, its copy wins over the one handed over", async () => {
    const t = setup();
    legacy(t);
    await (await openOutboxStorage(t.factory)).put(handedOver("x", { status: "sent", path: "legacy", sentAt: 1, attempts: 1 }));
    const outbox = await t.make(undefined, [handedOver("x", { attempts: 1, claim: { owner: "old", at: 0 } })]);
    await drain();
    expect(t.hub.received).toEqual([]);
    expect(t.view().entries).toMatchObject([{ commandId: "x", status: "sent" }]);
    expect((await stored(t.factory))[0]).toMatchObject({ status: "sent" });
    outbox.close();
  });

  it("another tab moves the entry on between the load and the write-back: its copy stands, and this one is not posted", async () => {
    const t = setup(); // a keeper row: the other tab may post a send whose earlier claim went stale
    // On disk: a claim from a page that went away. Handed over: this page's copy, released by an answer (a 429)
    // whose write did not land.
    await (await openOutboxStorage(t.factory)).put(handedOver("x", { attempts: 1, claim: { owner: "gone", at: 0 } }));
    const held = await holding(t, "load");
    const a = t.build(held.storage, { initial: [handedOver("x", { attempts: 1, lastError: "busy" })] });
    const loading = a.init();
    await drain(); // a's load has read the claimed copy; its answer waits
    // The other tab, a minute later, takes the stale claim over: the send may have been applied, so it ends it.
    t.timers.advance(CLAIM_STALE_MS + 1);
    const b = await t.make(await openOutboxStorage(t.factory));
    await drain();
    expect(t.transport.posts("/message")).toHaveLength(0);
    const theirs = (await stored(t.factory))[0];
    expect(theirs).toMatchObject({ attempts: 1, ambiguous: true, status: "failed", error: MAYBE_SENT });
    // Now a merges: its copy beat what it read, but the write-back compares with what is stored now, so the other
    // tab's claim stands and a leaves the send to it.
    held.release();
    await loading;
    await drain();
    expect(t.transport.posts("/message")).toHaveLength(0);
    expect(await stored(t.factory)).toEqual([theirs]);
    expect(t.view().entries).toMatchObject([{ status: "failed", error: MAYBE_SENT }]);
    a.close();
    b.close();
  });

  it("a handed-over send storage does not have is written once, and sent", async () => {
    const t = setup();
    const held = await holding(t, "load");
    const outbox = t.build(held.storage, { initial: [handedOver("x")] });
    const loading = outbox.init();
    held.release();
    await loading;
    await drain();
    expect(t.hub.received.map((b) => b["command_id"])).toEqual(["x"]);
    // Written back once by init(); every later write is the send's own lifecycle (its answer).
    expect(held.puts()).toBe(2);
    expect(await stored(t.factory)).toMatchObject([{ commandId: "x", status: "sent" }]);
  });

  it("a result that comes while loading settles a handed-over send before it is posted", async () => {
    const t = setup();
    const held = await holding(t, "load");
    const outbox = t.build(held.storage, { initial: [handedOver("x", { attempts: 1, status: "sent", path: "legacy", sentAt: 1 })] });
    const loading = outbox.init();
    outbox.noteResult("x", { ok: true });
    held.release();
    await loading;
    await drain();
    expect(t.view().entries).toEqual([]);
    expect(await stored(t.factory)).toEqual([]);
  });
});

describe("two copies of one send: the one further along wins", () => {
  const copy = (over: Partial<OutboxEntry>): OutboxEntry => ({
    commandId: "x", session: KEY, text: "x", attachments: [], mode: "queue", createdAt: 1, status: "waiting", attempts: 0, ...over,
  });
  const claim = { owner: "p", at: 0 };

  it("orders copies by stage, then attempts, then a released claim, then ambiguity", () => {
    // Each pair is [older, newer]: one step of the lifecycle, or two copies a step apart.
    const steps: [string, OutboxEntry, OutboxEntry][] = [
      ["claimed", copy({}), copy({ attempts: 1, claim })],
      ["answered 2xx", copy({ attempts: 1, claim }), copy({ attempts: 1, status: "sent", path: "legacy" })],
      ["released by its answer (429, 5xx, 401)", copy({ attempts: 1, claim }), copy({ attempts: 1, lastError: "busy" })],
      ["a stale claim abandoned", copy({ attempts: 1, claim }), copy({ attempts: 1, ambiguous: true })],
      ["claimed again", copy({ attempts: 1, ambiguous: true }), copy({ attempts: 2, ambiguous: true, claim })],
      ["ended while waiting", copy({ attempts: 2, claim }), copy({ attempts: 2, status: "failed", error: "no" })],
      ["ended after it was sent", copy({ attempts: 1, status: "sent" }), copy({ attempts: 1, status: "unconfirmed" })],
      ["a released copy marked ambiguous", copy({ attempts: 1 }), copy({ attempts: 1, ambiguous: true })],
    ];
    for (const [step, older, newer] of steps) {
      expect([step, furtherAlong(newer, older) > 0]).toEqual([step, true]);
      expect([step, furtherAlong(older, newer) < 0]).toEqual([step, true]);
    }
    // Copies the order cannot tell apart: the stored one is kept (see the spec for why that is safe).
    expect(furtherAlong(copy({ attempts: 1, status: "failed" }), copy({ attempts: 1, status: "unconfirmed" }))).toBe(0);
  });

  it("a send an older build stored as `pending` ends `failed` like any waiting one, on disk and in view", async () => {
    const t = setup();
    t.world.canPost = false;
    const old = { ...copy({ commandId: "old" }), status: "pending" } as unknown as OutboxEntry;
    await (await openOutboxStorage(t.factory)).put(old);
    const outbox = await t.make();
    outbox.sessionGone();
    await drain();
    expect(t.view().entries).toMatchObject([{ commandId: "old", status: "failed", error: SESSION_GONE }]);
    expect(await stored(t.factory)).toMatchObject([{ commandId: "old", status: "failed", error: SESSION_GONE }]);
    // A status this build does not know never decides a comparison by winning it.
    const unknown = { ...copy({}), status: "archived" } as unknown as OutboxEntry;
    expect(furtherAlong(copy({}), unknown)).toBeGreaterThan(0);
    expect(furtherAlong(unknown, copy({}))).toBeLessThan(0);
  });

  it("a claim is always written with one more attempt (the order depends on it)", async () => {
    const owner = "me";
    for (const stored of [copy({}), copy({ attempts: 3, ambiguous: true }), copy({ attempts: 2, claim: { owner: "other", at: 0 } })]) {
      const decided = decideClaim(stored, owner, CLAIM_STALE_MS + 1, CLAIM_STALE_MS);
      expect(decided.claim.kind).toBe("claimed");
      expect(decided.write).toMatchObject({ attempts: stored.attempts + 1, claim: { owner, at: CLAIM_STALE_MS + 1 } });
      // And through each storage, which must use `decideClaim`.
      for (const storage of [new MemoryStorage("m"), await openOutboxStorage(new IDBFactory())]) {
        await storage.put(stored);
        const claimed = await storage.claim(stored, owner, CLAIM_STALE_MS + 1, CLAIM_STALE_MS);
        expect(claimed).toMatchObject({ kind: "claimed", entry: { attempts: stored.attempts + 1, claim: { owner } } });
        // A memory storage keeps nothing (the outbox's own list is the copy); a database keeps the claimed copy.
        if (storage.kind === "indexeddb") expect((await storage.load(KEY))[0]).toMatchObject({ attempts: stored.attempts + 1, claim: { owner } });
      }
    }
  });

});

describe("when storage fails", () => {
  const lost = () => new Error("Connection to Indexed Database server lost");
  type Op = "load" | "put" | "remove" | "claim" | "abandon";
  /**
   * The test's database behind a connection that throws on the operations named, `times` times each (Infinity:
   * until `heal()`), as a lost WebKit IndexedDB connection does. A FallbackStorage over it, with no fresh
   * connection to reopen unless `reopen` is given.
   */
  async function failing(t: ReturnType<typeof setup>, fails: Partial<Record<Op, number>>, reopen?: () => Promise<OutboxStorage | null>) {
    const real = await openOutboxStorage(t.factory);
    const left = { ...fails };
    const calls: Op[] = [];
    const guard = <T,>(op: Op, run: () => Promise<T>): Promise<T> => {
      calls.push(op);
      if ((left[op] ?? 0) > 0) {
        left[op] = (left[op] as number) - 1;
        return Promise.reject(lost());
      }
      return run();
    };
    const inner: OutboxStorage = {
      get kind() {
        return real.kind;
      },
      get note() {
        return real.note;
      },
      load: (session) => guard("load", () => real.load(session)),
      put: (entry) => guard("put", () => real.put(entry)),
      remove: (id) => guard("remove", () => real.remove(id)),
      claim: (...args) => guard("claim", () => real.claim(...args)),
      abandon: (...args) => guard("abandon", () => real.abandon(...args)),
      reconcile: (entry) => guard("put", () => real.reconcile(entry)),
      close: () => {},
    };
    const notes: string[] = [];
    const storage = new FallbackStorage(inner, (note) => notes.push(note), reopen);
    return { storage, notes, calls, heal: () => Object.keys(left).forEach((op) => (left[op as Op] = 0)) };
  }

  it("a failed write is tried once more on a fresh connection, and nothing falls back when that works", async () => {
    const t = setup();
    const fresh = await openOutboxStorage(t.factory);
    const db = await failing(t, { put: Infinity }, async () => fresh);
    await db.storage.put(handed("x"));
    expect(db.notes).toEqual([]);
    expect(db.storage.kind).toBe("indexeddb");
    expect(await stored(t.factory)).toMatchObject([{ commandId: "x" }]);
  });

  it("claim, the database knows the send: not posted while the claim cannot be recorded, posted once when it can", async () => {
    const t = setup();
    legacy(t);
    const db = await failing(t, { claim: 2 });
    const outbox = await t.make(db.storage);
    await outbox.send({ text: "rm -rf build" });
    await drain();
    expect(t.hub.received).toEqual([]);
    expect(t.view().entries).toMatchObject([{ status: "waiting", attempts: 0, lastError: STORAGE_DOWN }]);
    expect(t.view().persistent).toBe(false);
    expect(await stored(t.factory)).toMatchObject([{ status: "waiting", attempts: 0 }]);
    // Each pass tries the database again; the second retry finds it answering.
    t.timers.advance(1000);
    await drain();
    expect(t.hub.received).toHaveLength(1);
    expect(t.view()).toMatchObject({ persistent: true, note: "" });
    expect(t.view().entries).toMatchObject([{ status: "sent", path: "legacy" }]);
    expect(await stored(t.factory)).toMatchObject([{ status: "sent", path: "legacy", attempts: 1 }]);
    expect(db.notes).toEqual([expect.stringContaining("not sent until it answers again"), ""]);
  });

  it("database down, legacy row: no send is posted, each says why, and they go out once it answers", async () => {
    const t = setup();
    legacy(t);
    const db = await failing(t, { put: Infinity, claim: Infinity });
    const outbox = await t.make(db.storage);
    await outbox.send({ text: "x" }); // its first write fails: only memory is sure to have it
    await outbox.send({ text: "y" });
    await drain();
    t.timers.advance(30_000);
    await drain();
    expect(t.hub.received).toEqual([]);
    expect(t.view().storageDown).toBe(true);
    const shown = describeSends({ outbox: t.view(), connection: { state: "live", attempt: 0, since: 0, retryAt: null, detail: "" }, loaded: true, online: true });
    expect(shown.map((v) => v.reason)).toEqual([`${STORAGE_DOWN}. Trying again.`, `${STORAGE_DOWN}. Trying again.`]);
    // The database answers again: what was held is written, and each send goes once, under a recorded claim.
    db.heal();
    t.timers.advance(30_000);
    await drain();
    expect(t.hub.received.map((b) => b["text"])).toEqual(["x", "y"]);
    expect((await stored(t.factory)).map((e) => [e.text, e.status, e.attempts])).toEqual([
      ["x", "sent", 1],
      ["y", "sent", 1],
    ]);
    expect(t.view().storageDown).toBe(false);
  });

  it("database down, legacy row: the deadline still ends a send that never went", async () => {
    const t = setup();
    legacy(t);
    const db = await failing(t, { put: Infinity, claim: Infinity });
    const outbox = await t.make(db.storage);
    await outbox.send({ text: "x" });
    await drain();
    t.timers.advance(OUTBOX_TTL_MS + 1);
    await drain();
    t.timers.advance(60_000); // the next pass, past the deadline
    await drain();
    expect(t.hub.received).toEqual([]);
    expect(t.view().entries).toMatchObject([{ status: "failed", error: NOT_SENT_IN_TIME }]);
  });

  it("database down, keeper row: nothing is posted either, then once after the database answers", async () => {
    const t = setup(); // a keeper row
    const db = await failing(t, { put: Infinity, claim: Infinity });
    const outbox = await t.make(db.storage);
    await outbox.send({ text: "x" });
    await drain();
    t.timers.advance(30_000);
    await drain();
    expect(t.hub.received).toEqual([]);
    expect(t.view().entries).toMatchObject([{ status: "waiting", lastError: STORAGE_DOWN }]);
    db.heal();
    t.timers.advance(30_000);
    await drain();
    expect(t.hub.received.map((b) => b["text"])).toEqual(["x"]);
    expect(await stored(t.factory)).toMatchObject([{ status: "sent", path: "keeper", attempts: 1 }]);
  });

  it("a keeper-row page in an outage, the session leaves the keeper, a healthy legacy-row page: one post in all", async () => {
    const t = setup();
    // Page A's first write landed (the database holds x, waiting, 0 attempts); then its database stops answering.
    await (await openOutboxStorage(t.factory)).put({
      commandId: "x", session: KEY, text: "x", attachments: [], mode: "queue", createdAt: 1, status: "waiting", attempts: 0,
    });
    const db = await failing(t, { claim: Infinity, put: Infinity });
    await t.make(db.storage); // its row says keeper
    await drain();
    t.timers.advance(30_000);
    await drain();
    expect(t.hub.received).toEqual([]); // not posted under a claim only memory knows
    // The session leaves the keeper; page B, healthy, on the same database, sees a legacy row.
    legacy(t);
    await t.make(await openOutboxStorage(t.factory));
    await drain();
    t.timers.advance(60_000);
    await drain();
    expect(t.hub.received.map((b) => b["command_id"])).toEqual(["x"]);
    expect(t.hub.applied).toEqual(["x"]);
  });

  it("a first write the database took although the page heard an error, then a reload: one post in all", async () => {
    const t = setup();
    legacy(t);
    // Every write lands and then reports an error, as a connection lost mid-commit can; claims fail outright.
    const real = await openOutboxStorage(t.factory);
    const lost = () => new Error("Connection to Indexed Database server lost");
    const landsButFails: OutboxStorage = {
      kind: "indexeddb",
      note: "",
      load: (session) => real.load(session),
      put: async (entry) => {
        await real.put(entry);
        throw lost();
      },
      remove: (id) => real.remove(id),
      claim: async () => {
        throw lost();
      },
      abandon: (...args) => real.abandon(...args),
      reconcile: async () => {
        throw lost();
      },
      close: () => {},
    };
    const a = await t.make(new FallbackStorage(landsButFails));
    await a.send({ text: "rm -rf build" });
    await drain();
    expect(t.hub.received).toEqual([]); // not posted: the claim was not recorded
    expect(await stored(t.factory)).toMatchObject([{ status: "waiting", attempts: 0 }]);
    // Reload: a new page whose database answers.
    a.close();
    await t.make(await openOutboxStorage(t.factory));
    await drain();
    expect(t.hub.received).toHaveLength(1);
    expect(await stored(t.factory)).toMatchObject([{ status: "sent", attempts: 1 }]);
  });

  it("no database at all: memory is the only store, so its claims are the recorded ones and sends post", async () => {
    const t = setup();
    legacy(t);
    const outbox = await t.make(new MemoryStorage("IndexedDB is not available here"));
    await outbox.send({ text: "x" });
    await drain();
    expect(t.view().storageDown).toBe(false);
    expect(t.hub.received).toHaveLength(1);
    expect(t.view().entries).toMatchObject([{ status: "sent", path: "legacy" }]);
  });

  it("remove: held while the database is down, applied once it answers", async () => {
    const t = setup();
    t.world.canPost = false;
    const db = await failing(t, { remove: 2, put: 1 }); // in fallback, each write is one try on the database
    const outbox = await t.make(db.storage);
    const entry = await outbox.send({ text: "never mind" }); // written before the failures start
    await outbox.discard(entry.commandId);
    expect(await stored(t.factory)).toMatchObject([{ commandId: entry.commandId }]); // the removal is held
    await outbox.send({ text: "next" }); // this write fails (and its retry) and is held too
    await outbox.send({ text: "later" }); // this one answers: everything held is replayed first
    expect((await stored(t.factory)).map((e) => e.text)).toEqual(["next", "later"]);
  });

  it("abandon: a stale claim resolved in memory, then written once the database answers", async () => {
    const t = setup();
    legacy(t);
    t.world.canPost = false;
    await (await openOutboxStorage(t.factory)).put(handed("x", { attempts: 1, claim: { owner: "gone", at: 0 } }));
    const db = await failing(t, { abandon: 2 });
    const outbox = await t.make(db.storage);
    t.timers.advance(CLAIM_STALE_MS + 1);
    await drain();
    // Resolved in memory (its post may have reached the hub) and ended: never posted again. The end's write
    // found the database answering, and replayed the held copy first, further along than the stale claim.
    expect(t.view().entries).toMatchObject([{ commandId: "x", status: "failed", error: MAYBE_SENT, ambiguous: true }]);
    expect(t.view().entries[0]).not.toHaveProperty("claim");
    expect(t.hub.received).toEqual([]);
    void outbox;
    expect((await stored(t.factory)).find((e) => e.commandId === "x")).toMatchObject({ status: "failed", ambiguous: true, attempts: 1 });
    expect((await stored(t.factory)).find((e) => e.commandId === "x")).not.toHaveProperty("claim");
  });

  it("load: while no load has answered, no send the database may hold is claimed from memory", async () => {
    const t = setup();
    legacy(t);
    await (await openOutboxStorage(t.factory)).put(handed("x"));
    const db = await failing(t, { load: 2, claim: 2 });
    await t.make(db.storage, [handed("x")]); // handed over; the load fails, so the database's copy is unknown
    await drain();
    expect(t.hub.received).toEqual([]);
    expect(t.view().entries).toMatchObject([{ commandId: "x", lastError: STORAGE_DOWN }]);
    t.timers.advance(1000);
    await drain();
    expect(t.hub.received.map((b) => b["command_id"])).toEqual(["x"]);
  });

  const handed = (id: string, over: Partial<OutboxEntry> = {}): OutboxEntry => ({
    commandId: id, session: KEY, text: id, attachments: [], mode: "queue", createdAt: 1, status: "waiting", attempts: 0, ...over,
  });
});

describe("evidence after a send ended as may or may not have been sent", () => {
  /** A keeper-row send whose answer was lost: it ends in doubt. */
  const inDoubt = async () => {
    const t = setup();
    t.transport.down = true;
    const outbox = await t.make();
    const entry = await outbox.send({ text: "x" });
    await drain();
    t.transport.down = false;
    expect(t.view().entries).toMatchObject([{ status: "failed", error: MAYBE_SENT, ambiguous: true }]);
    return { t, outbox, entry };
  };

  it("an ok or cancelled result afterwards: it leaves as delivered", async () => {
    for (const result of [{ ok: true }, { ok: false, cancelled: true }]) {
      const { t, outbox, entry } = await inDoubt();
      outbox.noteResult(entry.commandId, result);
      await drain();
      expect(t.view().entries).toEqual([]);
      expect(await stored(t.factory)).toEqual([]);
    }
  });

  it("a failed result afterwards replaces the doubt with the hub's reason", async () => {
    const { t, outbox, entry } = await inDoubt();
    outbox.noteResult(entry.commandId, { ok: false, note: "the session is not running" });
    await drain();
    expect(t.view().entries).toMatchObject([{ status: "failed", error: "the session is not running" }]);
    expect(t.view().entries[0]?.ambiguous).toBeUndefined();
    // A definite failure then stands.
    outbox.noteResult(entry.commandId, { ok: false, note: "something else" });
    await drain();
    expect(t.view().entries).toMatchObject([{ status: "failed", error: "the session is not running" }]);
  });

  it("its receipt afterwards (live, or already in the session's records): it is sent", async () => {
    const { t, outbox, entry } = await inDoubt();
    outbox.noteReceipt(entry.commandId, "accepted");
    await drain();
    expect(t.view().entries).toMatchObject([{ status: "sent", path: "keeper", receipt: "accepted" }]);
    expect(t.view().entries[0]?.ambiguous).toBeUndefined();
    expect(await stored(t.factory)).toMatchObject([{ status: "sent", receipt: "accepted" }]);
    expect(t.transport.posts("/message")).toHaveLength(1); // its one post; the evidence posted nothing

    const again = await inDoubt();
    again.outbox.close();
    const restarted = again.t.build(await openOutboxStorage(again.t.factory), {
      knownReceipt: (id) => (id === again.entry.commandId ? "queued" : undefined),
    });
    await restarted.init();
    await drain();
    expect(again.t.view().entries).toMatchObject([{ status: "sent", receipt: "queued" }]);
  });

  it("a receipt saying it was refused moves nothing; a definite failure is not reopened by a receipt", async () => {
    const { t, outbox, entry } = await inDoubt();
    outbox.noteReceipt(entry.commandId, "rejected");
    await drain();
    expect(t.view().entries).toMatchObject([{ status: "failed", error: MAYBE_SENT }]);
    outbox.noteResult(entry.commandId, { ok: false, note: "no" });
    await drain();
    outbox.noteReceipt(entry.commandId, "accepted");
    await drain();
    expect(t.view().entries).toMatchObject([{ status: "failed", error: "no" }]);
  });

  it("the order: in doubt is between waiting and sent, below a definite end", () => {
    const base: OutboxEntry = { commandId: "x", session: KEY, text: "x", attachments: [], mode: "queue", createdAt: 1, status: "waiting", attempts: 1 };
    const doubt: OutboxEntry = { ...base, status: "failed", ambiguous: true, error: MAYBE_SENT };
    const sent: OutboxEntry = { ...base, status: "sent", path: "keeper", receipt: "accepted" };
    const failed: OutboxEntry = { ...base, status: "failed", error: "no" };
    expect(furtherAlong(doubt, base)).toBeGreaterThan(0);
    expect(furtherAlong(sent, doubt)).toBeGreaterThan(0);
    expect(furtherAlong(failed, sent)).toBeGreaterThan(0);
    expect(furtherAlong({ ...base, status: "unconfirmed" }, doubt)).toBeGreaterThan(0);
    expect(furtherAlong(failed, { ...base, status: "unconfirmed" })).toBe(0); // the reviewer's tie stays a tie
  });

  it("a send answered 202 with a `rejected` receipt (a satellite's refusal, answered again) fails with the hub's reason", async () => {
    const t = setup();
    t.transport.on("/message", () => ({ status: 202, body: { ok: true, receipt: "rejected", duplicate: true, reason: "machine is too far behind" } }), "POST");
    const outbox = await t.make();
    await outbox.send({ text: "x" });
    await drain();
    expect(t.view().entries).toMatchObject([{ status: "failed", error: "machine is too far behind" }]);
  });
});

describe("evidence that comes while the post is out", () => {
  /** A keeper-row send whose post waits until the test answers it. */
  const posting = async () => {
    const t = setup();
    let answer: (r: JsonResponse) => void = () => {};
    let fail: (e: Error) => void = () => {};
    t.transport.on("/message", () => new Promise<JsonResponse>((resolve, reject) => ((answer = resolve), (fail = reject))), "POST");
    const outbox = await t.make();
    const entry = await outbox.send({ text: "x" });
    await drain();
    expect(t.view().inFlight).toBe(entry.commandId);
    const shown = () => describeSends({ outbox: t.view(), connection: { state: "live", attempt: 0, since: 0, retryAt: null, detail: "" }, loaded: true, online: true });
    return { t, outbox, entry, answer: (r: JsonResponse) => answer(r), lose: () => fail(new TypeError("the answer never arrived")), shown };
  };

  it("its receipt, then the answer is lost: sent, and no Retry", async () => {
    const p = await posting();
    p.outbox.noteReceipt(p.entry.commandId, "accepted");
    p.lose();
    await drain();
    expect(p.t.view().entries).toMatchObject([{ status: "sent", path: "keeper", receipt: "accepted" }]);
    expect(p.shown()).toMatchObject([{ shown: "sent", canRetry: false }]);
    expect(await stored(p.t.factory)).toMatchObject([{ status: "sent", receipt: "accepted" }]);
  });

  it("its receipt, then a 503: sent", async () => {
    const p = await posting();
    p.outbox.noteReceipt(p.entry.commandId, "queued");
    p.answer({ status: 503, body: { error: { code: "keeper_failed", message: "no" } } });
    await drain();
    expect(p.t.view().entries).toMatchObject([{ status: "sent", receipt: "queued" }]);
  });

  it("an ok result, then the answer is lost: it leaves as delivered", async () => {
    const p = await posting();
    p.outbox.noteResult(p.entry.commandId, { ok: true });
    p.lose();
    await drain();
    expect(p.t.view().entries).toEqual([]);
    expect(await stored(p.t.factory)).toEqual([]);
  });

  it("a `rejected` receipt, then the answer is lost: failed with the hub's reason, not may-or-may-not", async () => {
    const p = await posting();
    p.outbox.noteReceipt(p.entry.commandId, "rejected", "machine is too far behind");
    p.lose();
    await drain();
    expect(p.t.view().entries).toMatchObject([{ status: "failed", error: "machine is too far behind" }]);
    expect(p.t.view().entries[0]?.ambiguous).toBeUndefined();
  });

  it("its receipt, then stop() before the answer is lost: what is written is sent", async () => {
    const p = await posting();
    p.outbox.noteReceipt(p.entry.commandId, "accepted");
    p.outbox.close();
    p.lose();
    await drain();
    expect(await stored(p.t.factory)).toMatchObject([{ status: "sent", receipt: "accepted" }]);
  });

  it("its receipt while the claim is being written: settled by the receipt, never posted", async () => {
    const t = setup();
    const inner = await openOutboxStorage(t.factory);
    let release: () => void = () => {};
    const slow: OutboxStorage = {
      get kind() {
        return inner.kind;
      },
      get note() {
        return inner.note;
      },
      load: (session) => inner.load(session),
      put: (entry) => inner.put(entry),
      remove: (id) => inner.remove(id),
      claim: (...args) => {
        const started = inner.claim(...args);
        return new Promise((resolve) => (release = () => void started.then(resolve)));
      },
      abandon: (...args) => inner.abandon(...args),
      reconcile: (entry) => inner.reconcile(entry),
      close: () => inner.close(),
    };
    const outbox = await t.make(slow);
    const entry = await outbox.send({ text: "x" });
    await drain();
    outbox.noteReceipt(entry.commandId, "accepted"); // another page's post reached the hub
    release();
    await drain();
    expect(t.hub.received).toEqual([]);
    expect(t.view().entries).toMatchObject([{ status: "sent", receipt: "accepted" }]);
  });

  it("a stored result in the answer still wins over a receipt", async () => {
    const p = await posting();
    p.outbox.noteReceipt(p.entry.commandId, "accepted");
    p.answer({ status: 202, body: { receipt: "accepted", result: { ok: false, note: "the session is not running" } } });
    await drain();
    expect(p.t.view().entries).toMatchObject([{ status: "failed", error: "the session is not running" }]);
  });
});
