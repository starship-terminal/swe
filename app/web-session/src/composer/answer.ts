// Answering a request (an approval or a question) so that the press lands on the request the person saw.
//
// A request is named to the hub by its id alone, and an agent that restarts counts its ids again from
// `perm-1` (G11: ids are reused), so an answer meant for the request on screen can reach a newer one that
// took its place. Each press therefore carries the request's `seq` (the journal position of the event the
// person saw) and is refused, before anything is posted, unless the latest open request with that id still
// has that `seq`. Check and post happen in one synchronous step, so no frame can arrive between them.
import type { ActionResult, RequestEvent, SessionStore } from "../core";
import { forgetPosted, postedGeneration, readPosted, writePosted, type Posted } from "./posted";

export interface Shown {
  requestId: string;
  seq: number;
  /** The journal's epoch when the request was shown: `seq` counts again after a reset, so it is part of the request's identity. */
  epoch: string;
}

/** The epoch the store holds now, as the text `Shown` carries. */
export const epochOf = (store: SessionStore): string => String(store.getState().session.epoch ?? "");

// What this view has posted for a request is kept by `posted.ts` (storage per session, memory when storage is off), so a reload
// or a second tab knows: the invariant is that this view posts at most one decision value for a request. The hub stores each
// answer as its own command, and for a machine that is asleep two answers are both held and delivered in order when it wakes,
// so a second, different answer is never taken (the first wins) and offering one would only be a lie. `taken`: the hub
// answered 2xx. `unknown`: no HTTP answer, or a server error, or a timeout, or the post is still on its way (a reload
// during it): the hub may or may not have it, and only the same decision may be sent again (a duplicate is harmless: the
// second fails on a request that is already closed). A refusal (a 4xx, the connection gate) posted nothing and is not kept.
export type { Posted } from "./posted";

export type AnswerOutcome =
  | { ok: true; receipt: string }
  | { ok: false; reason: "changed" | "busy" | "refused" | "decided" | "unknown"; message: string };

/** What was posted for this request, if anything. It outlives the dock and the page: a dock drawn again for the same request knows. */
export function postedFor(scope: string, shown: Shown): Posted | undefined {
  return readPosted(scope, shown);
}

const postedKey = (shown: Shown): string => `${shown.epoch}\u0000${shown.requestId}\u0000${shown.seq}`;

/** Whether a refused answer may still have reached the hub: a timeout, a server error or no answer at all may; a plain refusal did not. */
function inDoubt(result: Extract<ActionResult, { ok: false }>): boolean {
  if (result.reason === "network") return true;
  return result.reason === "refused" && (result.status === 408 || result.status >= 500);
}

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

/** `scope` is the session key the record is kept under (the composer's `sessionKey`). */
export async function answer(store: SessionStore, scope: string, shown: Shown, decision: string, label = decision): Promise<AnswerOutcome> {
  const key = postedKey(shown);
  let mine = answering.get(store);
  if (mine === undefined) answering.set(store, (mine = new Set()));
  if (mine.has(key)) return { ok: false, reason: "busy", message: "Your answer is already on its way." };
  const before = postedFor(scope, shown);
  if (before !== undefined) {
    // At most one decision value per request. A different one is never posted; the same one only when the first may not have arrived.
    if (before.decision !== decision) {
      return { ok: false, reason: "decided", message: `You already answered this request (${before.label}). It cannot be answered again with something else.` };
    }
    if (before.outcome === "taken") return { ok: false, reason: "decided", message: "The hub already has your answer. Nothing more was sent." };
  }
  const current = openRequest(store, shown.requestId);
  if (current === undefined || current.seq !== shown.seq || epochOf(store) !== shown.epoch) {
    return {
      ok: false,
      reason: "changed",
      message: current === undefined ? "That request is no longer open. Nothing was sent." : "That request changed while you were looking. Nothing was sent: read the new one.",
    };
  }
  mine.add(key);
  // Taken before the post: a sign-out while it is on its way ends what may be written for it.
  const since = postedGeneration();
  // Written before the post leaves: a reload while it is on its way, or another tab, must find it already in doubt.
  writePosted(scope, shown, { decision, label, outcome: "unknown" }, since);
  let result: ActionResult;
  try {
    result = await store.getState().respond(shown.requestId, decision);
  } catch (error) {
    mine.delete(key);
    throw error;
  }
  mine.delete(key);
  if (result.ok) {
    writePosted(scope, shown, { decision, label, outcome: "taken", receipt: result.receipt }, since);
    return { ok: true, receipt: result.receipt };
  }
  if (inDoubt(result)) return { ok: false, reason: "unknown", message: result.message };
  // A definite refusal: nothing reached the hub, so the answers come back (unless an earlier post of this decision is still in doubt).
  if (before === undefined) forgetPosted(scope, shown, since);
  else writePosted(scope, shown, before, since);
  return { ok: false, reason: "refused", message: result.message };
}
