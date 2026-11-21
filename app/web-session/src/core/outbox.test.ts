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
  TERMINAL_KEPT_MAX,
  TERMINAL_KEPT_MS,
  type OutboxView,
} from "./outbox";
import {
  CLAIM_STALE_MS,
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
      keeperHeld: () => world.keeper,
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
      claim: (...args) => inner.claim(...args),
      abandon: (...args) => inner.abandon(...args),
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

  it("an abandoned claim on a keeper row is posted again under the same id, and the hub answers with the stored receipt", async () => {
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
    expect(t.hub.received.map((b) => b["command_id"])).toEqual(["x1"]);
    expect(t.hub.applied).toEqual(["x1"]);
    expect(t.view().entries[0]).toMatchObject({ status: "sent", path: "keeper", receipt: "queued", attempts: 2 });
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
    expect(t.view().entries[0]).toMatchObject({ status: "waiting", ambiguous: true });
    expect(t.view().entries[0]?.claim).toBeUndefined();
    expect((await stored(t.factory))[0]).toMatchObject({ ambiguous: true });
    // Its own deadline is the next timer, in the future.
    expect(t.timers.nextIn).toBe(OUTBOX_TTL_MS - CLAIM_STALE_MS - 20);
    // Once it may post, the repeat rule decides: this path does not de-duplicate.
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
        newId: () => "c1", owner: "page", keeperHeld: () => t.world.keeper, onLate: (e, settled) => late.push([e, settled]), onChange: () => {},
      });
      await first.init();
      await first.send({ text: "x" });
      await drain();
      const carried = first.carry();
      first.close();
      const second = new Outbox({
        session: KEY, storage: await openOutboxStorage(t.factory), transport: t.transport, timers: t.timers, random: () => 0.5,
        newId: () => "c2", owner: "page", keeperHeld: () => t.world.keeper, initial: carried, onChange: (view) => t.views.push(view),
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
  it("keeper_failed, or a code this client does not know: may have been applied, so the row decides", async () => {
    for (const code of ["keeper_failed", "something_new"]) {
      const t = setup({ random: () => 0 });
      let n = 0;
      t.transport.on("/message", () => {
        n++;
        return n === 1
          ? { status: 503, body: { error: { code, message: "the session's keeper did not answer" } } }
          : { status: 202, body: { ok: true, queued: true } };
      }, "POST");
      const outbox = await t.make();
      await outbox.send({ text: "hello" });
      await drain();
      expect(t.view().entries[0], code).toMatchObject({ status: "waiting", ambiguous: true });
      t.world.keeper = false; // detached before the retry
      t.timers.advance(1000);
      await drain();
      expect(n, code).toBe(1);
      expect(t.view().entries[0], code).toMatchObject({ status: "failed", error: MAYBE_SENT });
    }
  });

  it("every 503, store_failed included, may have been applied (a satellite answers it after storing): the row decides", async () => {
    for (const keeper of [true, false]) {
      const t = setup({ random: () => 0 });
      if (!keeper) legacy(t);
      let n = 0;
      t.transport.on("/message", () => (++n === 1 ? { status: 503, body: { error: { code: "store_failed", message: "disk" } } } : { status: 202, body: { receipt: "duplicate" } }), "POST");
      const outbox = await t.make();
      await outbox.send({ text: "hello" });
      await drain();
      t.timers.advance(500);
      await drain();
      if (keeper) {
        expect(n).toBe(2);
        expect(t.view().entries[0]).toMatchObject({ status: "sent", path: "keeper", receipt: "duplicate" });
      } else {
        expect(n).toBe(1);
        expect(t.view().entries[0]).toMatchObject({ status: "failed", error: MAYBE_SENT });
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
      canPost: () => false, keeperHeld: () => true, onChange: (view) => t.views.push(view),
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
