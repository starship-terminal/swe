// The session state the reducer builds from v2 frames. Plain data, no
// classes: the store holds it and the timeline reads it.
import type {
  ApprovalResolvedData,
  Caps,
  ChangedFile,
  Checkpoint,
  FileEdit,
  FrameOf,
  PlanUpdatedData,
  QueuedMessage,
  Row,
  SessionPhase,
  TaskUpdatedData,
  TurnEndedData,
  UsageUpdatedData,
} from "./types";

/** The reply, thinking or tool text still being written, as far as this client holds it. */
export interface LiveText {
  id: string;
  kind: "assistant" | "thinking" | "tool_use";
  text: string;
  /** The text's length in code points: the unit the hub's `offset` counts in. */
  length: number;
  turnId?: string;
  done?: boolean;
}

/** A request the agent is waiting on: an approval, or a question for the user. */
export type RequestEvent = FrameOf<"approval.requested"> | FrameOf<"input.requested">;

export interface Resolution {
  seq: number;
  /** What closed it. `dropped` is the client's own: the hub's state frame stopped listing it. */
  source: ApprovalResolvedData["source"] | "dropped";
  decision?: ApprovalResolvedData["decision"];
  optionId?: string;
  text?: string;
  by?: string;
}

interface ItemBase {
  /** Where the item sits in the session's journal. Timeline items are keyed by it. */
  seq: number;
  ts: number;
  turnId?: string;
}

export interface MessageItem extends ItemBase {
  kind: "message";
  /** The message's own id; it pairs the message with the live text it ended. */
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  by?: string;
  attachments?: string[];
  /** The command that sent it, for a user message. */
  commandId?: string;
}

export interface ThinkingItem extends ItemBase {
  kind: "thinking";
  id: string;
  text: string;
  durationS?: number;
}

export interface ToolItem extends ItemBase {
  kind: "tool";
  callId: string;
  name?: string;
  summary?: string;
  input?: unknown;
  taskId?: string;
  status: "running" | "ok" | "error" | "cancelled";
  /** The latest `tool.updated` text. */
  text?: string;
  output?: string;
  fileEdits?: FileEdit[];
  completedSeq?: number;
  completedAt?: number;
}

export interface RequestItem extends ItemBase {
  kind: "request";
  requestId: string;
  event: RequestEvent;
  resolution?: Resolution;
}

export interface CompactedItem extends ItemBase {
  kind: "compacted";
  summary?: string;
  tokensBefore?: number;
}

export interface ErrorItem extends ItemBase {
  kind: "error";
  code: string;
  message: string;
  fatal?: boolean;
}

export interface CheckpointItem extends ItemBase {
  kind: "checkpoint";
  what: "created" | "restored";
  checkpointId: string;
  files?: number;
  note?: string;
  by?: string;
}

export type TimelineItem =
  | MessageItem
  | ThinkingItem
  | ToolItem
  | RequestItem
  | CompactedItem
  | ErrorItem
  | CheckpointItem;

export interface TurnInfo {
  id: string;
  startedSeq?: number;
  startedAt?: number;
  by?: string;
  text?: string;
  endedSeq?: number;
  endedAt?: number;
  status?: TurnEndedData["status"];
  reason?: string;
  durationS?: number;
}

export interface TaskInfo extends TaskUpdatedData {
  /** The `seq` of its latest update. */
  seq: number;
}

export interface CheckpointInfo extends Checkpoint {
  /** The `seq` of the `checkpoint.restored` that last restored it. */
  restoredSeq?: number;
}

export interface CommandRecord {
  commandId: string;
  receipt?: { status: string; reason?: string; op?: string; ts: number };
  result?: {
    op: string;
    ok: boolean;
    note?: string;
    degraded?: boolean;
    cancelled?: boolean;
    requestId?: string;
    launchId?: string;
    ts: number;
  };
}

export interface Notice {
  /** Counts up with each notice, so a view can tell two equal ones apart. */
  n: number;
  kind: string;
  message: string;
  status?: string;
  ts: number;
}

export interface SessionState {
  /** The session key, from the first snapshot or event. */
  session: string | null;
  epoch: number | null;
  /** The highest journaled `seq` seen, raised to the `head` of each state frame and ping. */
  cursor: number;
  /** The hub's boot id, from the last snapshot. A change means the hub restarted. */
  bootId: string | null;
  /** Something to draw: a snapshot, or a catch-up, has been applied. */
  loaded: boolean;
  /** The state frame that ends a catch-up has arrived on the current connection. */
  caughtUp: boolean;
  row: Row | null;
  phase: SessionPhase | null;
  phaseReason: string | null;
  harnessSessionId: string | null;
  caps: Caps | null;
  openTurn: string | null;
  turns: TurnInfo[];
  /** Timeline items in journal order, one per `seq`. */
  items: TimelineItem[];
  index: { tool: Record<string, number>; request: Record<string, number> };
  /** Messages in progress. */
  live: LiveText[];
  /** Messages a hole in the deltas stopped: appends are ignored until `live[]` replaces the text. */
  stopped: string[];
  /** The requests still open, in the order they were asked. */
  pending: RequestEvent[];
  /** Answers that arrived before their request did (frames out of order), kept until it shows up. */
  earlyAnswers: Record<string, Resolution>;
  plan: PlanUpdatedData | null;
  tasks: TaskInfo[];
  checkpoints: CheckpointInfo[];
  changes: ChangedFile[] | null;
  queue: QueuedMessage[];
  usage: UsageUpdatedData | null;
  commands: Record<string, CommandRecord>;
  commandOrder: string[];
  lastNotice: Notice | null;
  hasMore: boolean;
  nextBefore: number | null;
  /** The `seq` each kind of state event last applied at; an older one that arrives later is ignored. */
  stateSeq: Record<string, number>;
  /** Frames applied since the state was made: lets a test see work was done. */
  applied: number;
}

export function emptySession(): SessionState {
  return {
    session: null,
    epoch: null,
    cursor: 0,
    bootId: null,
    loaded: false,
    caughtUp: false,
    row: null,
    phase: null,
    phaseReason: null,
    harnessSessionId: null,
    caps: null,
    openTurn: null,
    turns: [],
    items: [],
    index: { tool: {}, request: {} },
    live: [],
    stopped: [],
    pending: [],
    earlyAnswers: {},
    plan: null,
    tasks: [],
    checkpoints: [],
    changes: null,
    queue: [],
    usage: null,
    commands: {},
    commandOrder: [],
    lastNotice: null,
    hasMore: false,
    nextBefore: null,
    stateSeq: {},
    applied: 0,
  };
}

/** How many answers that came before their request are kept. */
export const EARLY_ANSWERS_KEPT = 50;

/** How many command records the state keeps. */
export const COMMANDS_KEPT = 200;
