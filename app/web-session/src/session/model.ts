// What the timeline draws, worked out from the store's state with no React in it, so the rules can be
// tested on their own: which items sit together in a work group, where a turn's footer goes, which
// outbox sends are still worth showing. The core keeps the journal as one flat list (`session.items`);
// grouping it is this file's job (G11, "No coalescing in the core").
import type {
  CheckpointItem,
  CompactedItem,
  ErrorItem,
  LiveText,
  MessageItem,
  RequestItem,
  SendView,
  SessionState,
  ThinkingItem,
  TimelineItem,
  ToolItem,
  TurnInfo,
} from "../core";
import { diffStat } from "./format";

/** What a work group holds: the steps between two pieces of prose. */
export type WorkEntry = ThinkingItem | ToolItem | CheckpointItem;

export interface WorkGroup {
  kind: "work";
  key: string;
  /** `work-<seq of the first entry>`: unique among groups, so it is the React key. */
  id: string;
  /** The `seq` of every entry: a person's open or closed choice is kept for any of them, so it survives the group growing at either end. */
  seqs: number[];
  turnId: string | undefined;
  entries: WorkEntry[];
  /** The turn this group belongs to has ended: a step still "running" in it never will finish. */
  ended: boolean;
  /** The agent is still at it: the turn is open and nothing was written after this group. */
  running: boolean;
  /** Seconds from the first entry to the last one's end, or null while it runs. */
  span: number | null;
  tools: number;
  thoughts: number;
  failed: number;
}

export interface TurnFooter {
  kind: "footer";
  key: string;
  turn: TurnInfo;
  /** The files this turn's tools changed, counted once each. */
  files: number;
  added: number;
  removed: number;
}

export type Row =
  | { kind: "user"; key: string; item: MessageItem }
  | { kind: "assistant"; key: string; item: MessageItem }
  | { kind: "system"; key: string; item: MessageItem }
  | WorkGroup
  | { kind: "request"; key: string; item: RequestItem }
  | { kind: "notice"; key: string; item: CompactedItem | ErrorItem }
  | { kind: "live"; key: string; live: LiveText; stopped: boolean }
  | { kind: "working"; key: string }
  | TurnFooter
  | { kind: "send"; key: string; send: SendView };

/** The `commandId`s whose message is already in the timeline. */
function deliveredIds(items: readonly TimelineItem[]): Set<string> {
  const ids = new Set<string>();
  for (const item of items) {
    if (item.kind === "message" && item.role === "user") {
      if (item.commandId !== undefined && item.commandId !== "") ids.add(item.commandId);
    }
  }
  return ids;
}

/**
 * The sends still worth a bubble. The timeline draws a send's message itself once the journal has it,
 * and this hides the bubble then, matching by `commandId`: the `command_id` the keeper path echoes on the
 * user's `message.completed`. The outbox entry stays until its `command.result`, so without this a
 * delivered message would show twice.
 *
 * Only the keeper path echoes it. A session no keeper holds (the legacy path) gets its user message from
 * the harness's transcript, which knows nothing of the id the send was posted under: `protocol_compat.to_v2`
 * has no `command_id` to pass on. There the bubble and the message show together until the send's
 * `command.result` removes the entry (or, if that result is lost, until the entry ends unconfirmed), and
 * nothing is guessed from the text. See "Legacy sends" in the G12 spec.
 *
 * A send the hub only queued is shown here until the queue lists it; then the composer's queued tray (G13) draws it
 * and `Timeline` leaves the bubble out.
 */
export function visibleSends(sends: readonly SendView[], items: readonly TimelineItem[]): SendView[] {
  if (sends.length === 0) return [];
  const delivered = deliveredIds(items);
  return sends.filter((send) => !delivered.has(send.commandId));
}

function endOf(entry: WorkEntry): number {
  return entry.kind === "tool" ? (entry.completedAt ?? entry.ts) : entry.ts;
}

function group(entries: WorkEntry[], turnId: string | undefined): WorkGroup {
  const id = `work-${(entries[0] as WorkEntry).seq}`;
  const tools = entries.filter((e) => e.kind === "tool");
  const start = Math.min(...entries.map((e) => e.ts));
  const end = Math.max(...entries.map(endOf));
  return {
    kind: "work",
    key: id,
    id,
    seqs: entries.map((entry) => entry.seq),
    turnId,
    entries,
    ended: false,
    running: false,
    span: Math.max(0, end - start),
    tools: tools.length,
    thoughts: entries.filter((e) => e.kind === "thinking").length,
    failed: tools.filter((e) => e.kind === "tool" && e.status === "error").length,
  };
}

interface Segment {
  turnId: string | undefined;
  items: TimelineItem[];
}

/** Split the journal into runs of one turn. An item with no turn (an error, say) stays with the run before it. */
function segments(items: readonly TimelineItem[]): Segment[] {
  const out: Segment[] = [];
  for (const item of items) {
    const last = out[out.length - 1];
    if (last === undefined || (item.turnId !== undefined && item.turnId !== last.turnId && last.turnId !== undefined)) {
      out.push({ turnId: item.turnId, items: [item] });
    } else {
      if (last.turnId === undefined && item.turnId !== undefined) last.turnId = item.turnId;
      last.items.push(item);
    }
  }
  return out;
}

function footerFor(turn: TurnInfo, items: readonly TimelineItem[]): TurnFooter | null {
  if (turn.status === undefined && turn.endedAt === undefined) return null;
  const paths = new Set<string>();
  let added = 0;
  let removed = 0;
  for (const item of items) {
    if (item.kind !== "tool" || item.fileEdits === undefined) continue;
    for (const edit of item.fileEdits) {
      paths.add(edit.path);
      const stat = diffStat(edit.diff);
      added += stat.added;
      removed += stat.removed;
    }
  }
  const failed = turn.status !== undefined && turn.status !== "completed";
  // A turn that changed nothing and ended well has nothing to say under it.
  if (!failed && paths.size === 0) return null;
  return {
    kind: "footer",
    key: `footer-${turn.id}`,
    turn,
    files: paths.size,
    added,
    removed,
  };
}

/** The rows of one run of one turn, with its work groups made. */
function segmentRows(segment: Segment): Row[] {
  const rows: Row[] = [];
  let run: WorkEntry[] = [];
  const close = () => {
    if (run.length > 0) rows.push(group(run, segment.turnId));
    run = [];
  };
  for (const item of segment.items) {
    switch (item.kind) {
      case "thinking":
      case "tool":
      case "checkpoint":
        run.push(item);
        break;
      case "message":
        close();
        rows.push({ kind: item.role, key: `item-${item.seq}`, item } as Row);
        break;
      case "request":
        close();
        rows.push({ kind: "request", key: `item-${item.seq}`, item });
        break;
      case "compacted":
      case "error":
        close();
        rows.push({ kind: "notice", key: `item-${item.seq}`, item });
        break;
    }
  }
  close();
  return rows;
}

/** The parts of the session the timeline reads: a view selects these one by one, so a ping redraws nothing. */
export type TimelineInput = Pick<SessionState, "items" | "live" | "turns" | "stopped" | "openTurn" | "phase">;

/** Every row of the timeline, top to bottom: the journal's items, live text, turn footers, then unfinished sends. */
export function buildRows(session: TimelineInput, sends: readonly SendView[]): Row[] {
  const turns = new Map(session.turns.map((turn) => [turn.id, turn]));
  const runs = segments(session.items);
  // A live reply whose turn has no item yet still needs a place: a run of its own turn, or, for text
  // with no turn on it (the protocol allows that, and a legacy session's live text has none), the last run.
  for (const live of session.live) {
    if (live.turnId !== undefined && !runs.some((run) => run.turnId === live.turnId)) runs.push({ turnId: live.turnId, items: [] });
  }
  if (runs.length === 0 && session.live.some((live) => live.turnId === undefined)) runs.push({ turnId: undefined, items: [] });
  const lastRunOf = new Map<string | undefined, number>();
  runs.forEach((run, index) => lastRunOf.set(run.turnId, index));
  const stopped = new Set(session.stopped);
  const rows: Row[] = [];
  runs.forEach((run, index) => {
    const own = segmentRows(run);
    const isLastRunOfTurn = lastRunOf.get(run.turnId) === index;
    const isLastRun = index === runs.length - 1;
    // Live text goes after the last items of its turn; text with no turn goes with the last run.
    const live = session.live.filter((entry) => (entry.turnId === undefined ? isLastRun : isLastRunOfTurn && entry.turnId === run.turnId));
    for (const entry of live) own.push({ kind: "live", key: `live-${entry.id}`, live: entry, stopped: stopped.has(entry.id) });
    const open = run.turnId !== undefined && session.openTurn === run.turnId;
    const turnOf = run.turnId === undefined ? undefined : turns.get(run.turnId);
    const finished = turnOf !== undefined && (turnOf.status !== undefined || turnOf.endedAt !== undefined);
    if (finished) for (const row of own) if (row.kind === "work") row.ended = true;
    const tail = own[own.length - 1];
    if (open && isLastRunOfTurn && tail?.kind === "work") {
      tail.running = true;
      tail.span = null;
    }
    rows.push(...own);
    const turn = run.turnId === undefined ? undefined : turns.get(run.turnId);
    if (turn !== undefined && isLastRunOfTurn) {
      const turnItems = runs.filter((other) => other.turnId === turn.id).flatMap((other) => other.items);
      const footer = footerFor(turn, turnItems);
      if (footer !== null) rows.push(footer);
    }
  });
  // The agent is between steps: nothing is streaming and no group says it is working.
  const last = rows[rows.length - 1];
  const idle = last === undefined || (last.kind !== "live" && !(last.kind === "work" && last.running));
  if (session.openTurn !== null && session.phase === "running" && idle && !rows.some((row) => row.kind === "request" && row.item.resolution === undefined)) {
    rows.push({ kind: "working", key: "working" });
  }
  for (const send of visibleSends(sends, session.items)) rows.push({ kind: "send", key: `send-${send.commandId}`, send });
  return rows;
}

/** What a tool step is called in a list: its summary, else its name. */
export function toolTitle(tool: ToolItem): string {
  return tool.summary !== undefined && tool.summary !== "" ? tool.summary : (tool.name ?? "Tool call");
}

/** The project and title a session's row names, for the breadcrumb. */
export function rowLabels(row: SessionState["row"]): { project: string | null; title: string | null } {
  if (row === null) return { project: null, title: null };
  const text = (value: unknown): string | null => (typeof value === "string" && value.trim() !== "" ? value.trim() : null);
  const cwd = text(row["cwd"]);
  const project = cwd === null ? null : (cwd.replace(/[\\/]+$/, "").split(/[\\/]/).pop() ?? null);
  return { project: project === "" ? null : project, title: text(row["title"]) ?? text(row["harness_title"]) };
}


/** What makes a row the same row across a change of kind: a reply is one thing while it streams and after. */
export function rowIdentity(row: Row): string {
  if (row.kind === "live") return `reply-${row.live.id}`;
  if (row.kind === "assistant") return `reply-${row.item.id}`;
  return row.key;
}

/**
 * Whether two rows draw the same: the rows are made again on every change, but what they hold (the
 * journal's items, the live text) is the same object until it changes, so a streamed delta finds only
 * its own row different and the other thousands are not drawn again.
 */
export function rowsEqual(a: Row, b: Row): boolean {
  if (a.kind !== b.kind || a.key !== b.key) return false;
  switch (a.kind) {
    case "user":
    case "assistant":
    case "system":
    case "request":
    case "notice":
      return a.item === (b as typeof a).item;
    case "live":
      return a.live === (b as typeof a).live && a.stopped === (b as typeof a).stopped;
    case "work": {
      const other = b as WorkGroup;
      return (
        a.running === other.running &&
        a.ended === other.ended &&
        a.span === other.span &&
        a.entries.length === other.entries.length &&
        a.entries.every((entry, index) => entry === other.entries[index])
      );
    }
    case "footer": {
      const other = b as TurnFooter;
      return a.turn === other.turn && a.files === other.files && a.added === other.added && a.removed === other.removed;
    }
    case "working":
      return true;
    case "send": {
      const other = (b as typeof a).send;
      const own = a.send;
      return (
        own.commandId === other.commandId &&
        own.shown === other.shown &&
        own.reason === other.reason &&
        own.canRetry === other.canRetry &&
        own.canDiscard === other.canDiscard &&
        own.text === other.text
      );
    }
  }
}

/**
 * Names for finding a row again after the rows were made again with older history in front: an item's key,
 * and for a work group the `seq` of each of its entries (its own key changes when older steps join its front).
 */
export function rowAnchorIds(row: Row): string[] {
  return row.kind === "work" ? row.seqs.map((seq) => `seq-${seq}`) : [row.key];
}
