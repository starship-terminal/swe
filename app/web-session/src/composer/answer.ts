// Answering a request (an approval or a question) so that the press lands on the request the person saw.
//
// A request is named to the hub by its id alone, and an agent that restarts counts its ids again from
// `perm-1` (G11: ids are reused), so an answer meant for the request on screen can reach a newer one that
// took its place. Each press therefore carries the request's `seq` (the journal position of the event the
// person saw) and is refused, before anything is posted, unless the latest open request with that id still
// has that `seq`. Check and post happen in one synchronous step, so no frame can arrive between them.
import type { ActionResult, RequestEvent, SessionStore } from "../core";

export interface Shown {
  requestId: string;
  seq: number;
}

export type AnswerOutcome = { ok: true } | { ok: false; reason: "changed" | "busy" | "refused"; message: string };

const answering = new WeakMap<SessionStore, Set<string>>();

/** The words the hub reads as a decision itself; any other option is answered by its 1-based place in the list. */
const DECISIONS = new Set(["allow", "always", "deny"]);

/** What to send for an option: its id when that is a decision, else its number in the list. */
export function decisionFor(option: { id: string }, index: number): string {
  return DECISIONS.has(option.id) ? option.id : String(index + 1);
}

/** The request the hub would answer for this id, as this client holds it: the latest open one. */
export function openRequest(store: SessionStore, requestId: string): RequestEvent | undefined {
  const { pending } = store.getState().session;
  for (let i = pending.length - 1; i >= 0; i--) {
    const found = pending[i] as RequestEvent;
    if (found.data.request_id === requestId) return found;
  }
  return undefined;
}

export async function answer(store: SessionStore, shown: Shown, decision: string): Promise<AnswerOutcome> {
  const key = `${shown.requestId}\u0000${shown.seq}`;
  let mine = answering.get(store);
  if (mine === undefined) answering.set(store, (mine = new Set()));
  if (mine.has(key)) return { ok: false, reason: "busy", message: "Your answer is already on its way." };
  const current = openRequest(store, shown.requestId);
  if (current === undefined || current.seq !== shown.seq) {
    return {
      ok: false,
      reason: "changed",
      message: current === undefined ? "That request is no longer open. Nothing was sent." : "That request changed while you were looking. Nothing was sent: read the new one.",
    };
  }
  mine.add(key);
  let result: ActionResult;
  try {
    result = await store.getState().respond(shown.requestId, decision);
  } finally {
    mine.delete(key);
  }
  return result.ok ? { ok: true } : { ok: false, reason: "refused", message: result.message };
}
