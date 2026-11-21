// The reducer: v2 frames in, session state out.
//
// It is a pure function (`reduce(state, frame)`) and holds the rules of
// docs/specs/G9-sync-v2.md, "What G11 must know":
//
// - The cursor is the highest journaled `seq` seen, raised to the `head` of
//   every state frame and ping. A `seq` of 0 never moves it.
// - A `session.snapshot` replaces everything. A `session.row` replaces the row.
// - The `session.state` frame with `live[]` that ends every catch-up replaces
//   the live text, drops the request cards it does not list, and sets the
//   cursor to its `head`.
// - Live text is keyed by the delta's `id`. A delta is appended only at the
//   offset the client holds; one behind the text held is ignored; a hole
//   stops appends to that message and asks the store for a snapshot.
// - Timeline items are keyed by `seq`, so a frame seen twice, or out of
//   order, lands once and in its place. Requests and commands are applied by
//   id, because a snapshot can be one event ahead of its `head`.
// - Gaps in `seq` are allowed: the hub leaves out events a view does not draw.
//
// What the reducer cannot do on its own it returns as an `Effect`.
import {
  COMMANDS_KEPT,
  EARLY_ANSWERS_KEPT,
  emptySession,
  indexGet,
  indexSet,
  type CheckpointInfo,
  type CheckpointItem,
  type CommandRecord,
  type CompactedItem,
  type ErrorItem,
  type LiveText,
  type MessageItem,
  type RequestItem,
  type Resolution,
  type SessionState,
  type TaskInfo,
  type ThinkingItem,
  type TimelineItem,
  type ToolItem,
  type TurnInfo,
} from "./state";
import {
  codePointLength,
  seqOf,
  type Frame,
  type FrameOf,
  type LiveMessage,
  type MessagesPage,
  type Snapshot,
} from "./types";

/** Work for the store that the reducer cannot do itself. */
export type Effect = {
  type: "fetch_snapshot";
  /** `hole`: a delta did not line up. `epoch`: a state frame is on an epoch the client does not hold. */
  reason: "hole" | "epoch";
  messageId?: string;
};

export interface Reduced {
  state: SessionState;
  effects: Effect[];
}

/** A copy of `o` without the keys whose value is undefined (the types forbid an explicit undefined). */
function defined<T extends object>(o: { [K in keyof T]: T[K] | undefined }): T {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(o)) if (value !== undefined) out[key] = value;
  return out as T;
}

/** The states in which the hub ends every message in progress. */
const ENDS_LIVE = new Set(["idle", "agent_down", "parked", "closed"]);

/** The event types whose latest value is the state, so an older one arriving late is ignored. */
const SINGLETONS = [
  "session.state",
  "session.caps",
  "plan.updated",
  "changes.updated",
  "queue.updated",
  "usage.updated",
] as const;

function toLive(message: LiveMessage): LiveText {
  return defined<LiveText>({
    id: message.id,
    kind: message.kind,
    text: message.text,
    length: codePointLength(message.text),
    turnId: message.turn_id,
    done: message.done,
  });
}

// ---------------------------------------------------------------------
// Timeline items

function findSeq(items: readonly TimelineItem[], seq: number): number {
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((items[mid] as TimelineItem).seq < seq) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Add an item in `seq` order. An item already at that `seq` stays as it is. */
function insertItem(s: SessionState, item: TimelineItem): void {
  const items = s.items;
  const last = items[items.length - 1];
  if (last === undefined || item.seq > last.seq) {
    s.items = items.concat(item);
  } else {
    const at = findSeq(items, item.seq);
    if ((items[at] as TimelineItem).seq === item.seq) return;
    s.items = [...items.slice(0, at), item, ...items.slice(at)];
  }
  // The index points at the latest instance of an id: an older one (a page of history) never takes it over.
  if (item.kind === "tool" && item.seq > (indexGet(s.index.tool, item.callId) ?? -1)) {
    s.index = { ...s.index, tool: indexSet(s.index.tool, item.callId, item.seq) };
  }
  if (item.kind === "request" && item.seq > (indexGet(s.index.request, item.requestId) ?? -1)) {
    s.index = { ...s.index, request: indexSet(s.index.request, item.requestId, item.seq) };
  }
}

function replaceItem(s: SessionState, seq: number, update: (item: TimelineItem) => TimelineItem): void {
  const at = findSeq(s.items, seq);
  const item = s.items[at];
  if (item === undefined || item.seq !== seq) return;
  const next = update(item);
  if (next === item) return;
  const copy = s.items.slice();
  copy[at] = next;
  s.items = copy;
}

/**
 * The item of the latest instance of an id at or before `atOrBefore`. An id can be used again (an
 * agent that restarts counts from its first id), so a page of older history must match its events
 * to their own instance, not to the newest one the index points at.
 */
function instance<T extends TimelineItem>(
  s: SessionState,
  kind: T["kind"],
  latest: number | undefined,
  matches: (item: T) => boolean,
  atOrBefore: number,
): T | undefined {
  if (latest !== undefined && latest <= atOrBefore) {
    const item = s.items[findSeq(s.items, latest)];
    return item !== undefined && item.seq === latest && item.kind === kind ? (item as T) : undefined;
  }
  // An id the index has never seen has no instance: only an older one than the latest needs a search.
  if (latest === undefined) return undefined;
  for (let at = findSeq(s.items, atOrBefore + 1) - 1; at >= 0; at--) {
    const item = s.items[at] as TimelineItem;
    if (item.kind === kind && matches(item as T)) return item as T;
  }
  return undefined;
}

function hasItemAt(s: SessionState, seq: number): boolean {
  const at = findSeq(s.items, seq);
  return s.items[at]?.seq === seq;
}

function toolItem(s: SessionState, callId: string, atOrBefore = Infinity): ToolItem | undefined {
  return instance<ToolItem>(s, "tool", indexGet(s.index.tool, callId), (item) => item.callId === callId, atOrBefore);
}

function upsertTurn(s: SessionState, id: string, patch: Partial<TurnInfo>): void {
  const at = s.turns.findIndex((turn) => turn.id === id);
  if (at < 0) {
    s.turns = [...s.turns, { id, ...patch }];
  } else {
    s.turns = [...s.turns.slice(0, at), { ...(s.turns[at] as TurnInfo), ...patch }, ...s.turns.slice(at + 1)];
  }
}

function dropLive(s: SessionState, id: string): void {
  if (s.live.some((entry) => entry.id === id)) s.live = s.live.filter((entry) => entry.id !== id);
}

/**
 * The frames that make timeline items and turns. `live` is false when
 * history is being loaded (a snapshot's `messages`, an older page): then the
 * frame only adds to the timeline, and what is true now (open turn, live
 * text, pending requests) comes from the snapshot's own fields.
 */
function applyTimeline(s: SessionState, frame: Frame, live: boolean): void {
  const seq = seqOf(frame);
  switch (frame.type) {
    case "turn.started": {
      const id = frame.turn_id;
      if (id === undefined) return;
      const known = s.turns.find((turn) => turn.id === id);
      if (seq > 0 && known?.startedSeq === seq) return; // the same event again
      upsertTurn(
        s,
        id,
        defined<TurnInfo>({ id, startedSeq: seq || undefined, startedAt: frame.ts, by: frame.data.by, text: frame.data.text }),
      );
      // A start that arrives after its turn's end (out of order) does not reopen it.
      if (live && known?.endedSeq === undefined) s.openTurn = id;
      return;
    }
    case "turn.ended": {
      const id = frame.turn_id;
      if (id === undefined) return;
      if (seq > 0 && s.turns.find((turn) => turn.id === id)?.endedSeq === seq) return; // the same event again
      upsertTurn(
        s,
        id,
        defined<TurnInfo>({
          id,
          endedSeq: seq || undefined,
          endedAt: frame.ts,
          status: frame.data.status,
          reason: frame.data.reason,
          durationS: frame.data.duration_s,
        }),
      );
      if (live) {
        // Only the text of the turn that ended: a newer turn may already be writing. Text with no turn
        // on it (a state frame's live[] may leave it out) goes with the open turn.
        const open = s.openTurn === id;
        s.live = s.live.filter((entry) => (entry.turnId === undefined ? !open : entry.turnId !== id));
        s.stopped = s.stopped.filter((stopped) => s.live.some((entry) => entry.id === stopped));
        if (open) s.openTurn = null;
      }
      return;
    }
    case "message.completed": {
      if (seq > 0 && hasItemAt(s, seq)) return; // the same event again
      if (live) dropLive(s, frame.data.id);
      if (seq <= 0) return;
      insertItem(
        s,
        defined<MessageItem>({
          kind: "message",
          seq,
          ts: frame.ts,
          turnId: frame.turn_id,
          id: frame.data.id,
          role: frame.data.kind,
          text: frame.data.text,
          by: frame.data.by,
          attachments: frame.data.attachments,
          commandId: frame.command_id,
        }),
      );
      return;
    }
    case "thinking.completed": {
      if (seq > 0 && hasItemAt(s, seq)) return;
      if (live) dropLive(s, frame.data.id);
      if (seq <= 0) return;
      insertItem(
        s,
        defined<ThinkingItem>({
          kind: "thinking",
          seq,
          ts: frame.ts,
          turnId: frame.turn_id,
          id: frame.data.id,
          text: frame.data.text,
          durationS: frame.data.duration_s,
        }),
      );
      return;
    }
    case "tool.started": {
      if (seq > 0 && hasItemAt(s, seq)) return;
      if (live) dropLive(s, frame.data.call_id);
      const known = toolItem(s, frame.data.call_id);
      if (known !== undefined && known.seq === seq) return; // the same event again
      if (known !== undefined && known.seq > seq && known.completedSeq === known.seq) {
        // The completion got here first (its item sits at the completion's seq): take the start's fields.
        replaceItem(s, known.seq, (item) =>
          item.kind === "tool"
            ? defined<ToolItem>({
                ...item,
                name: item.name ?? frame.data.name,
                summary: item.summary ?? frame.data.summary,
                input: item.input ?? frame.data.input,
                taskId: item.taskId ?? frame.data.task_id,
              })
            : item,
        );
        return;
      }
      // Otherwise a new call (an id an earlier call used is reused), or an older one in a page of history.
      if (seq <= 0) return;
      if (seq > 0 && toolItem(s, frame.data.call_id, seq)?.seq === seq) return;
      insertItem(
        s,
        defined<ToolItem>({
          kind: "tool",
          seq,
          ts: frame.ts,
          turnId: frame.turn_id,
          callId: frame.data.call_id,
          name: frame.data.name,
          summary: frame.data.summary,
          input: frame.data.input,
          taskId: frame.data.task_id,
          status: "running",
        }),
      );
      return;
    }
    case "tool.updated": {
      const known = toolItem(s, frame.data.call_id);
      if (known === undefined) return;
      replaceItem(s, known.seq, (item) =>
        item.kind === "tool"
          ? defined<ToolItem>({ ...item, text: frame.data.text ?? item.text, summary: frame.data.summary ?? item.summary })
          : item,
      );
      return;
    }
    case "tool.completed": {
      const known = toolItem(s, frame.data.call_id, seq > 0 ? seq : Infinity);
      const done = defined<Partial<ToolItem>>({
        status: frame.data.status,
        name: frame.data.name,
        output: frame.data.output,
        fileEdits: frame.data.file_edits,
        completedSeq: seq || undefined,
        completedAt: frame.ts,
      });
      if (known !== undefined) {
        replaceItem(s, known.seq, (item) => (item.kind === "tool" ? { ...item, ...done } : item));
      } else if (seq > 0) {
        // Its start is older than what this client holds.
        insertItem(
          s,
          defined<ToolItem>({
            kind: "tool",
            seq,
            ts: frame.ts,
            turnId: frame.turn_id,
            callId: frame.data.call_id,
            ...done,
            status: frame.data.status,
          }),
        );
      }
      return;
    }
    case "approval.requested":
    case "input.requested": {
      const id = frame.data.request_id;
      const known = requestItem(s, id, seq > 0 ? seq : Infinity);
      // The same event can arrive again right after a snapshot: the same seq is the same request.
      // A different seq under a known id is a new request that reuses it (an agent that restarts
      // starts its counters again), so it is shown, and opens, like any other.
      const repeat = known !== undefined && known.seq === seq;
      const early = repeat ? undefined : s.earlyAnswers[id];
      if (!repeat && seq > 0) {
        insertItem(
          s,
          defined<RequestItem>({
            kind: "request",
            seq,
            ts: frame.ts,
            turnId: frame.turn_id,
            requestId: id,
            event: frame,
            resolution: early,
          }),
        );
      }
      if (early !== undefined) {
        const { [id]: _used, ...rest } = s.earlyAnswers;
        s.earlyAnswers = rest;
      }
      if (live && !repeat && early === undefined) {
        s.pending = [...s.pending.filter((p) => p.data.request_id !== id), frame];
      } else if (live && repeat && known !== undefined && known.resolution === undefined && !s.pending.some((p) => p.data.request_id === id)) {
        s.pending = [...s.pending, frame];
      }
      return;
    }
    case "approval.resolved": {
      const id = frame.data.request_id;
      if (live) {
        // Only the request this answers stops being open: the latest with its id at or before the answer, as
        // `resolve` picks it. An answer that arrives again later must not close a newer request that reuses the id.
        const at = seq > 0 ? seq : Infinity;
        let answered: (typeof s.pending)[number] | undefined;
        for (const p of s.pending) {
          if (p.data.request_id === id && seqOf(p) <= at && (answered === undefined || seqOf(p) > seqOf(answered))) answered = p;
        }
        if (answered !== undefined) s.pending = s.pending.filter((p) => p !== answered);
      }
      const resolution = defined<Resolution>({
        seq,
        source: frame.data.source,
        decision: frame.data.decision,
        optionId: frame.data.option_id,
        text: frame.data.text,
        by: frame.data.by,
      });
      if (requestItem(s, id, seq > 0 ? seq : Infinity) === undefined) {
        // Its request has not arrived (frames out of order, or it is older than this client's history).
        if (live && s.earlyAnswers[id] === undefined) {
          const kept = Object.keys(s.earlyAnswers);
          const trimmed = { ...s.earlyAnswers };
          if (kept.length >= EARLY_ANSWERS_KEPT) delete trimmed[kept[0] as string];
          s.earlyAnswers = { ...trimmed, [id]: resolution };
        }
        return;
      }
      resolve(s, id, resolution, seq > 0 ? seq : Infinity);
      return;
    }
    case "context.compacted":
      if (seq > 0) {
        insertItem(
          s,
          defined<CompactedItem>({
            kind: "compacted",
            seq,
            ts: frame.ts,
            turnId: frame.turn_id,
            summary: frame.data.summary,
            tokensBefore: frame.data.tokens_before,
          }),
        );
      }
      return;
    case "error":
      if (seq > 0) {
        insertItem(
          s,
          defined<ErrorItem>({
            kind: "error",
            seq,
            ts: frame.ts,
            turnId: frame.turn_id,
            code: frame.data.code,
            message: frame.data.message,
            fatal: frame.data.fatal,
          }),
        );
      }
      return;
    case "checkpoint.created":
    case "checkpoint.restored": {
      if (seq > 0) {
        insertItem(
          s,
          defined<CheckpointItem>({
            kind: "checkpoint",
            seq,
            ts: frame.ts,
            turnId: frame.turn_id,
            what: frame.type === "checkpoint.created" ? "created" : "restored",
            checkpointId: frame.data.checkpoint_id,
            files: frame.data.files,
            note: frame.type === "checkpoint.created" ? frame.data.note : undefined,
            by: frame.type === "checkpoint.restored" ? frame.data.by : undefined,
          }),
        );
      }
      return;
    }
    default:
      return;
  }
}

function requestItem(s: SessionState, id: string, atOrBefore = Infinity): RequestItem | undefined {
  return instance<RequestItem>(s, "request", indexGet(s.index.request, id), (item) => item.requestId === id, atOrBefore);
}

function resolutionOf(s: SessionState, id: string): Resolution | undefined {
  return requestItem(s, id)?.resolution;
}

/** Whether a request has been answered (and not merely dropped by a state frame). */
function isResolved(s: SessionState, id: string): boolean {
  const found = resolutionOf(s, id);
  return found !== undefined && found.source !== "dropped";
}

/** Record how a request ended. The first answer stands; only a `dropped` mark is replaced. */
function resolve(s: SessionState, id: string, resolution: Resolution, atOrBefore = Infinity): void {
  const seq = requestItem(s, id, atOrBefore)?.seq;
  if (seq === undefined) return;
  replaceItem(s, seq, (item) => {
    if (item.kind !== "request") return item;
    if (item.resolution !== undefined && item.resolution.source !== "dropped") return item;
    return { ...item, resolution };
  });
}

// ---------------------------------------------------------------------
// Snapshot

/** A task's latest update. An older one (or the same one again) arriving later changes nothing. */
function applyTask(s: SessionState, frame: FrameOf<"task.updated">, seq: number): void {
  const known = s.tasks.find((t) => t.task_id === frame.data.task_id);
  if (known !== undefined && seq > 0 && known.seq >= seq) return;
  const task: TaskInfo = { ...frame.data, seq };
  s.tasks = upsertBy(s.tasks, (t) => t.task_id === task.task_id, task);
}

/** A checkpoint made, or restored. A repeat keeps what the checkpoint already says. */
function applyCheckpoint(s: SessionState, frame: FrameOf<"checkpoint.created"> | FrameOf<"checkpoint.restored">, seq: number): void {
  if (frame.type === "checkpoint.created") {
    const known = s.checkpoints.find((c) => c.checkpoint_id === frame.data.checkpoint_id);
    const info: CheckpointInfo = defined<CheckpointInfo>({
      checkpoint_id: frame.data.checkpoint_id,
      files: frame.data.files,
      note: frame.data.note,
      seq,
      ts: frame.ts,
      turn_id: frame.turn_id,
      restoredSeq: known?.restoredSeq,
    });
    s.checkpoints = upsertBy(s.checkpoints, (c) => c.checkpoint_id === info.checkpoint_id, info);
  } else {
    s.checkpoints = s.checkpoints.map((c) =>
      c.checkpoint_id === frame.data.checkpoint_id && (c.restoredSeq ?? -1) < seq ? { ...c, restoredSeq: seq } : c,
    );
  }
}

/**
 * The snapshot's seq-ordered `messages`, as history. Tasks and checkpoints are timeline events too
 * (G9: "Turn boundaries, requests and their answers, tasks, plans and checkpoints are in"), so they
 * are read from here, as they are from a stream, and a state built from a snapshot matches one built
 * from the frames.
 */
function applyMessages(s: SessionState, messages: readonly Frame[]): void {
  for (const event of messages) {
    applyTimeline(s, event, false);
    if (event.type === "task.updated") applyTask(s, event, seqOf(event));
    else if (event.type === "checkpoint.created" || event.type === "checkpoint.restored") {
      applyCheckpoint(s, event, seqOf(event));
    }
  }
}

/** A `session.snapshot` replaces everything the client holds about the session. */
function applySnapshot(prev: SessionState, snapshot: Snapshot): SessionState {
  const s = emptySession();
  // What is about the client, not the session, carries over.
  s.caughtUp = prev.caughtUp;
  s.commands = prev.commands;
  s.commandOrder = prev.commandOrder;
  s.lastNotice = prev.lastNotice;
  s.applied = prev.applied;

  s.session = snapshot.session;
  s.epoch = snapshot.epoch;
  s.cursor = snapshot.head;
  s.bootId = snapshot.boot_id;
  s.loaded = true;
  s.row = snapshot.row;
  s.phase = snapshot.state;
  s.phaseReason = snapshot.reason ?? null;
  s.caps = snapshot.caps;
  s.openTurn = snapshot.open_turn;
  s.live = snapshot.live.map(toLive);
  s.queue = snapshot.queue;
  s.plan = snapshot.plan;
  s.changes = snapshot.changes;
  s.usage = snapshot.usage;
  s.hasMore = snapshot.has_more;
  s.nextBefore = snapshot.next_before;
  for (const key of SINGLETONS) s.stateSeq[key] = snapshot.head;

  applyMessages(s, snapshot.messages);
  // What the snapshot's own fields add: the tasks still running and the checkpoints whose events are
  // older than the messages it carries. A task or checkpoint the messages showed is already there.
  for (const task of snapshot.tasks) {
    if (!s.tasks.some((t) => t.task_id === task.task_id)) s.tasks = [...s.tasks, { ...task, seq: snapshot.head }];
  }
  for (const checkpoint of snapshot.checkpoints) {
    if (!s.checkpoints.some((c) => c.checkpoint_id === checkpoint.checkpoint_id)) {
      s.checkpoints = [...s.checkpoints, { ...checkpoint }];
    }
  }
  // In the order they were made, as a stream gives them: the snapshot's own list can hold checkpoints older than its messages.
  s.checkpoints = [...s.checkpoints].sort((a, b) => a.seq - b.seq);
  // A restore the messages show, for a checkpoint made before them.
  for (const event of snapshot.messages) {
    if (event.type === "checkpoint.restored") applyCheckpoint(s, event, seqOf(event));
  }
  // The requests still open are the snapshot's own list: some are older than
  // the messages it carries, and one can be a step ahead of `head`.
  for (const event of snapshot.pending) {
    if (requestItem(s, event.data.request_id, event.seq)?.seq !== event.seq) {
      insertItem(
        s,
        defined<RequestItem>({
          kind: "request",
          seq: event.seq,
          ts: event.ts,
          turnId: event.turn_id,
          requestId: event.data.request_id,
          event,
        }),
      );
    }
    s.pending = [...s.pending, event];
  }
  // A request the messages show answered is not open, whatever the list says.
  s.pending = s.pending.filter((event) => !isResolved(s, event.data.request_id));
  return s;
}

/**
 * After a hole in the deltas: the live text from a snapshot fetched by GET replaces the text of the
 * messages a hole stopped, and appends carry on from it. Nothing else changes: the stream is still
 * in step with everything but those texts, and it may have moved on while the snapshot was on its
 * way, so a message that is not stopped keeps the text the stream gave it, and one the stream ended
 * meanwhile (`ended`) is not brought back.
 */
export function applyLive(state: SessionState, live: readonly LiveMessage[], ended: ReadonlySet<string> = new Set()): SessionState {
  const stopped = new Set(state.stopped);
  const next = state.live.filter((entry) => !stopped.has(entry.id));
  const held = new Set(next.map((entry) => entry.id));
  const order = state.live.map((entry) => entry.id);
  const taken = new Map<string, LiveText>();
  for (const message of live) {
    if (ended.has(message.id)) continue;
    if (stopped.has(message.id) || !held.has(message.id)) taken.set(message.id, toLive(message));
  }
  // Keep each message where it was, and add the ones that were not held at the end.
  const merged: LiveText[] = [];
  for (const id of order) {
    const entry = taken.get(id) ?? next.find((e) => e.id === id);
    if (entry !== undefined) merged.push(entry);
    taken.delete(id);
  }
  merged.push(...taken.values());
  return { ...state, live: merged, stopped: [] };
}

/** Older history (a `messages` page): added to the timeline, nothing else changes. */
export function applyHistory(state: SessionState, page: MessagesPage): SessionState {
  const s = { ...state };
  applyMessages(s, page.messages);
  s.hasMore = page.has_more;
  s.nextBefore = page.next_before;
  return s;
}

// ---------------------------------------------------------------------
// Frames

function stale(s: SessionState, frame: Frame, key: string): boolean {
  const seq = seqOf(frame);
  return seq > 0 && seq <= (s.stateSeq[key] ?? 0);
}

function mark(s: SessionState, frame: Frame, key: string): void {
  const seq = seqOf(frame);
  if (seq > (s.stateSeq[key] ?? 0)) s.stateSeq = { ...s.stateSeq, [key]: seq };
}

function recordCommand(s: SessionState, id: string, update: (record: CommandRecord) => CommandRecord): void {
  const known = s.commands[id];
  const next = update(known ?? { commandId: id });
  s.commands = { ...s.commands, [id]: next };
  if (known === undefined) {
    s.commandOrder = [...s.commandOrder, id];
    if (s.commandOrder.length > COMMANDS_KEPT) {
      const gone = s.commandOrder[0] as string;
      const { [gone]: _dropped, ...rest } = s.commands;
      s.commands = rest;
      s.commandOrder = s.commandOrder.slice(1);
    }
  }
}

function upsertBy<T>(list: readonly T[], match: (item: T) => boolean, item: T): T[] {
  const at = list.findIndex(match);
  return at < 0 ? [...list, item] : [...list.slice(0, at), item, ...list.slice(at + 1)];
}

function applyDelta(s: SessionState, frame: FrameOf<"message.delta">, effects: Effect[]): void {
  const delta = frame.data;
  if (s.stopped.includes(delta.id)) return;
  const at = s.live.findIndex((entry) => entry.id === delta.id);
  const held = at < 0 ? 0 : (s.live[at] as LiveText).length;
  const length = codePointLength(delta.text);
  if (delta.offset === held) {
    const entry: LiveText =
      at < 0
        ? defined<LiveText>({
            id: delta.id,
            kind: delta.kind,
            text: delta.text,
            length,
            turnId: frame.turn_id,
            done: delta.done,
          })
        : defined<LiveText>({
            ...(s.live[at] as LiveText),
            text: (s.live[at] as LiveText).text + delta.text,
            length: held + length,
            done: delta.done ?? (s.live[at] as LiveText).done,
          });
    s.live = at < 0 ? [...s.live, entry] : [...s.live.slice(0, at), entry, ...s.live.slice(at + 1)];
  } else if (delta.offset < held && delta.offset + length <= held) {
    // Text the client already has (it took a snapshot by GET while this delta was on its way).
    return;
  } else {
    // A hole. Drop the delta, stop appending to this message, and ask for the
    // snapshot: its live[] replaces the text and appends carry on from there.
    s.stopped = [...s.stopped, delta.id];
    effects.push({ type: "fetch_snapshot", reason: "hole", messageId: delta.id });
  }
}

function applyStateFrame(s: SessionState, frame: FrameOf<"session.state">, effects: Effect[]): void {
  const data = frame.data;
  const sync = data.live !== undefined;
  if (!sync && stale(s, frame, "session.state")) return;
  s.phase = data.state;
  s.phaseReason = data.reason ?? null;
  if (data.harness_session_id !== undefined) s.harnessSessionId = data.harness_session_id;
  if (!sync) {
    mark(s, frame, "session.state");
    if (ENDS_LIVE.has(data.state)) {
      s.live = [];
      s.stopped = [];
    }
    return;
  }
  // The frame that ends every catch-up.
  s.live = (data.live ?? []).map(toLive);
  s.stopped = [];
  if (data.pending !== undefined) {
    const open = new Set(data.pending);
    // Ids it does not list are closed. Ids this client does not know are left alone: their event is on its way.
    const dropped = s.pending.filter((p) => !open.has(p.data.request_id));
    if (dropped.length > 0) {
      s.pending = s.pending.filter((p) => open.has(p.data.request_id));
      for (const gone of dropped) resolve(s, gone.data.request_id, { seq: 0, source: "dropped" });
    }
  }
  if (data.head !== undefined && data.head > s.cursor) s.cursor = data.head;
  if (data.epoch !== undefined) {
    if (s.epoch === null) s.epoch = data.epoch;
    else if (s.epoch !== data.epoch) effects.push({ type: "fetch_snapshot", reason: "epoch" });
  }
  s.caughtUp = true;
  s.loaded = true;
}

/** Apply one frame. The input state is never changed. */
export function reduce(state: SessionState, frame: Frame): Reduced {
  const effects: Effect[] = [];
  if (frame.type === "session.snapshot") {
    const next = applySnapshot(state, frame.data);
    next.applied = state.applied + 1;
    return { state: next, effects };
  }
  const s: SessionState = { ...state, applied: state.applied + 1 };
  if (s.session === null && frame.session) s.session = frame.session;
  const seq = seqOf(frame);

  switch (frame.type) {
    case "ping":
      if (frame.data.head > s.cursor) s.cursor = frame.data.head;
      return { state: s, effects };
    case "session.row":
      s.row = frame.data.row;
      return { state: s, effects };
    case "message.delta":
      applyDelta(s, frame, effects);
      return { state: s, effects };
    case "session.state":
      applyStateFrame(s, frame, effects);
      break;
    case "session.caps":
      if (!stale(s, frame, "session.caps")) {
        s.caps = frame.data.caps;
        mark(s, frame, "session.caps");
      }
      break;
    case "plan.updated":
      if (!stale(s, frame, "plan.updated")) {
        s.plan = frame.data;
        mark(s, frame, "plan.updated");
      }
      break;
    case "changes.updated":
      if (!stale(s, frame, "changes.updated")) {
        s.changes = frame.data.files;
        mark(s, frame, "changes.updated");
      }
      break;
    case "queue.updated":
      if (!stale(s, frame, "queue.updated")) {
        s.queue = frame.data.items;
        mark(s, frame, "queue.updated");
      }
      break;
    case "usage.updated":
      if (!stale(s, frame, "usage.updated")) {
        s.usage = frame.data;
        mark(s, frame, "usage.updated");
      }
      break;
    case "task.updated":
      applyTask(s, frame, seq);
      break;
    case "checkpoint.created":
    case "checkpoint.restored":
      applyCheckpoint(s, frame, seq);
      applyTimeline(s, frame, true);
      break;
    case "command.receipt":
      if (frame.command_id !== undefined && frame.command_id !== "") {
        recordCommand(s, frame.command_id, (record) => ({
          ...record,
          receipt: defined<NonNullable<CommandRecord["receipt"]>>({
            status: frame.data.status,
            reason: frame.data.reason,
            op: frame.data.op,
            ts: frame.ts,
          }),
        }));
      }
      break;
    case "command.result":
      if (frame.command_id !== undefined && frame.command_id !== "") {
        recordCommand(s, frame.command_id, (record) => ({
          ...record,
          result: defined<NonNullable<CommandRecord["result"]>>({
            op: frame.data.op,
            ok: frame.data.ok,
            note: frame.data.note,
            degraded: frame.data.degraded,
            cancelled: frame.data.cancelled,
            requestId: frame.data.request_id,
            launchId: frame.data.launch_id,
            ts: frame.ts,
          }),
        }));
      }
      break;
    case "notify":
      s.lastNotice = defined<NonNullable<SessionState["lastNotice"]>>({
        n: (s.lastNotice?.n ?? 0) + 1,
        kind: frame.data.kind,
        message: frame.data.message,
        status: frame.data.status,
        ts: frame.ts,
      });
      break;
    default:
      applyTimeline(s, frame, true);
      break;
  }

  // Any journaled frame, applied or not, is one the client has now seen.
  if (seq > s.cursor) s.cursor = seq;
  return { state: s, effects };
}

/** Apply frames in order, collecting what the store has to do. */
export function reduceAll(state: SessionState, frames: readonly Frame[]): Reduced {
  let current = state;
  const effects: Effect[] = [];
  for (const frame of frames) {
    const out = reduce(current, frame);
    current = out.state;
    effects.push(...out.effects);
  }
  return { state: current, effects };
}

/** The state of a connection that has just opened: nothing has caught up yet. */
export function beginConnection(state: SessionState): SessionState {
  return { ...state, caughtUp: false };
}
