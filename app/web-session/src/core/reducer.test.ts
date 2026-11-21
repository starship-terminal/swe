import { describe, expect, it } from "vitest";

import { applyHistory, beginConnection, reduce, reduceAll, type Effect } from "./reducer";
import { emptyIndex, emptySession, indexGet, indexSet, type SessionState, type TimelineItem, type ToolItem } from "./state";
import { fixtureNames, loadFixture, snapshotFrame, type Fixture } from "./test-support";
import { seqOf, type Frame, type Snapshot } from "./types";

// ---------------------------------------------------------------------
// Frames made by hand, for the rules the recordings do not reach. Every
// other test replays what the hub really sent.

const S = "claude:t1";
let ts = 1_760_000_000;

function env<T extends Frame["type"]>(
  type: T,
  data: Extract<Frame, { type: T }>["data"],
  extra: { seq?: number; turn_id?: string; command_id?: string } = {},
): Frame {
  return { v: 2, type, session: S, ts: ts++, ...extra, data } as unknown as Frame;
}

const delta = (id: string, text: string, offset: number, extra: { done?: boolean } = {}) =>
  env("message.delta", { id, kind: "assistant", text, seq: 1, offset, ...extra }, { turn_id: "t1" });

function snapshotBody(over: Partial<Snapshot> = {}): Snapshot {
  return {
    session: S,
    epoch: 0,
    head: 0,
    boot_id: "boot-1",
    row: { key: S },
    state: "idle",
    caps: null,
    open_turn: null,
    pending: [],
    queue: [],
    live: [],
    plan: null,
    tasks: [],
    checkpoints: [],
    changes: null,
    usage: null,
    messages: [],
    has_more: false,
    next_before: null,
    ...over,
  };
}

function start(over: Partial<Snapshot> = {}): SessionState {
  return reduce(emptySession(), snapshotFrame(snapshotBody(over))).state;
}

const syncState = (live: { id: string; text: string }[], head = 0, epoch = 0, pending: string[] = []) =>
  env(
    "session.state",
    { state: "running", live: live.map((m) => ({ kind: "assistant" as const, ...m })), pending, head, epoch },
    { seq: 0 },
  );

function feed(state: SessionState, ...frames: Frame[]): { state: SessionState; effects: Effect[] } {
  return reduceAll(state, frames);
}

const itemsOf = (state: SessionState, kind: TimelineItem["kind"]) => state.items.filter((i) => i.kind === kind);

// ---------------------------------------------------------------------
// Replaying the recordings

function replay(fixture: Fixture, stopAt?: [number, number]): SessionState {
  let state = emptySession();
  fixture.connections.forEach((connection, c) => {
    state = beginConnection(state);
    connection.frames.forEach((frame, f) => {
      if (stopAt !== undefined && (c > stopAt[0] || (c === stopAt[0] && f > stopAt[1]))) return;
      const out = reduce(state, frame);
      expect(out.effects, `${fixture.scenario} ${c}/${f} ${frame.type}`).toEqual([]);
      state = out.state;
    });
  });
  return state;
}

/** The fields of the state the snapshot also holds, in the snapshot's own shape. */
function strip(items: readonly TimelineItem[]): unknown[] {
  // `tool.updated` is bookkeeping a snapshot leaves out.
  return items.map((item) => {
    if (item.kind !== "tool") return item;
    const { text: _text, ...rest } = item as ToolItem;
    return rest;
  });
}

describe("a recorded stream adds up to the snapshot the hub holds at its end", () => {
  for (const name of fixtureNames()) {
    it(name, () => {
      const fixture = loadFixture(name);
      const last: [number, number] = [
        fixture.connections.length - 1,
        (fixture.connections[fixture.connections.length - 1] as Fixture["connections"][number]).frames.length - 1,
      ];
      const final = fixture.snapshots.find((s) => s.after[0] === last[0] && s.after[1] === last[1]);
      expect(final, "the recording ends with a snapshot").toBeDefined();
      const body = (final as Fixture["snapshots"][number]).body;

      const live = replay(fixture);
      const reference = reduce(emptySession(), snapshotFrame(body)).state;

      expect(live.cursor).toBe(body.head);
      expect(live.epoch).toBe(body.epoch);
      expect(live.phase).toBe(body.state);
      expect(live.openTurn).toBe(body.open_turn);
      expect(live.caps).toEqual(body.caps);
      expect(live.plan).toEqual(body.plan);
      expect(live.changes).toEqual(body.changes);
      expect(live.usage).toEqual(body.usage);
      expect(live.queue).toEqual(body.queue);
      expect(live.pending).toEqual(body.pending);
      expect(live.live.map(({ length: _l, ...m }) => m)).toEqual(
        body.live.map((m) => ({ ...m })),
      );
      // Tasks and checkpoints are the same whether built from the frames or from the snapshot, restores included.
      expect(live.tasks).toEqual(reference.tasks);
      expect(live.checkpoints).toEqual(reference.checkpoints);
      // And the snapshot's own lists say the same: its tasks are the running ones, its checkpoints the made ones.
      expect(live.tasks.filter((t) => t.status === "running").map(({ seq: _s, ...t }) => t)).toEqual(body.tasks);
      expect(live.checkpoints.map(({ restoredSeq: _r, ...c }) => c)).toEqual(body.checkpoints);
      expect(strip(live.items)).toEqual(strip(reference.items));
      expect(live.turns).toEqual(reference.turns);
      // The row's state and status are as of the last snapshot or row frame; the live phase is `phase`.
      const rowOf = (row: SessionState["row"]) => {
        const { state: _s, status: _st, last_active_at: _l, ...rest } = row ?? { key: "" };
        return rest;
      };
      expect(rowOf(live.row)).toEqual(rowOf(body.row));
    });
  }
});

describe("the default recording", () => {
  const fixture = loadFixture("default");
  const state = replay(fixture);

  it("draws every kind of timeline item", () => {
    expect(new Set(state.items.map((i) => i.kind))).toEqual(
      new Set(["message", "thinking", "tool", "checkpoint"]),
    );
    const thinking = itemsOf(state, "thinking")[0];
    expect(thinking).toMatchObject({ text: "Let me look at the file first.", durationS: 1.5 });
  });

  it("keeps a tool call as one item from start to completion, with its file edits", () => {
    const edit = state.items.find((i): i is ToolItem => i.kind === "tool" && i.name === "Edit") as ToolItem;
    expect(edit.status).toBe("ok");
    expect(edit.fileEdits).toEqual([
      { path: "src/app.py", kind: "modify", diff: "@@ -1 +1 @@\n-print('hi')\n+print('hello')\n" },
    ]);
    expect(edit.completedSeq).toBeGreaterThan(edit.seq);
    const failed = state.items.find((i): i is ToolItem => i.kind === "tool" && i.callId === "call-9") as ToolItem;
    expect(failed).toMatchObject({ status: "error", output: "1 failing", text: "running 3 tests", summary: "npm test" });
  });

  it("holds the plan, tasks, checkpoints, changes and usage", () => {
    expect(state.plan?.items.map((i) => i.status)).toEqual(["completed", "in_progress", "pending"]);
    expect(state.tasks).toMatchObject([{ task_id: "task-1", status: "completed", title: "Explore the repo" }]);
    expect(state.checkpoints).toMatchObject([{ checkpoint_id: "ck-1", files: 1, restoredSeq: expect.any(Number) }]);
    expect(state.changes).toEqual([{ path: "src/app.py", kind: "modify", added: 1, removed: 1 }]);
    expect(state.usage).toMatchObject({ input_tokens: 1200, context_window: 200000, cost_usd: 0.012 });
  });

  it("ends each turn and ends the live text with it", () => {
    expect(state.turns.map((t) => [t.id, t.status])).toEqual([
      ["t1", "completed"],
      ["t2", "completed"],
    ]);
    expect(state.openTurn).toBeNull();
    expect(state.live).toEqual([]);
    expect(state.phase).toBe("idle");
  });

  it("records the command receipts and results by id", () => {
    expect(state.commands["m2"]).toMatchObject({ receipt: { status: expect.any(String) }, result: { ok: true } });
    expect(state.lastNotice?.n).toBeGreaterThan(0);
  });
});

describe("the streaming recording", () => {
  const fixture = loadFixture("streaming");

  it("appends each delta at its offset and ends the text when the message completes", () => {
    let state = emptySession();
    const seen: string[] = [];
    for (const frame of fixture.connections[0]?.frames ?? []) {
      state = reduce(state, frame).state;
      seen.push(state.live[0]?.text ?? "");
    }
    expect(seen).toContain("Hello wor");
    expect(seen.at(-1)).toBe("");
    expect(state.items.at(-1)).toMatchObject({ kind: "message", role: "assistant", text: "Hello world" });
  });

  it("raises the cursor to a ping's head", () => {
    let state = reduce(emptySession(), (fixture.connections[0] as Fixture["connections"][number]).frames[0] as Frame).state;
    const head = state.cursor;
    state = reduce(state, env("ping", { ts: 1, head: head + 4 })).state;
    expect(state.cursor).toBe(head + 4);
    state = reduce(state, env("ping", { ts: 2, head: head })).state;
    expect(state.cursor).toBe(head + 4);
  });

  it("recovers from a lost delta by asking for the snapshot and taking its live[]", () => {
    const frames = (fixture.connections[0] as Fixture["connections"][number]).frames;
    const lost = frames.findIndex((f) => f.type === "message.delta" && f.data.offset === 3);
    expect(lost).toBeGreaterThan(0);
    let state = emptySession();
    const effects: Effect[] = [];
    for (const [i, frame] of frames.entries()) {
      if (i === lost) continue; // never arrives
      const out = reduce(state, frame);
      state = out.state;
      effects.push(...out.effects);
      if (frame.type === "message.delta" && frame.data.offset === 6) break;
    }
    // The third chunk did not line up: dropped, appends stopped, snapshot asked for.
    expect(effects).toEqual([{ type: "fetch_snapshot", reason: "hole", messageId: "a1" }]);
    expect(state.live.map((l) => l.text)).toEqual(["Hel"]);
    expect(state.stopped).toEqual(["a1"]);
    // Further deltas for that message are ignored until the snapshot's text replaces it.
    const next = reduce(state, frames.find((f) => f.type === "message.delta" && f.data.offset === 9) as Frame);
    expect(next.state.live.map((l) => l.text)).toEqual(["Hel"]);
    expect(next.effects).toEqual([]);
  });
});

// ---------------------------------------------------------------------
// The delta rule

describe("the delta rule", () => {
  it("appends a delta whose offset is the length of the text held", () => {
    const state = feed(start(), delta("a", "Hel", 0), delta("a", "lo", 3, { done: true })).state;
    expect(state.live).toEqual([{ id: "a", kind: "assistant", text: "Hello", length: 5, turnId: "t1", done: true }]);
  });

  it("ignores a delta behind the text held that ends inside it", () => {
    const state = feed(start(), delta("a", "Hello", 0)).state;
    const out = reduce(state, delta("a", "llo", 2));
    expect(out.effects).toEqual([]);
    expect(out.state.live[0]?.text).toBe("Hello");
    expect(out.state.stopped).toEqual([]);
  });

  it("treats a delta that starts behind the text but runs past it as a hole", () => {
    const state = feed(start(), delta("a", "Hello", 0)).state;
    const out = reduce(state, delta("a", "lo wor", 3));
    expect(out.effects).toEqual([{ type: "fetch_snapshot", reason: "hole", messageId: "a" }]);
    expect(out.state.live[0]?.text).toBe("Hello");
  });

  it("treats a first delta that does not start at 0 as a hole", () => {
    const out = reduce(start(), delta("a", "lo", 3));
    expect(out.effects).toHaveLength(1);
    expect(out.state.live).toEqual([]);
    expect(out.state.stopped).toEqual(["a"]);
  });

  it("keeps other messages going while one is stopped", () => {
    let state = feed(start(), delta("a", "A", 0), delta("b", "B", 0)).state;
    state = reduce(state, delta("a", "zz", 9)).state;
    state = reduce(state, delta("b", "b", 1)).state;
    expect(state.live.map((l) => [l.id, l.text])).toEqual([
      ["a", "A"],
      ["b", "Bb"],
    ]);
  });

  it("counts the offset in code points, as the hub does, not in UTF-16 units", () => {
    // "😀" is two UTF-16 units and one code point. The hub's next offset is 1.
    const state = feed(start(), delta("a", "😀", 0)).state;
    expect(state.live[0]?.length).toBe(1);
    const out = reduce(state, delta("a", "!", 1));
    expect(out.effects).toEqual([]);
    expect(out.state.live[0]?.text).toBe("😀!");
    expect(out.state.live[0]?.length).toBe(2);
  });

  it("resumes from the state frame's live[] after a hole", () => {
    let state = feed(start(), delta("a", "Hel", 0), delta("a", "xx", 9)).state;
    expect(state.stopped).toEqual(["a"]);
    state = reduce(state, syncState([{ id: "a", text: "Hello wor" }], 5)).state;
    expect(state.stopped).toEqual([]);
    state = reduce(state, delta("a", "ld", 9)).state;
    expect(state.live[0]?.text).toBe("Hello world");
  });

  it("ends the text on message.completed, thinking.completed, tool.started and turn.ended", () => {
    const withLive = () =>
      feed(start(), delta("a", "x", 0), delta("call-1", "y", 0), delta("th", "z", 0)).state;
    expect(
      reduce(withLive(), env("message.completed", { id: "a", kind: "assistant", text: "x!" }, { seq: 1, turn_id: "t1" }))
        .state.live.map((l) => l.id),
    ).toEqual(["call-1", "th"]);
    expect(
      reduce(withLive(), env("thinking.completed", { id: "th", text: "z!" }, { seq: 1, turn_id: "t1" })).state.live.map(
        (l) => l.id,
      ),
    ).toEqual(["a", "call-1"]);
    expect(
      reduce(withLive(), env("tool.started", { call_id: "call-1", name: "Bash" }, { seq: 1, turn_id: "t1" })).state.live.map(
        (l) => l.id,
      ),
    ).toEqual(["a", "th"]);
    expect(reduce(withLive(), env("turn.ended", { status: "completed" }, { seq: 1, turn_id: "t1" })).state.live).toEqual([]);
  });

  it("ends all live text when a journaled state says the session is idle, parked, closed or down", () => {
    for (const phase of ["idle", "parked", "closed", "agent_down"] as const) {
      const state = feed(start(), delta("a", "x", 0), env("session.state", { state: phase }, { seq: 4 })).state;
      expect(state.live, phase).toEqual([]);
    }
    const running = feed(start(), delta("a", "x", 0), env("session.state", { state: "running" }, { seq: 4 })).state;
    expect(running.live).toHaveLength(1);
  });

  it("replaces a streamed text with the whole text of its message", () => {
    const state = feed(
      start(),
      delta("a", "Hel", 0),
      env("message.completed", { id: "a", kind: "assistant", text: "Hello" }, { seq: 3, turn_id: "t1" }),
    ).state;
    expect(state.live).toEqual([]);
    expect(itemsOf(state, "message")[0]).toMatchObject({ text: "Hello", seq: 3 });
  });
});

// ---------------------------------------------------------------------
// Snapshot, state frame, row

describe("session.snapshot", () => {
  it("replaces everything the client holds", () => {
    let state = feed(
      start({ head: 3, epoch: 0 }),
      delta("a", "x", 0),
      env("message.completed", { id: "m1", kind: "user", text: "old" }, { seq: 4, turn_id: "t1" }),
    ).state;
    state = reduce(
      state,
      snapshotFrame(
        snapshotBody({
          head: 9,
          epoch: 1,
          boot_id: "boot-2",
          state: "running",
          open_turn: "t5",
          live: [{ id: "z", kind: "assistant", text: "Hi 😀" }],
          queue: [{ command_id: "q1", text: "later" }],
          plan: { items: [{ text: "a", status: "pending" }] },
          changes: [{ path: "a", kind: "add" }],
          usage: { input_tokens: 1 },
          has_more: true,
          next_before: 12,
        }),
      ),
    ).state;
    expect(state.items).toEqual([]);
    expect(state.live).toEqual([{ id: "z", kind: "assistant", text: "Hi 😀", length: 4 }]);
    expect(state).toMatchObject({
      cursor: 9,
      epoch: 1,
      bootId: "boot-2",
      phase: "running",
      openTurn: "t5",
      hasMore: true,
      nextBefore: 12,
    });
    expect(state.queue).toHaveLength(1);
    expect(state.plan?.items).toHaveLength(1);
  });

  it("lists the open requests from its own pending[], even one older than its messages or one step ahead", () => {
    const asked = env(
      "approval.requested",
      { request_id: "r1", kind: "tool", prompt: "Run it?" },
      { seq: 11, turn_id: "t1" },
    ) as Extract<Frame, { type: "approval.requested" }>;
    let state = start({ head: 10, pending: [asked] });
    expect(state.pending.map((p) => p.data.request_id)).toEqual(["r1"]);
    expect(itemsOf(state, "request")).toHaveLength(1);
    // The same event follows on the stream: applied by id, once.
    state = reduce(state, asked).state;
    expect(state.pending).toHaveLength(1);
    expect(itemsOf(state, "request")).toHaveLength(1);
    expect(state.cursor).toBe(11);
  });

  it("takes the hub's boot id, and carries the connection's own facts over", () => {
    let state = reduce(start(), env("command.result", { op: "send", ok: true }, { seq: 0, command_id: "c1" })).state;
    state = beginConnection(state);
    state = reduce(state, snapshotFrame(snapshotBody({ boot_id: "boot-9" }))).state;
    expect(state.bootId).toBe("boot-9");
    expect(state.commands["c1"]?.result?.ok).toBe(true);
    expect(state.caughtUp).toBe(false);
  });
});

describe("the state frame that ends a catch-up", () => {
  it("replaces the live text, takes head and epoch, and marks the connection caught up", () => {
    let state = feed(start(), delta("old", "gone", 0)).state;
    state = beginConnection(state);
    expect(state.caughtUp).toBe(false);
    state = reduce(state, syncState([{ id: "a", text: "Hel" }], 12)).state;
    expect(state.live.map((l) => [l.id, l.text])).toEqual([["a", "Hel"]]);
    expect(state.cursor).toBe(12);
    expect(state.caughtUp).toBe(true);
  });

  it("drops the request cards it does not list and leaves ids it does not know", () => {
    const ask = (id: string, seq: number) =>
      env("approval.requested", { request_id: id, kind: "tool", prompt: id }, { seq, turn_id: "t1" });
    let state = feed(start(), ask("r1", 1), ask("r2", 2)).state;
    expect(state.pending).toHaveLength(2);
    // r1 is closed; r3 is one it has not seen: its event is on its way.
    state = reduce(state, syncState([], 2, 0, ["r2", "r3"])).state;
    expect(state.pending.map((p) => p.data.request_id)).toEqual(["r2"]);
    const dropped = state.items.find((i) => i.kind === "request" && i.requestId === "r1");
    expect(dropped).toMatchObject({ resolution: { source: "dropped" } });
    // The event for r3 then arrives and is kept.
    state = reduce(state, ask("r3", 3)).state;
    expect(state.pending.map((p) => p.data.request_id)).toEqual(["r2", "r3"]);
  });

  it("never lowers the cursor", () => {
    let state = reduce(start({ head: 20 }), syncState([], 15)).state;
    expect(state.cursor).toBe(20);
    state = reduce(state, syncState([], 25)).state;
    expect(state.cursor).toBe(25);
  });

  it("asks for a snapshot when it is on an epoch the client does not hold", () => {
    const out = reduce(start({ epoch: 2 }), syncState([], 5, 3));
    expect(out.effects).toEqual([{ type: "fetch_snapshot", reason: "epoch" }]);
    expect(out.state.epoch).toBe(2);
  });
});

describe("session.row", () => {
  it("replaces the row", () => {
    const state = reduce(start(), env("session.row", { row: { key: S, title: "New", control: "read_only" } })).state;
    expect(state.row).toEqual({ key: S, title: "New", control: "read_only" });
  });
});

// ---------------------------------------------------------------------
// seq: duplicates, order, gaps, the cursor

describe("seq", () => {
  const msg = (seq: number, text = `m${seq}`) =>
    env("message.completed", { id: `id${seq}`, kind: "user", text }, { seq, turn_id: "t1" });

  it("keeps the cursor at the highest journaled seq", () => {
    const state = feed(start(), msg(3), msg(7), msg(5)).state;
    expect(state.cursor).toBe(7);
  });

  it("allows gaps", () => {
    const state = feed(start(), msg(2), msg(9), msg(40)).state;
    expect(state.items.map((i) => i.seq)).toEqual([2, 9, 40]);
    expect(state.cursor).toBe(40);
  });

  it("applies a frame seen twice once", () => {
    const state = feed(start(), msg(1), msg(2), msg(2), msg(1)).state;
    expect(state.items.map((i) => i.seq)).toEqual([1, 2]);
  });

  it("puts an out-of-order frame in its place", () => {
    const state = feed(start(), msg(1), msg(4), msg(3), msg(2)).state;
    expect(state.items.map((i) => i.seq)).toEqual([1, 2, 3, 4]);
    expect(state.cursor).toBe(4);
  });

  it("never lets a seq of 0 move the cursor", () => {
    const state = feed(start({ head: 5 }), env("notify", { kind: "finished", message: "x" }, { seq: 0 })).state;
    expect(state.cursor).toBe(5);
    expect(state.lastNotice).toMatchObject({ n: 1, message: "x" });
  });

  it("ignores a state event older than the one applied, but still counts its seq", () => {
    let state = feed(
      start(),
      env("queue.updated", { items: [{ command_id: "a", text: "new" }] }, { seq: 9 }),
      env("queue.updated", { items: [] }, { seq: 8 }),
    ).state;
    expect(state.queue.map((q) => q.text)).toEqual(["new"]);
    state = reduce(state, env("session.state", { state: "idle" }, { seq: 12 })).state;
    state = reduce(state, env("session.state", { state: "running" }, { seq: 10 })).state;
    expect(state.phase).toBe("idle");
    expect(state.cursor).toBe(12);
  });

  it("ignores a journaled event a snapshot already includes", () => {
    let state = start({ head: 10, plan: { items: [{ text: "from the snapshot", status: "pending" }] } });
    state = reduce(state, env("plan.updated", { items: [{ text: "older", status: "pending" }] }, { seq: 7 })).state;
    expect(state.plan?.items[0]?.text).toBe("from the snapshot");
  });
});

describe("an epoch change", () => {
  it("is a snapshot on the new epoch: the old timeline and cursor go", () => {
    let state = feed(
      start({ head: 50 }),
      env("message.completed", { id: "a", kind: "user", text: "old" }, { seq: 51, turn_id: "t1" }),
    ).state;
    expect(state.cursor).toBe(51);
    state = reduce(state, snapshotFrame(snapshotBody({ head: 4, epoch: 1 }))).state;
    expect(state).toMatchObject({ epoch: 1, cursor: 4, items: [] });
    // And the new numbering is accepted: seq 5 is the next event, not a stale one.
    state = reduce(
      state,
      env("message.completed", { id: "b", kind: "user", text: "new" }, { seq: 5, turn_id: "t2" }),
    ).state;
    expect(state.items.map((i) => i.seq)).toEqual([5]);
  });
});

// ---------------------------------------------------------------------
// Requests, tools, tasks, checkpoints by id

describe("requests", () => {
  const ask = (id: string, seq: number) =>
    env("approval.requested", { request_id: id, kind: "tool", prompt: id }, { seq, turn_id: "t1" });
  const answer = (id: string, seq: number, source: "user" | "autonomy" = "user", by = "phone") =>
    env("approval.resolved", { request_id: id, source, decision: "allow", by }, { seq, turn_id: "t1" });

  it("opens on a request and closes on its answer", () => {
    let state = feed(start(), ask("r1", 1)).state;
    expect(state.pending).toHaveLength(1);
    state = reduce(state, answer("r1", 2)).state;
    expect(state.pending).toEqual([]);
    expect(itemsOf(state, "request")[0]).toMatchObject({
      resolution: { seq: 2, source: "user", decision: "allow", by: "phone" },
    });
  });

  it("applies a repeated request and a repeated answer once, whichever order the snapshot left them in", () => {
    // The snapshot already shows r1 answered; its request arrives again afterwards.
    let state = feed(start(), ask("r1", 1), answer("r1", 2)).state;
    state = feed(state, ask("r1", 1), answer("r1", 2)).state;
    expect(state.pending).toEqual([]);
    expect(itemsOf(state, "request")).toHaveLength(1);
  });

  it("keeps the first answer: any device can answer and the first wins", () => {
    const state = feed(start(), ask("r1", 1), answer("r1", 2, "user", "phone"), answer("r1", 3, "user", "laptop")).state;
    expect(itemsOf(state, "request")[0]).toMatchObject({ resolution: { by: "phone" } });
  });

  it("does not reopen a request that was answered before it was seen", () => {
    const state = feed(start(), answer("r9", 5), ask("r9", 4)).state;
    expect(state.pending).toEqual([]);
  });

  it("opens a new request when an id is used again after the first was answered", () => {
    // An agent that restarts starts its counters over: perm-1 again, at a later seq.
    let state = feed(start(), ask("perm-1", 1), answer("perm-1", 2)).state;
    state = reduce(state, ask("perm-1", 7)).state;
    expect(state.pending.map((p) => p.seq)).toEqual([7]);
    expect(itemsOf(state, "request").map((i) => [i.seq, (i as { resolution?: unknown }).resolution === undefined])).toEqual([
      [1, false],
      [7, true],
    ]);
    // Its answer closes the new one, not the old.
    state = reduce(state, answer("perm-1", 9, "user", "laptop")).state;
    expect(state.pending).toEqual([]);
    expect(itemsOf(state, "request").map((i) => (i as { resolution?: { by?: string } }).resolution?.by)).toEqual(["phone", "laptop"]);
    // The same two events again change nothing.
    state = feed(state, ask("perm-1", 7), answer("perm-1", 9, "user", "laptop")).state;
    expect(state.pending).toEqual([]);
    expect(itemsOf(state, "request")).toHaveLength(2);
  });

  it("holds an input request like an approval", () => {
    const state = feed(
      start(),
      env("input.requested", { request_id: "q1", prompt: "Which branch?", kind: "text" }, { seq: 1, turn_id: "t1" }),
    ).state;
    expect(state.pending[0]?.type).toBe("input.requested");
  });
});

describe("tools", () => {
  it("shows a completion whose start is older than the client's history", () => {
    const state = feed(
      start(),
      env("tool.completed", { call_id: "c9", status: "ok", name: "Bash", output: "x" }, { seq: 6, turn_id: "t1" }),
    ).state;
    expect(state.items[0]).toMatchObject({ kind: "tool", callId: "c9", status: "ok", name: "Bash" });
  });

  it("shows a call that reuses the id of an earlier one as a new call", () => {
    const started = (seq: number) => env("tool.started", { call_id: "toolu_1", name: "Bash" }, { seq, turn_id: "t1" });
    const completed = (seq: number) => env("tool.completed", { call_id: "toolu_1", status: "ok" }, { seq, turn_id: "t1" });
    const state = feed(start(), started(1), completed(2), started(5), started(5), completed(6)).state;
    expect(state.items.map((i) => [i.seq, (i as ToolItem).status])).toEqual([
      [1, "ok"],
      [5, "ok"],
    ]);
  });

  it("takes a start that arrives after its completion into the completion's item", () => {
    const state = feed(
      start(),
      env("tool.completed", { call_id: "c", status: "ok", output: "x" }, { seq: 4, turn_id: "t1" }),
      env("tool.started", { call_id: "c", name: "Bash", summary: "ls" }, { seq: 3, turn_id: "t1" }),
    ).state;
    expect(state.items).toHaveLength(1);
    expect(state.items[0]).toMatchObject({ seq: 4, name: "Bash", summary: "ls", status: "ok", output: "x" });
  });

  it("ignores an update for a call it does not hold", () => {
    const state = reduce(start(), env("tool.updated", { call_id: "zz", text: "x" }, { seq: 0 })).state;
    expect(state.items).toEqual([]);
  });
});

describe("tasks and checkpoints", () => {
  it("updates a task in place and keeps the order they started in", () => {
    const task = (id: string, status: "running" | "completed", seq: number) =>
      env("task.updated", { task_id: id, kind: "subagent", status }, { seq });
    const state = feed(start(), task("a", "running", 1), task("b", "running", 2), task("a", "completed", 3)).state;
    expect(state.tasks.map((t) => [t.task_id, t.status])).toEqual([
      ["a", "completed"],
      ["b", "running"],
    ]);
  });

  it("marks a checkpoint restored", () => {
    const state = feed(
      start(),
      env("checkpoint.created", { checkpoint_id: "ck", files: 2 }, { seq: 1, turn_id: "t1" }),
      env("checkpoint.restored", { checkpoint_id: "ck", by: "phone" }, { seq: 2, turn_id: "t2" }),
    ).state;
    expect(state.checkpoints).toMatchObject([{ checkpoint_id: "ck", seq: 1, turn_id: "t1", restoredSeq: 2 }]);
    expect(itemsOf(state, "checkpoint").map((i) => (i as { what: string }).what)).toEqual(["created", "restored"]);
  });
});

describe("commands", () => {
  it("keeps receipts and results by command id, a bounded number of them", () => {
    let state = start();
    for (let n = 0; n < 205; n++) {
      state = reduce(state, env("command.receipt", { status: "accepted" }, { seq: 0, command_id: `c${n}` })).state;
    }
    state = reduce(state, env("command.result", { op: "send", ok: false, note: "no" }, { seq: 0, command_id: "c204" })).state;
    expect(state.commandOrder).toHaveLength(200);
    expect(state.commands["c0"]).toBeUndefined();
    expect(state.commands["c204"]).toMatchObject({ receipt: { status: "accepted" }, result: { ok: false, note: "no" } });
  });
});

describe("older history", () => {
  it("adds a page of events before the oldest and moves the page marker", () => {
    let state = feed(
      start({ head: 10, has_more: true, next_before: 8 }),
      env("message.completed", { id: "new", kind: "user", text: "new" }, { seq: 9, turn_id: "t2" }),
    ).state;
    state = applyHistory(state, {
      session: S,
      messages: [
        env("message.completed", { id: "a", kind: "user", text: "a" }, { seq: 2, turn_id: "t1" }),
        env("message.completed", { id: "b", kind: "assistant", text: "b" }, { seq: 3, turn_id: "t1" }),
      ] as never,
      has_more: false,
      next_before: null,
    });
    expect(state.items.map((i) => i.seq)).toEqual([2, 3, 9]);
    expect(state).toMatchObject({ hasMore: false, nextBefore: null, cursor: 10 });
  });
});

describe("older history with reused ids", () => {
  it("gives an older call and request their own items and leaves the newer ones alone", () => {
    const call = (type: "tool.started" | "tool.completed", seq: number, extra: object) =>
      env(type, { call_id: "toolu_1", ...(type === "tool.started" ? { name: "Bash" } : { status: "ok" }), ...extra } as never, { seq, turn_id: "t1" });
    const ask = (seq: number) => env("approval.requested", { request_id: "perm-1", kind: "tool", prompt: String(seq) }, { seq, turn_id: "t1" });
    const answer = (seq: number, by: string) => env("approval.resolved", { request_id: "perm-1", source: "user", decision: "allow", by }, { seq, turn_id: "t1" });
    // The client holds the newer instances (seq 100 and up) and is then given a page with the older ones.
    let state = feed(start({ head: 110, has_more: true, next_before: 50 }), call("tool.started", 100, {}), call("tool.completed", 101, { output: "new" }), ask(102), answer(103, "laptop")).state;
    state = applyHistory(state, {
      session: S,
      messages: [call("tool.started", 10, {}), call("tool.completed", 11, { output: "old" }), ask(12), answer(13, "phone")] as never,
      has_more: false,
      next_before: null,
    });
    const tools = state.items.filter((i): i is ToolItem => i.kind === "tool");
    expect(tools.map((t) => [t.seq, t.output, t.completedSeq])).toEqual([
      [10, "old", 11],
      [100, "new", 101],
    ]);
    const requests = state.items.filter((i) => i.kind === "request") as unknown as { seq: number; resolution?: { by?: string } }[];
    expect(requests.map((r) => [r.seq, r.resolution?.by])).toEqual([
      [12, "phone"],
      [102, "laptop"],
    ]);
    // Loading the same page again changes nothing.
    const again = applyHistory(state, { session: S, messages: [call("tool.started", 10, {}), ask(12)] as never, has_more: false, next_before: null });
    expect(again.items).toEqual(state.items);
    // And the newest instance is still the one later events find.
    state = reduce(state, call("tool.completed", 120, { output: "newest" })).state;
    expect((state.items.find((i) => i.seq === 100) as ToolItem).output).toBe("newest");
  });
});

describe("a frame seen twice", () => {
  const started = (id: string, seq: number) => env("turn.started", { by: "d" }, { seq, turn_id: id });
  const ended = (id: string, seq: number) => env("turn.ended", { status: "completed" }, { seq, turn_id: id });

  it("does not wipe the next turn's live text or reopen an ended turn", () => {
    let state = feed(start(), started("t1", 1), ended("t1", 2), started("t2", 3), delta("a2", "Hel", 0)).state;
    expect(state.live.map((l) => l.text)).toEqual(["Hel"]);
    state = reduce(state, ended("t1", 2)).state;
    expect([state.cursor, state.live.map((l) => l.text), state.openTurn]).toEqual([3, ["Hel"], "t2"]);
    state = reduce(state, started("t1", 1)).state;
    expect(state.openTurn).toBe("t2");
    expect(state.turns.map((t) => [t.id, t.endedSeq])).toEqual([["t1", 2], ["t2", undefined]]);
  });

  it("does not end live text again, drop a task's newer state, or move a checkpoint", () => {
    const msg = env("message.completed", { id: "a", kind: "assistant", text: "x" }, { seq: 1, turn_id: "t1" });
    const task = (status: "running" | "completed", seq: number) => env("task.updated", { task_id: "k", kind: "subagent", status }, { seq });
    let state = feed(start(), msg, delta("a", "next", 0), task("running", 4), task("completed", 5)).state;
    state = feed(state, msg, task("running", 4), task("completed", 5)).state;
    expect(state.live.map((l) => l.text)).toEqual(["next"]);
    expect(state.tasks.map((t) => [t.task_id, t.status, t.seq])).toEqual([["k", "completed", 5]]);
    const created = env("checkpoint.created", { checkpoint_id: "ck", files: 1 }, { seq: 6, turn_id: "t1" });
    state = feed(state, created, env("checkpoint.restored", { checkpoint_id: "ck" }, { seq: 7, turn_id: "t1" }), created).state;
    expect(state.checkpoints).toMatchObject([{ checkpoint_id: "ck", seq: 6, restoredSeq: 7 }]);
  });

  it("changes nothing when every journaled frame of every recording is applied a second time", () => {
    for (const name of fixtureNames()) {
      const fixture = loadFixture(name);
      const journaled = fixture.connections.flatMap((c) => c.frames).filter((f) => seqOf(f) > 0);
      // From a snapshot, so nothing needs an earlier frame; then the journaled frames, twice.
      const base = reduce(emptySession(), snapshotFrame(fixture.snapshots[0]?.body as Snapshot)).state;
      const once = reduceAll(base, journaled).state;
      const twice = reduceAll(once, journaled).state;
      const { applied: _a, ...a } = once;
      const { applied: _b, ...b } = twice;
      expect(b, name).toEqual(a);
    }
  });
});

describe("a turn that ends after a newer one has started", () => {
  it("clears only the text of the turn that ended", () => {
    const delta2 = env("message.delta", { id: "a2", kind: "assistant", text: "Hel", seq: 1, offset: 0 }, { turn_id: "t2" });
    const delta1 = env("message.delta", { id: "a1", kind: "assistant", text: "old", seq: 1, offset: 0 }, { turn_id: "t1" });
    let state = feed(
      start(),
      env("turn.started", {}, { seq: 1, turn_id: "t1" }),
      delta1,
      env("turn.started", {}, { seq: 4, turn_id: "t2" }),
      delta2,
    ).state;
    state = reduce(state, env("turn.ended", { status: "completed" }, { seq: 5, turn_id: "t1" })).state;
    expect(state.live.map((l) => [l.id, l.text])).toEqual([["a2", "Hel"]]);
    expect(state.openTurn).toBe("t2");
    state = reduce(state, env("turn.ended", { status: "completed" }, { seq: 6, turn_id: "t2" })).state;
    expect(state.live).toEqual([]);
    expect(state.openTurn).toBeNull();
  });
});

describe("checkpoints older than a snapshot's messages", () => {
  const made = (id: string, seq: number) => env("checkpoint.created", { checkpoint_id: id, files: 1 }, { seq, turn_id: "t1" });
  it("come first, as they do when a stream gave them", () => {
    const fromFrames = feed(start(), made("ck-old", 1), made("ck-new", 50), env("checkpoint.restored", { checkpoint_id: "ck-old" }, { seq: 51, turn_id: "t1" })).state;
    // The snapshot's window starts after ck-old: it is in checkpoints[] only, and its restore is in messages[].
    const body = snapshotBody({
      head: 51,
      checkpoints: [{ checkpoint_id: "ck-old", files: 1, seq: 1, ts: fromFrames.checkpoints[0]?.ts ?? 0, turn_id: "t1" }, { checkpoint_id: "ck-new", files: 1, seq: 50, ts: fromFrames.checkpoints[1]?.ts ?? 0, turn_id: "t1" }],
      messages: [made("ck-new", 50), env("checkpoint.restored", { checkpoint_id: "ck-old" }, { seq: 51, turn_id: "t1" })] as never,
    });
    const fromSnapshot = reduce(emptySession(), snapshotFrame(body)).state;
    expect(fromSnapshot.checkpoints.map((c) => [c.checkpoint_id, c.restoredSeq])).toEqual([
      ["ck-old", 51],
      ["ck-new", undefined],
    ]);
    expect(fromSnapshot.checkpoints.map((c) => c.checkpoint_id)).toEqual(fromFrames.checkpoints.map((c) => c.checkpoint_id));
  });
});

describe("a command result with no id", () => {
  it("is recorded nowhere", () => {
    const state = reduce(start(), env("command.result", { op: "send", ok: true }, { seq: 0, command_id: "" })).state;
    expect(state.commandOrder).toEqual([]);
  });
});

describe("cost", () => {
  it("grows with the length of a session, not with its square", () => {
    // 16,000 tool calls took 35 s when the id index was copied on every insert.
    let state = start();
    const t0 = performance.now();
    for (let n = 1; n <= 16_000; n++) {
      state = reduce(state, env("tool.started", { call_id: `toolu_${n}`, name: "Bash" }, { seq: 2 * n - 1, turn_id: "t1" })).state;
      state = reduce(state, env("tool.completed", { call_id: `toolu_${n}`, status: "ok" }, { seq: 2 * n, turn_id: "t1" })).state;
    }
    const elapsed = performance.now() - t0;
    expect(state.items).toHaveLength(16_000);
    expect(state.items.every((i) => (i as ToolItem).status === "ok")).toBe(true);
    expect(elapsed).toBeLessThan(5000);
  });

  it("the id index finds every id it was given and the newest instance of a reused one", () => {
    let index = emptyIndex();
    for (let n = 0; n < 2000; n++) index = indexSet(index, `id-${n}`, n);
    for (let n = 0; n < 2000; n += 97) expect(indexGet(index, `id-${n}`)).toBe(n);
    expect(indexGet(index, "nope")).toBeUndefined();
    const before = index;
    index = indexSet(index, "id-5", 99);
    expect([indexGet(before, "id-5"), indexGet(index, "id-5")]).toEqual([5, 99]);
  });
});

describe("purity", () => {
  it("never changes the state it is given", () => {
    const before = feed(start(), delta("a", "x", 0)).state;
    const frozen = JSON.stringify(before);
    reduce(before, delta("a", "y", 1));
    reduce(before, env("message.completed", { id: "m", kind: "user", text: "t" }, { seq: 1, turn_id: "t1" }));
    reduce(before, snapshotFrame(snapshotBody()));
    expect(JSON.stringify(before)).toBe(frozen);
  });
});

// ---------------------------------------------------------------------
// What each recording shows, frame by frame

/** The state after each frame of each connection, in order. */
function trace(name: string): { frame: Frame; state: SessionState; connection: number }[] {
  const fixture = loadFixture(name);
  let state = emptySession();
  const out: { frame: Frame; state: SessionState; connection: number }[] = [];
  fixture.connections.forEach((connection, c) => {
    state = beginConnection(state);
    for (const frame of connection.frames) {
      state = reduce(state, frame).state;
      out.push({ frame, state, connection: c });
    }
  });
  return out;
}

describe("the approval recording", () => {
  const steps = trace("approval");

  it("shows the approval pending, then the question, then neither", () => {
    const pendingIds = steps.map((s) => s.state.pending.map((p) => p.data.request_id).join(","));
    expect(pendingIds).toContain("r1");
    expect(pendingIds).toContain("r2");
    expect(steps.at(-1)?.state.pending).toEqual([]);
    const request = steps.at(-1)?.state.items.filter((i) => i.kind === "request") ?? [];
    expect(request.map((i) => (i as { resolution?: { source: string } }).resolution?.source)).toEqual(["user", "user"]);
  });

  it("gives a second device that connects while it is open the same pending request", () => {
    const second = steps.filter((s) => s.connection === 1);
    const caught = second.find((s) => s.state.caughtUp) as (typeof steps)[number];
    expect(caught.state.pending.map((p) => p.data.request_id)).toEqual(["r1"]);
    expect(caught.state.phase).toBe("waiting");
  });
});

describe("the queued and steer recordings", () => {
  it("holds a message in the queue until the turn ends, then sends it", () => {
    const steps = trace("queued");
    const queues = steps.map((s) => s.state.queue.map((q) => q.command_id).join(","));
    expect(queues).toContain("q1");
    expect(steps.at(-1)?.state.queue).toEqual([]);
    expect(steps.at(-1)?.state.items.some((i) => i.kind === "message" && i.text === "then lint it")).toBe(true);
    expect(steps.at(-1)?.state.commands["q1"]?.result).toMatchObject({ ok: true });
  });

  it("takes a steered message out of the queue and into the turn in flight", () => {
    const steps = trace("steer");
    const queues = steps.map((s) => s.state.queue.map((q) => q.command_id).join(","));
    expect(queues).toEqual(expect.arrayContaining(["q1", "q1,q2", "q1", ""]));
    const end = steps.at(-1)?.state as SessionState;
    expect(end.items.find((i) => i.kind === "message" && i.text === "text q2")).toMatchObject({
      // The steer is a command of its own: the message carries its id, not the queued message's.
      commandId: expect.stringMatching(/^hub-command-/),
    });
    // The queued message itself ends as "removed from the queue" (cancelled), whatever happened to its text.
    expect(end.commands["q2"]?.result).toMatchObject({ ok: false, cancelled: true });
    expect(end.commands["hub-command-2"]?.result).toMatchObject({ ok: true });
    expect(end.commands["q1"]?.result).toMatchObject({ ok: false, cancelled: true });
  });
});

describe("the reconnect recording", () => {
  it("continues a reply from the state frame's text, then completes it", () => {
    const steps = trace("reconnect");
    const caught = steps.find((s) => s.connection === 1 && s.state.caughtUp) as (typeof steps)[number];
    expect(caught.state.live.map((l) => l.text)).toEqual(["Hello wor"]);
    // The small gap was replayed: no snapshot on the second connection.
    expect(steps.filter((s) => s.connection === 1).some((s) => s.frame.type === "session.snapshot")).toBe(false);
    expect(steps.at(-1)?.state.items.find((i) => i.kind === "message" && i.role === "assistant")).toMatchObject({
      text: "Hello world",
    });
    expect(steps.at(-1)?.state.live).toEqual([]);
  });
});

describe("the overflow recording", () => {
  it("gets every event it missed, then the text in progress, and carries on from its end", () => {
    const steps = trace("overflow");
    const recovered = steps.find((s) => s.frame.type === "session.state" && s.state.live.length > 0) as (typeof steps)[number];
    expect(recovered.state.live.map((l) => [l.id, l.text])).toEqual([["a2", "still "]]);
    expect(recovered.state.items.filter((i) => i.kind === "tool")).toHaveLength(6);
    const end = steps.at(-1)?.state as SessionState;
    expect(end.items.at(-1)).toMatchObject({ kind: "message", text: "still going" });
  });
});

describe("the epoch change recording", () => {
  it("takes a snapshot on the new epoch while connected, and again on reconnecting with the old one", () => {
    const steps = trace("epoch_change");
    const epochs = steps.filter((s) => s.frame.type === "session.snapshot").map((s) => s.state.epoch);
    expect(epochs).toEqual([0, 1, 1]);
    expect(steps.at(-1)?.state.epoch).toBe(1);
    expect(steps.filter((s) => s.connection === 1).map((s) => s.frame.type)).toEqual(["session.snapshot", "session.state"]);
  });
});

describe("the failed turn recording", () => {
  it("shows the error and compaction in the timeline and the turn as failed", () => {
    const state = (trace("failed_turn").at(-1) as { state: SessionState }).state;
    expect(state.items.find((i) => i.kind === "error")).toMatchObject({ code: "agent_error", message: "The agent could not continue." });
    expect(state.items.find((i) => i.kind === "compacted")).toMatchObject({ tokensBefore: 90000 });
    expect(state.turns.at(-1)).toMatchObject({ status: "failed" });
  });
});
