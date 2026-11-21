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
  /** Stable for the group's life: the `seq` of its first entry. The open and closed state is kept by it. */
  id: string;
  turnId: string | undefined;
  entries: WorkEntry[];
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
 * with the `commandId` the outbox posted it under (the hub echoes the command id, or the client id
 * on a session no keeper holds, as `command_id`); the outbox entry stays until its `command.result`,
 * so without this a delivered message would show twice. A send the hub only queued is still shown
 * here: the queue's own tray is the composer milestone's.
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
  const first = entries[0] as WorkEntry;
  const tools = entries.filter((e) => e.kind === "tool");
  const start = Math.min(...entries.map((e) => e.ts));
  const end = Math.max(...entries.map(endOf));
  return {
    kind: "work",
    key: `work-${first.seq}`,
    id: `work-${first.seq}`,
    turnId,
    entries,
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
  // A live reply whose turn has no item yet still needs a place.
  for (const live of session.live) {
    if (live.turnId !== undefined && !runs.some((run) => run.turnId === live.turnId)) runs.push({ turnId: live.turnId, items: [] });
  }
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
