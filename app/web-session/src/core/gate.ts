// When an action is allowed.
//
// Sends wait in the outbox, so a send is allowed whenever the session can be
// written to. Approvals, Stop, Restore and the queue edits need a live
// connection that has caught up: a "yes" sent from a stale screen can land on
// a request that has already moved on, so they are refused instead of queued,
// with a reason the view can show next to the control.
import type { ConnectionStatus } from "./supervisor";
import type { SessionState } from "./state";

export type GatedAction = "respond" | "interrupt" | "restore" | "queue";

export type RefusalReason =
  | "not_live"
  | "catching_up"
  | "read_only"
  | "unauthorized"
  | "gone"
  | "outdated";

export interface Refusal {
  ok: false;
  reason: RefusalReason;
  /** One sentence for the person, ready to show. */
  message: string;
}

/** `row.control` is "full" for a session SWE can drive; anything else is read-only, with `control_reason`. */
export function readOnlyReason(row: SessionState["row"]): string | null {
  if (row === null) return null;
  const control = row["control"];
  if (typeof control !== "string" || control === "full") return null;
  const reason = row["control_reason"];
  return typeof reason === "string" && reason !== "" ? reason : "This session cannot be controlled from here";
}

const NOT_LIVE: Record<ConnectionStatus["state"], { reason: RefusalReason; message: string }> = {
  connecting: { reason: "not_live", message: "Connecting to the hub. Try again in a moment." },
  live: { reason: "not_live", message: "" },
  reconnecting: { reason: "not_live", message: "Reconnecting to the hub. Try again once it is back." },
  offline: { reason: "not_live", message: "You are offline. Reconnect to do this." },
  hub_down: { reason: "not_live", message: "The hub is not answering. Try again once it is back." },
  unauthorized: { reason: "unauthorized", message: "This device is not signed in to the hub." },
  gone: { reason: "gone", message: "This session no longer exists." },
  outdated: { reason: "outdated", message: "The hub is too old for this view. Update SWE on that machine." },
};

/** Why a gated action cannot run now, or null when it can. */
export function gate(connection: ConnectionStatus, session: SessionState): Refusal | null {
  const readOnly = readOnlyReason(session.row);
  if (readOnly !== null) return { ok: false, reason: "read_only", message: readOnly };
  if (connection.state !== "live") {
    const why = NOT_LIVE[connection.state];
    return { ok: false, reason: why.reason, message: why.message };
  }
  if (!session.caughtUp) {
    return { ok: false, reason: "catching_up", message: "Catching up with the session. Try again in a moment." };
  }
  return null;
}

/** Why a send cannot be accepted at all (it never waits for a connection, but it needs a session that takes input). */
export function gateSend(connection: ConnectionStatus, session: SessionState): Refusal | null {
  const readOnly = readOnlyReason(session.row);
  if (readOnly !== null) return { ok: false, reason: "read_only", message: readOnly };
  if (connection.state === "gone") return { ok: false, reason: "gone", message: NOT_LIVE.gone.message };
  if (connection.state === "outdated") return { ok: false, reason: "outdated", message: NOT_LIVE.outdated.message };
  return null;
}
