// G32: a running session's options in the reducer, the store's setOption, and the selector a picker reads
// (docs/specs/G32-session-options.md).
import { describe, expect, it } from "vitest";

import { describeOptions, FEATURE_SESSION_OPTIONS } from "./options";
import { reduce } from "./reducer";
import { emptySession } from "./state";
import { MemoryStorage } from "./outbox-storage";
import { createSessionStore } from "./store";
import { FakeEnvironment, FakeTimers, FakeTransport, drain, snapshotFrame } from "./test-support";
import type { JsonRequest } from "./transport";
import type { Caps, Frame, SessionOptions, Snapshot } from "./types";

const T = "claude:s1";
const LOOKUP = "/v1/sessions/claude%3As1/commands/";
const FEATURES = ["sync_v2", "snapshot", "command_id", "require_keeper", "command_lookup", FEATURE_SESSION_OPTIONS, "session_runtime"];
const CAPS: Caps = { steer: true, plan_mode: true, rollback: false, fork: true, stop_task: true, options: ["model", "permission_mode"] };
const CHOICES = [{ id: "default" }, { id: "sonnet" }, { id: "haiku" }];

const offered = (model = "haiku", plan = false, reported?: string): SessionOptions => ({
  model: { value: model, mid_turn: "unverified", choices: CHOICES, ...(reported === undefined ? {} : { reported }) },
  permission_mode: { value: plan ? "plan" : "default", mid_turn: "unverified" },
  plan_mode: { value: plan, mid_turn: "unverified" },
});

const env = (type: string, data: unknown, extra: Record<string, unknown> = {}): Frame =>
  ({ v: 2, type, session: T, ts: 1, ...extra, data }) as unknown as Frame;
const optionsFrame = (seq: number, options: SessionOptions): Frame => env("session.options", { options }, { seq });
const result = (seq: number, commandId: string, ok: boolean, note?: string): Frame =>
  env("command.result", { op: "set_option", ok, ...(note === undefined ? {} : { note }) }, { seq, command_id: commandId });

const snap = (head: number, over: Partial<Snapshot> = {}): Snapshot =>
  ({
    session: T, epoch: 0, head, boot_id: "b1", row: { key: T, mode: "keeper", control: "full" }, state: "idle", caps: CAPS,
    options: offered(), open_turn: null, pending: [], queue: [], live: [], plan: null, tasks: [], checkpoints: [], changes: null,
    usage: null, messages: [], has_more: false, next_before: null, ...over,
  }) as Snapshot;

function setup(over: { features?: string[]; snapshot?: Partial<Snapshot> } = {}) {
  const timers = new FakeTimers();
  const transport = new FakeTransport();
  const features = over.features ?? FEATURES;
  transport.on("/v1/status", () => ({ status: 200, body: { ok: true, v: 1, protocols: [1, 2], features, boot_id: "b1" } }));
  const posts: Record<string, unknown>[] = [];
  // The hub's answer to each post; a test replaces it. By default the keeper takes it.
  const answer: { next: (body: Record<string, unknown>) => { status: number; body: unknown } | Promise<{ status: number; body: unknown }> } = {
    next: (body) => ({ status: 200, body: { ok: true, receipt: "accepted", command_id: body.command_id } }),
  };
  transport.on(
    "/command",
    async (_path, request: JsonRequest) => {
      const body = request.body as Record<string, unknown>;
      posts.push(body);
      return answer.next(body);
    },
    "POST",
  );
  let n = 0;
  const store = createSessionStore({
    sessionKey: T, transport, timers, random: () => 0, environment: new FakeEnvironment(),
    openStorage: async () => new MemoryStorage("mem"),
    newId: () => `id${++n}`,
    silenceMs: 48 * 60 * 60 * 1000,
  });
  const state = () => store.getState();
  let seq = 10;
  const send = async (...frames: Frame[]) => {
    transport.last.send(...frames);
    await drain();
  };
  const catchUp = async (head = 5) => {
    await send(snapshotFrame(snap(head, over.snapshot)), env("session.state", { state: "idle", live: [], pending: [], head, epoch: 0 }, { seq: 0 }));
  };
  const next = () => ++seq;
  return { timers, transport, posts, answer, store, state, send, catchUp, next };
}

describe("the reducer", () => {
  it("keeps the options of a session.options frame, and of a snapshot", () => {
    let s = reduce(emptySession(), snapshotFrame(snap(4))).state;
    expect(s.options?.model?.value).toBe("haiku");
    s = reduce(s, optionsFrame(5, offered("sonnet", false, "claude-sonnet-5-5"))).state;
    expect(s.options?.model).toEqual({ value: "sonnet", mid_turn: "unverified", choices: CHOICES, reported: "claude-sonnet-5-5" });
    // An older one arriving late (a replay overlapping the live stream) does not undo it.
    s = reduce(s, optionsFrame(3, offered("default"))).state;
    expect(s.options?.model?.value).toBe("sonnet");
  });

  it("knows nothing from a hub that sends no options", () => {
    const old = snap(4);
    delete (old as { options?: unknown }).options;
    const s = reduce(emptySession(), snapshotFrame(old)).state;
    expect(s.options).toBeNull();
    expect(describeOptions({ session: s, connection: { state: "live", attempt: 0, since: 0, retryAt: null, detail: "" }, supported: false, work: {} })).toEqual({ known: false, turnOpen: false });
  });
});

describe("the selector", () => {
  it("shows what the session offers: the value in force, the agent's list, and what can change", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    const view = t.state().sessionOptions;
    expect(view.known).toBe(true);
    expect(view.model).toMatchObject({ value: "haiku", choices: CHOICES, midTurn: "unverified", pending: null, error: null, canChange: true });
    expect(view.permission_mode).toMatchObject({ value: "default", choices: null, canChange: true });
    expect(view.plan_mode).toMatchObject({ value: false, canChange: true });
    // Effort is not offered by this session: not there at all.
    expect(view.effort).toBeUndefined();
  });

  it("says why nothing can change before the stream is live and caught up", () => {
    const t = setup();
    t.state().start();
    expect(t.state().sessionOptions.known).toBe(false);
  });

  it("follows the turn, so a picker can label a change made mid-turn", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    await t.send(env("turn.started", {}, { seq: t.next(), turn_id: "t1" }));
    expect(t.state().sessionOptions.turnOpen).toBe(true);
    expect(t.state().sessionOptions.model?.canChange).toBe(true);
  });

  it("follows the agent: a value it changed by itself (a plan mode it left) shows when the hub says so", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    await t.send(optionsFrame(t.next(), offered("haiku", true)));
    expect(t.state().sessionOptions.plan_mode?.value).toBe(true);
    await t.send(optionsFrame(t.next(), offered("haiku", false)));
    expect(t.state().sessionOptions.plan_mode?.value).toBe(false);
  });

  it("keeps the same object while nothing it shows changes", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    const before = t.state().sessionOptions;
    await t.send(env("message.delta", { id: "m1", kind: "assistant", text: "hi", seq: 1, offset: 0 }));
    expect(t.state().sessionOptions).toBe(before);
    // A new row object that says the same: read again, and still the same object.
    await t.send(env("session.row", { row: { key: T, mode: "keeper", control: "full" } }));
    expect(t.state().session.row).not.toBe(null);
    expect(t.state().sessionOptions).toBe(before);
  });
});

describe("setOption", () => {
  it("posts set_option with a fresh command id, shows it pending, and the value only when the hub says so", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    const sent = await t.state().setOption("model", "sonnet");
    expect(sent).toEqual({ ok: true, commandId: "id1", receipt: "accepted" });
    expect(t.posts).toEqual([{ op: "set_option", name: "model", value: "sonnet", command_id: "id1" }]);
    // No optimistic value: the hub has not said.
    expect(t.state().sessionOptions.model).toMatchObject({ value: "haiku", pending: { value: "sonnet", commandId: "id1" } });
    // The keeper logs the new options before the result.
    await t.send(optionsFrame(t.next(), offered("sonnet", false, "claude-sonnet-5-5")));
    expect(t.state().sessionOptions.model).toMatchObject({ value: "sonnet", reported: "claude-sonnet-5-5", pending: { value: "sonnet" } });
    await t.send(result(t.next(), "id1", true));
    expect(t.state().sessionOptions.model).toMatchObject({ value: "sonnet", pending: null, error: null });
  });

  it("plan mode takes a flag", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    await t.state().setOption("plan_mode", true);
    expect(t.posts).toEqual([{ op: "set_option", name: "plan_mode", value: true, command_id: "id1" }]);
  });

  it("a change the hub refuses shows its reason as the hub said it", async () => {
    const t = setup();
    t.answer.next = () => ({ status: 409, body: { ok: false, error: { code: "command_rejected", message: "model must be one short token" } } });
    t.state().start();
    await t.catchUp();
    const sent = await t.state().setOption("model", "two words");
    expect(sent).toEqual({ ok: false, reason: "refused", message: "model must be one short token", status: 409 });
    expect(t.state().sessionOptions.model).toMatchObject({
      value: "haiku", pending: null, error: { value: "two words", message: "model must be one short token" },
    });
  });

  it("a change the agent fails shows the result's note unaltered, and the next change clears it", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    await t.state().setOption("model", "opus");
    const note = "claude refused set_model: Model 'opus' is not available";
    await t.send(result(t.next(), "id1", false, note));
    expect(t.state().sessionOptions.model).toMatchObject({ value: "haiku", pending: null, error: { value: "opus", message: note } });
    await t.state().setOption("model", "sonnet");
    expect(t.state().sessionOptions.model?.error).toBeNull();
  });

  it("a refused receipt on the stream settles it", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    await t.state().setOption("model", "sonnet");
    await t.send(env("command.receipt", { status: "rejected", reason: "unsupported", op: "set_option" }, { seq: t.next(), command_id: "id1" }));
    expect(t.state().sessionOptions.model).toMatchObject({ pending: null, error: { message: "unsupported" } });
  });

  it("a result that came down the stream before the post's answer still settles it", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    t.answer.next = async (body) => {
      t.transport.last.send(result(t.next(), String(body.command_id), true));
      return { status: 200, body: { ok: true, receipt: "accepted", command_id: body.command_id } };
    };
    await t.state().setOption("model", "sonnet");
    await drain();
    expect(t.state().sessionOptions.model?.pending).toBeNull();
  });

  it("two quick changes end on the last one: one change in flight at a time, and only the latest waits", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    await t.state().setOption("model", "sonnet");
    expect(await t.state().setOption("model", "opus")).toEqual({ ok: true, commandId: null, receipt: "deferred" });
    expect(await t.state().setOption("model", "default")).toEqual({ ok: true, commandId: null, receipt: "deferred" });
    expect(t.posts.map((p) => p.value)).toEqual(["sonnet"]);
    expect(t.state().sessionOptions.model?.pending).toEqual({ value: "default", commandId: null });
    await t.send(optionsFrame(t.next(), offered("sonnet")), result(t.next(), "id1", true));
    // The first settled: the latest goes, the one in between never does.
    expect(t.posts.map((p) => [p.value, p.command_id])).toEqual([["sonnet", "id1"], ["default", "id2"]]);
    expect(t.state().sessionOptions.model?.pending).toEqual({ value: "default", commandId: "id2" });
    await t.send(optionsFrame(t.next(), offered("default")), result(t.next(), "id2", true));
    expect(t.state().sessionOptions.model).toMatchObject({ value: "default", pending: null });
  });

  it("the latest change goes even when the one before it failed", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    await t.state().setOption("model", "opus");
    await t.state().setOption("model", "sonnet");
    await t.send(result(t.next(), "id1", false, "no"));
    expect(t.posts.map((p) => p.value)).toEqual(["opus", "sonnet"]);
  });

  it("is not sent again when the latest change is the one that just took", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    await t.state().setOption("model", "opus");
    await t.state().setOption("model", "sonnet");
    await t.state().setOption("model", "opus");
    await t.send(result(t.next(), "id1", true));
    expect(t.posts.map((p) => p.value)).toEqual(["opus"]);
    expect(t.state().sessionOptions.model?.pending).toBeNull();
  });

  it("a result the stream missed is looked up after the next catch-up", async () => {
    const t = setup();
    t.transport.on(LOOKUP, () => ({
      status: 200,
      body: { ok: true, session: T, command_id: "id1", op: "set_option", receipt: "accepted", result: { ok: false, note: "the hub restarted before this was delivered" }, received_at: 1, updated_at: 2 },
    }));
    t.state().start();
    await t.catchUp();
    await t.state().setOption("model", "sonnet");
    expect(t.transport.calls.filter((c) => c.path.startsWith(LOOKUP))).toHaveLength(0);
    t.timers.advance(10_000);
    t.transport.last.end({ kind: "closed" });
    await drain();
    await t.catchUp(20);
    await drain();
    expect(t.transport.calls.filter((c) => c.path.startsWith(LOOKUP)).map((c) => c.path.slice(LOOKUP.length))).toEqual(["id1"]);
    expect(t.state().sessionOptions.model).toMatchObject({ pending: null, error: { message: "the hub restarted before this was delivered" } });
  });

  it("a lookup answered after its change settled does not touch the change sent after it", async () => {
    const t = setup();
    t.state().start();
    await t.catchUp();
    await t.state().setOption("model", "sonnet");
    await t.state().setOption("model", "opus");
    t.transport.on(LOOKUP, async () => {
      // While the lookup is out, the stream replays id1's result: id2 goes.
      t.transport.last.send(result(t.next(), "id1", true));
      await drain();
      return {
        status: 200,
        body: { ok: true, session: T, command_id: "id1", op: "set_option", receipt: "accepted", result: { ok: false, note: "late" }, received_at: 1, updated_at: 2 },
      };
    });
    t.timers.advance(10_000);
    t.transport.last.end({ kind: "closed" });
    await drain();
    await t.catchUp(20);
    await drain();
    expect(t.posts.map((p) => p.command_id)).toEqual(["id1", "id2"]);
    expect(t.state().sessionOptions.model).toMatchObject({ pending: { value: "opus", commandId: "id2" }, error: null });
  });

  it("a network failure ends the change with the reason", async () => {
    const t = setup();
    t.answer.next = () => {
      throw new TypeError("Failed to fetch");
    };
    t.state().start();
    await t.catchUp();
    expect(await t.state().setOption("model", "sonnet")).toEqual({ ok: false, reason: "network", message: "Failed to fetch" });
    expect(t.state().sessionOptions.model).toMatchObject({ pending: null, error: { value: "sonnet", message: "Failed to fetch" } });
  });
});

describe("the gates", () => {
  it("a hub that does not list session_options: refused without a post, and the selector says why", async () => {
    const t = setup({ features: FEATURES.filter((f) => f !== FEATURE_SESSION_OPTIONS) });
    t.state().start();
    await t.catchUp();
    expect(await t.state().setOption("model", "sonnet")).toMatchObject({ ok: false, reason: "unsupported_hub" });
    expect(t.posts).toEqual([]);
    expect(t.state().sessionOptions.model).toMatchObject({ canChange: false, reason: "unsupported_hub" });
  });

  it("an option the caps do not allow: refused without a post", async () => {
    const t = setup({ snapshot: { caps: { ...CAPS, plan_mode: false, options: ["permission_mode"] } } });
    t.state().start();
    await t.catchUp();
    expect(await t.state().setOption("model", "sonnet")).toMatchObject({ ok: false, reason: "not_allowed" });
    expect(await t.state().setOption("plan_mode", true)).toMatchObject({ ok: false, reason: "not_allowed" });
    expect(t.posts).toEqual([]);
    expect(t.state().sessionOptions.model).toMatchObject({ canChange: false, reason: "not_allowed", message: "This session cannot change the model." });
    expect(t.state().sessionOptions.permission_mode?.canChange).toBe(true);
  });

  it("an option the session does not offer: refused without a post", async () => {
    const t = setup({ snapshot: { caps: { ...CAPS, options: ["model", "effort", "permission_mode"] } } });
    t.state().start();
    await t.catchUp();
    expect(await t.state().setOption("effort", "high")).toMatchObject({ ok: false, reason: "not_allowed" });
    expect(t.posts).toEqual([]);
  });

  it("a session SWE does not run itself: refused", async () => {
    const t = setup({ snapshot: { row: { key: T, mode: "tmux", control: "full" } } });
    t.state().start();
    await t.catchUp();
    expect(await t.state().setOption("model", "sonnet")).toMatchObject({ ok: false, reason: "not_kept" });
    expect(t.posts).toEqual([]);
  });

  it("a read-only session, and a stream that is not live: refused with the gate's reason", async () => {
    const t = setup({ snapshot: { row: { key: T, mode: "keeper", control: "none", control_reason: "Resumed in a terminal" } } });
    t.state().start();
    await t.catchUp();
    expect(await t.state().setOption("model", "sonnet")).toEqual({ ok: false, reason: "read_only", message: "Resumed in a terminal" });
    const u = setup();
    u.state().start();
    expect(await u.state().setOption("model", "sonnet")).toMatchObject({ ok: false, reason: "not_live" });
    expect([...t.posts, ...u.posts]).toEqual([]);
  });
});
