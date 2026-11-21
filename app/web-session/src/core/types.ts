// The wire types are generated (generated/session-v2.ts, from the hub's
// schema). This file names the few shapes the client core derives from them.
import type { Frame } from "./generated/session-v2";

export type {
  ApprovalRequestedData,
  ApprovalResolvedData,
  Caps,
  ChangedFile,
  Checkpoint,
  CommandReceiptData,
  CommandResultData,
  Frame,
  LiveMessage,
  MessagesPage,
  NotifyData,
  Option,
  PlanUpdatedData,
  QueuedMessage,
  Row,
  FlagOption,
  OptionChoice,
  SessionOptions,
  StringOption,
  Snapshot,
  Status,
  TaskUpdatedData,
  TurnEndedData,
  UsageUpdatedData,
  FileEdit,
} from "./generated/session-v2";
export { PROTOCOL } from "./generated/session-v2";

/** The frame of one type. */
export type FrameOf<T extends Frame["type"]> = Extract<Frame, { type: T }>;

/** The session's state, as the hub names it. */
export type SessionPhase = FrameOf<"session.state">["data"]["state"];

/** A frame that has a `seq` field at all (deltas, pings, rows and snapshots do not). */
export function seqOf(frame: Frame): number {
  return "seq" in frame && typeof frame.seq === "number" ? frame.seq : 0;
}

/**
 * Whether a frame moves the cursor: journaled frames have a `seq` above 0.
 * A `seq` of 0 marks a frame the hub made for the stream; it is never a cursor.
 */
export function isJournaled(frame: Frame): boolean {
  return seqOf(frame) > 0;
}

/** A frame that arrived off the wire is v2 JSON with a `type`; anything else is not ours. */
export function isFrame(value: unknown): value is Frame {
  if (typeof value !== "object" || value === null) return false;
  const frame = value as { v?: unknown; type?: unknown };
  return frame.v === 2 && typeof frame.type === "string";
}

/** The length the hub counts a text in: Unicode code points, as Python's `len` does. */
export function codePointLength(text: string): number {
  let length = 0;
  for (let i = 0; i < text.length; i++) {
    const unit = text.charCodeAt(i);
    // A high surrogate followed by a low one is one code point.
    if (unit >= 0xd800 && unit <= 0xdbff && i + 1 < text.length) {
      const next = text.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) i++;
    }
    length++;
  }
  return length;
}
