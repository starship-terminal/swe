// Keeper-only posts and command lookups (G31), one test per row of the tables in
// docs/specs/G11-client-core.md, "Keeper-only posts and command lookups".
import { IDBFactory } from "fake-indexeddb";
import { describe, expect, it } from "vitest";

import {
  KEEPER_UNCONFIRMED,
  MAYBE_SENT,
  NOT_KEPT,
  Outbox,
  OUTBOX_TTL_MS,
  REPEATING,
  SESSION_GONE,
  type OutboxFeatures,
  type OutboxView,
} from "./outbox";
import { CLAIM_STALE_MS, decideClaim, openOutboxStorage, type OutboxEntry, type OutboxStorage } from "./outbox-storage";
import { describeSends } from "./sends";
import { PROTOCOL } from "./generated/session-v2";
import { FakeTimers, FakeTransport, drain } from "./test-support";
import type { JsonRequest, JsonResponse } from "./transport";

const KEY = "claude:t1";
const LOOKUP = "/v1/sessions/claude%3At1/commands/";

type Stored = { receipt: string; reason?: string; result?: { ok: boolean; note?: string; cancelled?: boolean } };
type Answer = JsonResponse | "lost" | "apply-and-lose";

/**
 * A hub that has G31. `owns`: whether a keeper holds the session when a post arrives. The keeper path stores each
 * id once and answers a repeat with what it stored; the legacy path types every post in, and refuses a post with
 * `require_keeper: true` (409 `not_kept`) before it does anything. `script` answers the next posts instead.
 */
class Hub {
  owns = true;
  store = new Map<string, Stored>();
  /** Every time a message was typed in, and by which route. */
  typed: { id: string; route: "keeper" | "legacy" }[] = [];
  posts: Record<string, unknown>[] = [];
  lookups: string[] = [];
  script: Answer[] = [];
  /** What the lookup answers when it is not the store (a status), or null for the store. */
  lookupStatus: number | null = null;

  post = (_path: string, request: JsonRequest): JsonResponse => {
    const body = request.body as { command_id: string; require_keeper?: boolean };
    this.posts.push(body);
    const scripted = this.script.shift();
    if (scripted === "lost") throw new TypeError("the connection dropped");
    if (scripted !== undefined && scripted !== "apply-and-lose") return scripted;
    const answer = this.route(body);
    if (scripted === "apply-and-lose") throw new TypeError("the answer never arrived");
    return answer;
  };

  private route(body: { command_id: string; require_keeper?: boolean }): JsonResponse {
    const id = body.command_id;
    if (this.owns) {
      const known = this.store.get(id);
      if (known !== undefined) {
        return { status: 202, body: { ok: true, duplicate: true, command_id: id, receipt: known.receipt, ...(known.result ? { result: known.result } : {}) } };
      }
      this.store.set(id, { receipt: "accepted" });
      this.typed.push({ id, route: "keeper" });
      return { status: 202, body: { ok: true, queued: true, command_id: id, receipt: "accepted" } };
    }
    if (body.require_keeper === true) return { status: 409, body: { error: { code: "not_kept", message: "not run by a session keeper" } } };
    this.typed.push({ id, route: "legacy" });
    return { status: 202, body: { ok: true, queued: true, client_id: id, waiting: false } };
  }

  lookup = (path: string): JsonResponse => {
    const id = decodeURIComponent(path.slice(path.lastIndexOf("/") + 1));
    this.lookups.push(id);
    if (this.lookupStatus !== null) return { status: this.lookupStatus, body: { error: { code: "x", message: "x" } } };
    const stored = this.store.get(id);
    if (stored === undefined) return { status: 404, body: { error: { code: "unknown_command", message: "no such command" } } };
    return {
      status: 200,
      body: { ok: true, session: KEY, command_id: id, op: "send", received_at: 1, updated_at: 1, ...stored },
    };
  };

  /** How many times each id was typed in: never more than once is the whole point. */
  timesTyped(id: string): number {
    return this.typed.filter((t) => t.id === id).length;
  }
}

const BOTH: OutboxFeatures = { requireKeeper: true, lookup: true };
const NEITHER: OutboxFeatures = { requireKeeper: false, lookup: false };

function setup() {
  const timers = new FakeTimers();
  const transport = new FakeTransport();
  const hub = new Hub();
  transport.on("/message", async (path, request) => hub.post(path, request), "POST");
  transport.on("/commands/", async (path) => hub.lookup(path), "GET");
  const factory = new IDBFactory();
  const world: { features: OutboxFeatures | null; keeperRow: boolean; canPost: boolean } = { features: BOTH, keeperRow: true, canPost: true };
  let ids = 0;
  const views: OutboxView[] = [];
  const build = (storage: OutboxStorage, more: Partial<ConstructorParameters<typeof Outbox>[0]> = {}) =>
    new Outbox({
      session: KEY,
      storage,
      transport,
      timers,
      random: () => 0.5,
      newId: () => `c${++ids}`,
      canPost: () => world.canPost,
      features: () => world.features,
      keeperRow: () => world.keeperRow,
      onChange: (view) => views.push(view),
      ...more,
    });
  const make = async (more: Partial<ConstructorParameters<typeof Outbox>[0]> = {}) => {
    const outbox = build(await openOutboxStorage(factory), more);
    await outbox.init();
    return outbox;
  };
  const view = () => views[views.length - 1] as OutboxView;
  const entry = (id: string) => view().entries.find((e) => e.commandId === id);
  const disk = async () => (await openOutboxStorage(factory)).load(KEY);
  /** Put an entry in storage as an earlier page left it. */
  const leave = async (over: Partial<OutboxEntry>): Promise<OutboxEntry> => {
    const left: OutboxEntry = {
      commandId: "old",
      session: KEY,
      text: "left",
      attachments: [],
      mode: "queue",
      createdAt: timers.now(),
      status: "waiting",
      attempts: 1,
      ...over,
    };
    const storage = await openOutboxStorage(factory);
    await storage.put(left);
    storage.close();
    return left;
  };
  /** Let a backoff run out, and what it started finish. */
  const later = async (ms = 30_000) => {
    timers.advance(ms);
    await drain();
  };
  return { timers, transport, hub, factory, world, views, view, entry, disk, leave, later, make, build };
}

describe("what a claim posts", () => {
  it("W on a keeper row with require_keeper listed: posted keeper-only, and the claim records it", async () => {
    const t = setup();
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    expect(t.hub.posts).toEqual([expect.objectContaining({ command_id: sent.commandId, require_keeper: true })]);
    expect(t.entry(sent.commandId)).toMatchObject({ status: "sent", path: "keeper", keeperOnly: true });
    expect(await t.disk()).toMatchObject([{ status: "sent", keeperOnly: true }]);
  });

  it("W on a keeper row with the features not known: posted plain, as merged", async () => {
    const t = setup();
    t.world.features = null;
    const outbox = await t.make();
    await outbox.send({ text: "hi" });
    await drain();
    expect(t.hub.posts[0]).not.toHaveProperty("require_keeper");
    expect(t.view().entries[0]?.keeperOnly).toBeUndefined();
  });

  it("W on a keeper row with require_keeper not listed: posted plain", async () => {
    const t = setup();
    t.world.features = NEITHER;
    const outbox = await t.make();
    await outbox.send({ text: "hi" });
    await drain();
    expect(t.hub.posts[0]).not.toHaveProperty("require_keeper");
  });

  it("W on a row that does not say keeper: posted plain, whatever the hub lists", async () => {
    const t = setup();
    t.world.keeperRow = false;
    t.hub.owns = false;
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    expect(t.hub.posts[0]).not.toHaveProperty("require_keeper");
    expect(t.entry(sent.commandId)).toMatchObject({ status: "sent", path: "legacy" });
  });

  it("WA with require_keeper listed: posted again keeper-only, and the keeper answers the stored receipt", async () => {
    const t = setup();
    t.hub.store.set("old", { receipt: "accepted" });
    await t.leave({ ambiguous: true, keeperOnly: true });
    await t.make();
    await drain();
    expect(t.hub.posts).toEqual([expect.objectContaining({ command_id: "old", require_keeper: true })]);
    expect(t.entry("old")).toMatchObject({ status: "sent", path: "keeper" });
    expect(t.entry("old")?.ambiguous).toBeUndefined();
    expect(t.hub.timesTyped("old")).toBe(0); // typed in by the earlier post, never by this one
  });

  it("WA with the features not known: not claimed, not posted, holding back the sends behind it, until they are", async () => {
    const t = setup();
    t.world.features = null;
    await t.leave({ ambiguous: true, keeperOnly: true, createdAt: t.timers.now() - 1000 });
    const outbox = await t.make();
    await outbox.send({ text: "behind" });
    await drain();
    expect(t.hub.posts).toEqual([]);
    expect(await t.disk()).toMatchObject([{ commandId: "old", attempts: 1 }, { status: "waiting", attempts: 0 }]);
    expect((await t.disk())[0]?.claim).toBeUndefined();
    t.world.features = BOTH;
    outbox.featuresChanged();
    await drain();
    expect(t.hub.posts.map((p) => [p.command_id, p.require_keeper])).toEqual([
      ["old", true],
      ["c1", true],
    ]);
  });

  it("WA with require_keeper not listed: ends may-or-may-not, never posted", async () => {
    const t = setup();
    t.world.features = NEITHER;
    await t.leave({ ambiguous: true, keeperOnly: true });
    await t.make();
    await drain();
    expect(t.hub.posts).toEqual([]);
    expect(t.entry("old")).toMatchObject({ status: "failed", ambiguous: true, error: MAYBE_SENT });
  });

  it("WA- (in doubt, not keeper-only): ends may-or-may-not, never posted, as merged", async () => {
    const t = setup();
    await t.leave({ ambiguous: true });
    await t.make();
    await drain();
    expect(t.hub.posts).toEqual([]);
    expect(t.entry("old")).toMatchObject({ status: "failed", error: MAYBE_SENT });
  });

  it("a stale claim taken over in the claim decides by the stored copy's keeperOnly", async () => {
    for (const [features, end] of [
      [BOTH, "sent"],
      [null, "waiting"],
      [NEITHER, "failed"],
    ] as const) {
      const t = setup();
      t.world.features = features;
      // Another page's claim, fresh when the age pass looks and stale by the time the claim's transaction reads
      // it: the claim takes it over, and the stored copy comes back ambiguous with its own keeperOnly.
      await t.leave({ keeperOnly: true, claim: { owner: "gone", at: t.timers.now() - 1 } });
      const inner = await openOutboxStorage(t.factory);
      const late: OutboxStorage = {
        kind: inner.kind,
        note: inner.note,
        load: (session) => inner.load(session),
        put: (entry) => inner.put(entry),
        remove: (id) => inner.remove(id),
        claim: (entry, owner, now, staleMs, keeperOnly) => inner.claim(entry, owner, now + CLAIM_STALE_MS, staleMs, keeperOnly),
        abandon: (entry, now, staleMs) => inner.abandon(entry, now, staleMs),
        reconcile: (entry) => inner.reconcile(entry),
        close: () => inner.close(),
      };
      const outbox = t.build(late);
      await outbox.init();
      await drain();
      expect(t.entry("old")?.status).toBe(end);
      expect(t.hub.posts.filter((p) => p.require_keeper !== true)).toEqual([]);
      expect(t.hub.posts).toHaveLength(end === "sent" ? 1 : 0);
      if (end === "waiting") expect((await t.disk())[0]).toMatchObject({ ambiguous: true, keeperOnly: true, attempts: 2 });
      if (end === "waiting") expect((await t.disk())[0]?.claim).toBeUndefined();
      outbox.close();
    }
  });

  it("decideClaim records keeperOnly with the claim, and an ambiguous entry keeps its own", () => {
    const base: OutboxEntry = { commandId: "x", session: KEY, text: "x", attachments: [], mode: "queue", createdAt: 1, status: "waiting", attempts: 1 };
    const claimed = (stored: OutboxEntry, keeperOnly: boolean) => decideClaim(stored, "me", 10 * CLAIM_STALE_MS, CLAIM_STALE_MS, keeperOnly).write;
    expect(claimed(base, true)).toMatchObject({ keeperOnly: true, attempts: 2 });
    expect(claimed({ ...base, keeperOnly: true }, false)?.keeperOnly).toBeUndefined();
    expect(claimed({ ...base, ambiguous: true, keeperOnly: true }, false)).toMatchObject({ keeperOnly: true });
    expect(claimed({ ...base, ambiguous: true }, true)?.keeperOnly).toBeUndefined();
    // A stale claim taken over: ambiguous now, and the claim's post's keeperOnly stands.
    expect(claimed({ ...base, keeperOnly: true, claim: { owner: "gone", at: 0 } }, false)).toMatchObject({ ambiguous: true, keeperOnly: true });
    expect(claimed({ ...base, claim: { owner: "gone", at: 0 } }, true)?.keeperOnly).toBeUndefined();
  });
});

describe("what an answer does", () => {
  it("2xx with a result settles a keeper-only send", async () => {
    const t = setup();
    t.hub.script.push({ status: 202, body: { ok: true, receipt: "accepted", result: { ok: true } } });
    const outbox = await t.make();
    await outbox.send({ text: "hi" });
    await drain();
    expect(t.view().entries).toEqual([]);
  });

  it("2xx with receipt rejected fails it with the hub's reason", async () => {
    const t = setup();
    t.hub.script.push({ status: 202, body: { ok: true, receipt: "rejected", reason: "machine is too far behind" } });
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    expect(t.entry(sent.commandId)).toMatchObject({ status: "failed", error: "machine is too far behind" });
  });

  it("2xx without a receipt (a hub that ignores the field) is sent on the legacy path", async () => {
    const t = setup();
    t.hub.script.push({ status: 202, body: { ok: true, queued: true } });
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    expect(t.entry(sent.commandId)).toMatchObject({ status: "sent", path: "legacy" });
  });

  for (const lost of ["lost", "apply-and-lose", { status: 503, body: {} }, { status: 408, body: {} }, { status: 502, body: {} }] as Answer[]) {
    it(`a keeper-only first post answered ${typeof lost === "string" ? lost : lost.status}: WA, repeated keeper-only, typed in once`, async () => {
      const t = setup();
      t.hub.script.push(lost);
      const outbox = await t.make();
      const sent = await outbox.send({ text: "hi" });
      await drain();
      expect(t.entry(sent.commandId)).toMatchObject({ status: "waiting", ambiguous: true, keeperOnly: true, lastError: REPEATING });
      await t.later();
      expect(t.hub.posts.map((p) => p.require_keeper)).toEqual([true, true]);
      expect(t.entry(sent.commandId)).toMatchObject({ status: "sent", path: "keeper" });
      expect(t.hub.timesTyped(sent.commandId)).toBe(1);
    });
  }

  it("a repeat whose answer is lost again stays WA and is repeated again", async () => {
    const t = setup();
    t.hub.script.push("apply-and-lose", "lost");
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    await t.later();
    expect(t.entry(sent.commandId)).toMatchObject({ status: "waiting", ambiguous: true });
    await t.later();
    expect(t.hub.posts).toHaveLength(3);
    expect(t.entry(sent.commandId)?.status).toBe("sent");
    expect(t.hub.timesTyped(sent.commandId)).toBe(1);
  });

  it("429 or 425: a first post is W, a repeat stays WA, both tried again", async () => {
    const t = setup();
    t.hub.script.push({ status: 429, body: { error: { code: "backlog_full", message: "too many" } } });
    const outbox = await t.make();
    const first = await outbox.send({ text: "hi" });
    await drain();
    expect(t.entry(first.commandId)).toMatchObject({ status: "waiting", lastError: "too many" });
    expect(t.entry(first.commandId)?.ambiguous).toBeUndefined();
    await t.later();
    expect(t.entry(first.commandId)?.status).toBe("sent");

    t.hub.script.push("apply-and-lose", { status: 425, body: {} });
    const second = await outbox.send({ text: "again" });
    await drain();
    await t.later();
    expect(t.entry(second.commandId)).toMatchObject({ status: "waiting", ambiguous: true });
    await t.later();
    expect(t.entry(second.commandId)?.status).toBe("sent");
    expect(t.hub.timesTyped(second.commandId)).toBe(1);
  });

  it("401 on a repeat: WA, paused until wake(), then repeated keeper-only", async () => {
    const t = setup();
    t.hub.script.push("apply-and-lose", { status: 401, body: {} });
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    await t.later();
    expect(t.view().paused).toBe("unauthorized");
    expect(t.entry(sent.commandId)).toMatchObject({ status: "waiting", ambiguous: true });
    await t.later(120_000);
    expect(t.hub.posts).toHaveLength(2);
    outbox.wake();
    await drain();
    expect(t.hub.posts.map((p) => p.require_keeper)).toEqual([true, true, true]);
    expect(t.entry(sent.commandId)?.status).toBe("sent");
  });

  it("409 not_kept to a first post: W, tried again, and posted plain once the row no longer says keeper", async () => {
    const t = setup();
    t.hub.owns = false; // the row is stale: no keeper holds the session
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    expect(t.entry(sent.commandId)).toMatchObject({ status: "waiting", lastError: NOT_KEPT, attempts: 1 });
    expect(t.entry(sent.commandId)?.ambiguous).toBeUndefined();
    t.world.keeperRow = false;
    await t.later();
    expect(t.hub.posts.map((p) => p.require_keeper)).toEqual([true, undefined]);
    expect(t.entry(sent.commandId)).toMatchObject({ status: "sent", path: "legacy" });
    expect(t.entry(sent.commandId)?.keeperOnly).toBeUndefined();
    expect(t.hub.typed).toEqual([{ id: sent.commandId, route: "legacy" }]);
  });

  it("409 not_kept to a repeat: the session left its keeper, and the legacy route never gets it", async () => {
    const t = setup();
    t.hub.script.push("apply-and-lose");
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    t.hub.owns = false; // parked, then resumed in a terminal
    t.world.keeperRow = false;
    await t.later();
    expect(t.entry(sent.commandId)).toMatchObject({ status: "failed", ambiguous: true, error: MAYBE_SENT });
    expect(t.hub.typed).toEqual([{ id: sent.commandId, route: "keeper" }]);
    await t.later(OUTBOX_TTL_MS);
    expect(t.hub.posts).toHaveLength(2);
  });

  it("404 or another 4xx: definite for a first post, may-or-may-not for a repeat", async () => {
    for (const status of [404, 422]) {
      const t = setup();
      t.hub.script.push({ status, body: { error: { code: "x", message: "refused" } } });
      const outbox = await t.make();
      const first = await outbox.send({ text: "hi" });
      await drain();
      expect(t.entry(first.commandId)).toMatchObject({ status: "failed", error: status === 404 ? SESSION_GONE : "refused" });
      expect(t.entry(first.commandId)?.ambiguous).toBeUndefined();

      t.hub.script.push("apply-and-lose", { status, body: { error: { code: "x", message: "refused" } } });
      const second = await outbox.send({ text: "again" });
      await drain();
      await t.later();
      expect(t.entry(second.commandId)).toMatchObject({ status: "failed", ambiguous: true, error: MAYBE_SENT });
      outbox.close();
    }
  });

  it("409 duplicate_client_id is sent on the legacy path, as merged", async () => {
    const t = setup();
    t.hub.script.push({ status: 409, body: { error: { code: "duplicate_client_id", message: "still queued" } } });
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    expect(t.entry(sent.commandId)).toMatchObject({ status: "sent", path: "legacy" });
  });

  it("a receipt on the stream while a refused repeat is out settles it as sent", async () => {
    const t = setup();
    t.hub.script.push("apply-and-lose");
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    t.transport.on(
      "/message",
      async () => {
        outbox.noteReceipt(sent.commandId, "accepted");
        return { status: 422, body: { error: { code: "bad_attachment", message: "gone" } } };
      },
      "POST",
    );
    await t.later();
    expect(t.entry(sent.commandId)).toMatchObject({ status: "sent", path: "keeper" });
  });
});

describe("deadlines and abandoned claims", () => {
  it("WA ends may-or-may-not 24 hours after it was made", async () => {
    const t = setup();
    t.hub.script.push("lost", "lost", "lost", "lost", "lost", "lost", "lost", "lost");
    t.world.canPost = true;
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    t.world.canPost = false; // offline from here: nothing more is posted
    await t.later(OUTBOX_TTL_MS);
    expect(t.entry(sent.commandId)).toMatchObject({ status: "failed", ambiguous: true, error: MAYBE_SENT });
    outbox.close();
  });

  it("a stale claim of a keeper-only post becomes WA and is repeated; with require_keeper not listed it ends", async () => {
    for (const features of [BOTH, NEITHER]) {
      const t = setup();
      t.world.features = features;
      t.world.canPost = false; // the age pass resolves it on its own
      await t.leave({ attempts: 1, keeperOnly: true, claim: { owner: "gone", at: t.timers.now() } });
      const outbox = await t.make();
      await t.later(CLAIM_STALE_MS);
      const left = t.entry("old");
      if (features === BOTH) {
        expect(left).toMatchObject({ status: "waiting", ambiguous: true, keeperOnly: true });
        t.world.canPost = true;
        outbox.wake();
        await drain();
        expect(t.hub.posts).toEqual([expect.objectContaining({ command_id: "old", require_keeper: true })]);
      } else {
        expect(left).toMatchObject({ status: "failed", error: MAYBE_SENT });
        expect(t.hub.posts).toEqual([]);
      }
      outbox.close();
    }
  });

  it("a stale claim of a plain post still ends may-or-may-not, as merged", async () => {
    const t = setup();
    t.world.canPost = false;
    await t.leave({ attempts: 1, claim: { owner: "gone", at: t.timers.now() } });
    await t.make();
    await t.later(CLAIM_STALE_MS);
    expect(t.entry("old")).toMatchObject({ status: "failed", error: MAYBE_SENT });
  });

  it("late() after stop(): a keeper-only post with no answer is repeated by the next outbox", async () => {
    const t = setup();
    let answer: (value: JsonResponse) => void = () => {};
    let refuse: (error: Error) => void = () => {};
    t.transport.on(
      "/message",
      (path, request) =>
        t.hub.posts.length === 0
          ? new Promise((resolve, reject) => {
              t.hub.posts.push(request.body as Record<string, unknown>);
              answer = resolve;
              refuse = reject;
            })
          : Promise.resolve(t.hub.post(path, request)),
      "POST",
    );
    let next: Outbox | null = null;
    const first = await t.make({ onLate: (entry, settled) => next?.adoptLate(entry, settled) });
    const sent = await first.send({ text: "hi" });
    await drain();
    first.close();
    next = await t.make();
    void answer;
    refuse(new TypeError("dropped"));
    await drain();
    await t.later();
    expect(t.hub.posts.map((p) => p.require_keeper)).toEqual([true, true]);
    expect(t.entry(sent.commandId)).toMatchObject({ status: "sent", path: "keeper" });
  });
});

describe("what the lookup settles", () => {
  /** A send the hub took, then the stream missed its result; the lookup runs when `outbox.caughtUp()` is called. */
  async function taken(t: ReturnType<typeof setup>) {
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    expect(t.entry(sent.commandId)?.status).toBe("sent");
    return { outbox, id: sent.commandId };
  }

  it("S-K with a stored ok or cancelled result: removed", async () => {
    for (const result of [{ ok: true }, { ok: false, cancelled: true }]) {
      const t = setup();
      const { outbox, id } = await taken(t);
      t.hub.store.set(id, { receipt: "accepted", result });
      outbox.caughtUp();
      await drain();
      expect(t.view().entries).toEqual([]);
      expect(t.transport.calls.filter((c) => c.path.startsWith(LOOKUP))).toEqual([{ path: `${LOOKUP}${id}`, request: {} }]);
    }
  });

  it("S-K with a failed result: F with its note", async () => {
    const t = setup();
    const { outbox, id } = await taken(t);
    t.hub.store.set(id, { receipt: "accepted", result: { ok: false, note: "session stayed busy" } });
    outbox.caughtUp();
    await drain();
    expect(t.entry(id)).toMatchObject({ status: "failed", error: "session stayed busy" });
    expect(t.entry(id)?.ambiguous).toBeUndefined();
  });

  it("S-K with receipt rejected: F with its reason; with another receipt: unchanged", async () => {
    const t = setup();
    const { outbox, id } = await taken(t);
    outbox.caughtUp();
    await drain();
    expect(t.entry(id)?.status).toBe("sent");
    t.hub.store.set(id, { receipt: "rejected", reason: "expired" });
    outbox.caughtUp();
    await drain();
    expect(t.entry(id)).toMatchObject({ status: "failed", error: "expired" });
  });

  it("U-K with a stored ok result: removed", async () => {
    const t = setup();
    await t.leave({ status: "unconfirmed", path: "keeper", error: KEEPER_UNCONFIRMED, endedAt: t.timers.now() });
    t.hub.store.set("old", { receipt: "accepted", result: { ok: true } });
    await t.make();
    await drain();
    expect(t.view().entries).toEqual([]);
  });

  it("U-K with receipt rejected: unchanged", async () => {
    const t = setup();
    await t.leave({ status: "unconfirmed", path: "keeper", error: KEEPER_UNCONFIRMED, endedAt: t.timers.now() });
    t.hub.store.set("old", { receipt: "rejected", reason: "expired" });
    await t.make();
    await drain();
    expect(t.entry("old")).toMatchObject({ status: "unconfirmed", error: KEEPER_UNCONFIRMED });
  });

  it("FA with a receipt the hub stored: S-K, and its own deadline runs", async () => {
    const t = setup();
    await t.leave({ status: "failed", ambiguous: true, error: MAYBE_SENT, endedAt: t.timers.now() });
    t.hub.store.set("old", { receipt: "queued" });
    await t.make();
    await drain();
    expect(t.entry("old")).toMatchObject({ status: "sent", path: "keeper", receipt: "queued" });
    await t.later(OUTBOX_TTL_MS);
    expect(t.entry("old")?.status).toBe("unconfirmed");
  });

  it("FA with a failed result or receipt rejected: F", async () => {
    for (const stored of [{ receipt: "accepted", result: { ok: false, note: "not delivered" } }, { receipt: "rejected", reason: "not delivered" }]) {
      const t = setup();
      await t.leave({ status: "failed", ambiguous: true, error: MAYBE_SENT, endedAt: t.timers.now() });
      t.hub.store.set("old", stored);
      await t.make();
      await drain();
      expect(t.entry("old")).toMatchObject({ status: "failed", error: "not delivered" });
      expect(t.entry("old")?.ambiguous).toBeUndefined();
    }
  });

  it("404, another status, or no answer changes nothing", async () => {
    for (const status of [404, 500, 0]) {
      const t = setup();
      await t.leave({ status: "failed", ambiguous: true, error: MAYBE_SENT, endedAt: t.timers.now() });
      if (status === 0) t.transport.down = true;
      else if (status !== 404) t.hub.lookupStatus = status;
      await t.make();
      await drain();
      expect(t.entry("old")).toMatchObject({ status: "failed", ambiguous: true, error: MAYBE_SENT });
    }
  });

  it("asks nothing with command_lookup not listed or not known, and never posts a message", async () => {
    for (const features of [null, NEITHER, { requireKeeper: true, lookup: false }]) {
      const t = setup();
      t.world.features = features;
      await t.leave({ status: "failed", ambiguous: true, error: MAYBE_SENT, endedAt: t.timers.now() });
      const outbox = await t.make();
      outbox.caughtUp();
      await drain();
      expect(t.hub.lookups).toEqual([]);
      expect(t.hub.posts).toEqual([]);
    }
  });

  it("asks only for S-K, U-K and FA, at start, after a catch-up, and when the feature becomes known", async () => {
    const t = setup();
    t.world.features = null;
    const now = t.timers.now();
    for (const [id, over] of [
      ["sk", { status: "sent", path: "keeper" }],
      ["sl", { status: "sent", path: "legacy", sentAt: now }],
      ["uk", { status: "unconfirmed", path: "keeper", endedAt: now }],
      ["ul", { status: "unconfirmed", path: "legacy", endedAt: now }],
      ["fa", { status: "failed", ambiguous: true, endedAt: now }],
      ["f", { status: "failed", endedAt: now }],
    ] as const) {
      await t.leave({ commandId: id, ...over });
    }
    const outbox = await t.make();
    await drain();
    expect(t.hub.lookups).toEqual([]);
    t.world.features = BOTH;
    outbox.featuresChanged();
    await drain();
    expect([...t.hub.lookups].sort()).toEqual(["fa", "sk", "uk"]);
    outbox.caughtUp();
    await drain();
    expect(t.hub.lookups).toHaveLength(6);
    // A second call while one runs is folded into it.
    outbox.caughtUp();
    outbox.caughtUp();
    outbox.caughtUp();
    await drain();
    expect(t.hub.lookups).toHaveLength(12);
    expect(t.hub.posts).toEqual([]);
  });
});

describe("two tabs and older builds", () => {
  it("two pages repeating one WA send post it one after the other, and it is typed in once", async () => {
    const t = setup();
    t.hub.store.set("old", { receipt: "accepted" });
    await t.leave({ ambiguous: true, keeperOnly: true });
    const a = t.build(await openOutboxStorage(t.factory));
    const b = t.build(await openOutboxStorage(t.factory));
    await Promise.all([a.init(), b.init()]);
    await drain();
    await t.later(CLAIM_STALE_MS);
    expect(t.hub.posts.every((p) => p.require_keeper === true)).toBe(true);
    expect(t.hub.posts.length).toBeGreaterThanOrEqual(1);
    expect(t.hub.timesTyped("old")).toBe(0);
    expect(await t.disk()).toMatchObject([{ status: "sent", path: "keeper" }]);
  });

  it("an outbox with no way to know the features never posts a WA send, and its deadline ends it", async () => {
    // An older build never reads `keeperOnly`: it claims the send, finds it ambiguous and ends it (the merged
    // code, covered by outbox.test.ts). This build without a features source holds it instead.
    const t = setup();
    await t.leave({ ambiguous: true, keeperOnly: true });
    const outbox = t.build(await openOutboxStorage(t.factory), { features: () => null, keeperRow: () => false });
    await outbox.init();
    await drain();
    expect(t.entry("old")?.status).toBe("waiting");
    await t.later(OUTBOX_TTL_MS);
    expect(t.hub.posts).toEqual([]);
    expect(t.entry("old")).toMatchObject({ status: "failed", error: MAYBE_SENT });
  });

  it("a W send after a keeper-only 429 is posted plain by an outbox without the features: nothing was stored", async () => {
    const t = setup();
    await t.leave({ keeperOnly: true, lastError: "too many" });
    const outbox = t.build(await openOutboxStorage(t.factory), { features: () => null, keeperRow: () => false });
    await outbox.init();
    await drain();
    expect(t.hub.posts).toEqual([expect.not.objectContaining({ require_keeper: true })]);
    expect(t.hub.timesTyped("old")).toBe(1);
  });
});

describe("what a WA send shows", () => {
  it("says it will be sent again under the same id, and offers no Discard", async () => {
    const t = setup();
    t.hub.script.push("lost");
    const outbox = await t.make();
    const sent = await outbox.send({ text: "hi" });
    await drain();
    const [view] = describeSends({
      outbox: t.view(),
      connection: { state: "live", attempt: 0, since: 0, retryAt: null, detail: "" },
      loaded: true,
      online: true,
    });
    expect(view).toMatchObject({ commandId: sent.commandId, shown: "waiting", canDiscard: false, canRetry: false });
    expect(view?.reason).toBe(`${REPEATING}. Trying again.`);
    const left: OutboxEntry = { ...(t.entry(sent.commandId) as OutboxEntry) };
    delete left.lastError;
    const [quiet] = describeSends({
      outbox: { ...t.view(), entries: [left] },
      connection: { state: "live", attempt: 0, since: 0, retryAt: null, detail: "" },
      loaded: true,
      online: true,
    });
    expect(quiet?.reason).toContain("sent again under the same id");
  });

  it("the feature names are the hub's", () => {
    expect(PROTOCOL.features).toEqual(expect.arrayContaining(["require_keeper", "command_lookup"]));
  });
});
