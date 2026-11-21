// What each send that is not finished shows: one state and one sentence, so a
// message bubble always has something to say. The composer and the timeline
// read this, not `session.commands` (which only holds what the stream said).
import type { OutboxView } from "./outbox";
import type { OutboxEntry } from "./outbox-storage";
import type { ConnectionStatus } from "./supervisor";

export type SendShown =
  /** Kept on this device; not posted yet, or due to be tried again. `reason` says what it waits for. */
  | "waiting"
  /** Its post is on its way. */
  | "sending"
  /** The hub took it; it waits for the session to take it. */
  | "sent"
  /** Terminal: not delivered, or may or may not have been. `reason` says which. */
  | "failed"
  /** Terminal: the hub took it but never said whether it reached the session. */
  | "unconfirmed";

export interface SendView {
  commandId: string;
  text: string;
  attachments: string[];
  mode: "queue" | "steer";
  createdAt: number;
  shown: SendShown;
  /** One sentence for the person. */
  reason: string;
  /** The person can send it again as a new message. */
  canRetry: boolean;
  /** The person can dismiss it (a failed one), or take it back before it is posted. */
  canDiscard: boolean;
}

export interface SendContext {
  outbox: OutboxView;
  connection: ConnectionStatus;
  /** The session's row has been seen. */
  loaded: boolean;
  online: boolean;
}

function waitingReason(entry: OutboxEntry, context: SendContext): string {
  if (context.outbox.paused === "unauthorized" || context.connection.state === "unauthorized") {
    return "Waiting: this device is not signed in to the hub. It will be sent once it is.";
  }
  if (!context.online || context.connection.state === "offline") {
    return "Waiting: you are offline. It will be sent when you are back.";
  }
  if (!context.loaded) return "Waiting for the hub. It will be sent once the session is loaded.";
  if (entry.claim !== undefined && entry.claim.owner !== context.outbox.owner) {
    return "Being sent from another tab, or from a page that closed while sending. Checked again within a minute.";
  }
  if (entry.lastError !== undefined) return `${entry.lastError}. Trying again.`;
  if (context.connection.state === "hub_down") return "Waiting: the hub is not answering. It will be sent once it does.";
  return "Waiting to be sent.";
}

function sentReason(entry: OutboxEntry): string {
  if (entry.receipt === "queued") return "Queued: it will be sent when the current turn ends.";
  if (entry.receipt === "held") return "Held by the hub until that machine is back.";
  return "Sent. Waiting for the session to take it.";
}

/** Each unfinished send, oldest first, with what its bubble shows. */
export function describeSends(context: SendContext): SendView[] {
  return context.outbox.entries.map((entry) => {
    const base = {
      commandId: entry.commandId,
      text: entry.text,
      attachments: entry.attachments,
      mode: entry.mode,
      createdAt: entry.createdAt,
    };
    switch (entry.status) {
      case "waiting":
        if (context.outbox.inFlight === entry.commandId) {
          return { ...base, shown: "sending", reason: "Sending.", canRetry: false, canDiscard: false };
        }
        // Not posted yet: taking it back is safe. Posted once already: the hub may hold it, so it stays.
        return { ...base, shown: "waiting", reason: waitingReason(entry, context), canRetry: false, canDiscard: entry.attempts === 0 };
      case "sent":
        return { ...base, shown: "sent", reason: sentReason(entry), canRetry: false, canDiscard: false };
      case "failed":
        return { ...base, shown: "failed", reason: entry.error ?? "Not sent.", canRetry: true, canDiscard: true };
      case "unconfirmed":
        return { ...base, shown: "unconfirmed", reason: entry.error ?? "Not confirmed.", canRetry: true, canDiscard: true };
    }
  });
}
