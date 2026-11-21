// Sending the message in the box. One press sends once.
//
// The store's `send` is accepted at once and kept (G11), but it is a promise that takes a moment (the
// first write to the device's storage), and a person, a key held down, a re-render or a second composer
// can ask again in that moment. So a send is refused while another one for the same store is on its way,
// whatever the buttons look like.
import type { SendResult, SessionStore } from "../core";

export const MAX_MESSAGE_CHARS = 200_000;

export type SubmitFailure =
  | { ok: false; reason: "empty"; message: string }
  | { ok: false; reason: "busy"; message: string }
  | { ok: false; reason: "loading"; message: string }
  | { ok: false; reason: "too_long"; message: string }
  | { ok: false; reason: "refused"; message: string }
  | { ok: false; reason: "error"; message: string };

export type SubmitResult = Extract<SendResult, { ok: true }> | SubmitFailure;

const sending = new WeakSet<SessionStore>();

/** A message is on its way from this store. */
export function isSending(store: SessionStore): boolean {
  return sending.has(store);
}

/**
 * Send `text` (with the attachments staged for it). The text goes as typed, minus the blank space at its
 * end. Nothing is sent, and nothing is cleared by the caller, unless the answer is `ok`: a refusal and an
 * error leave the draft where it is.
 */
export async function submitMessage(
  store: SessionStore,
  input: { text: string; attachments: string[]; mode: "queue" | "steer" },
): Promise<SubmitResult> {
  const text = input.text.replace(/\s+$/, "");
  if (text === "" && input.attachments.length === 0) return { ok: false, reason: "empty", message: "Write a message first." };
  if (text.length > MAX_MESSAGE_CHARS) {
    return { ok: false, reason: "too_long", message: `This message is over ${MAX_MESSAGE_CHARS.toLocaleString("en-US")} characters. Shorten it to send it.` };
  }
  if (sending.has(store)) return { ok: false, reason: "busy", message: "Still sending your last message." };
  // While the outbox reads what this device kept, a send would be drawn beside entries that may be about to
  // change, and in a browser whose storage never answers it would never settle: keep the draft instead.
  if (store.getState().outbox.loading) {
    return { ok: false, reason: "loading", message: "Loading the sends kept on this device. Try again in a moment." };
  }
  // A steer needs a reply to stop. If the turn ended since the button was drawn, the message goes as a plain
  // send: that is what the person wants (it goes now), and the hub refuses a steer when idle.
  let mode = input.mode;
  if (mode === "steer" && !turnOpen(store)) mode = "queue";
  sending.add(store);
  try {
    const result = await store.getState().send({ text, mode, ...(input.attachments.length > 0 ? { attachments: input.attachments } : {}) });
    if (result.ok) return result;
    return { ok: false, reason: "refused", message: result.message };
  } catch (error) {
    return { ok: false, reason: "error", message: error instanceof Error && error.message !== "" ? error.message : "The message could not be saved on this device." };
  } finally {
    sending.delete(store);
  }
}

/** A reply is in progress: the session has a turn open, or says it is running or waiting on someone. */
export function turnOpen(store: SessionStore): boolean {
  const { openTurn, phase } = store.getState().session;
  return openTurn !== null || phase === "running" || phase === "waiting";
}
