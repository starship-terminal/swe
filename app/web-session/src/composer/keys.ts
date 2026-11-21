// What a key press in the message box means. Pure, so each case is tested without a screen.

export type EnterAction =
  /** Send the message. */
  | "send"
  /** Do nothing, and keep the key from typing: a held Enter after a send must not add blank lines to the next draft. */
  | "swallow"
  /** Leave the key to the box (a new line, or an input method choosing a word). */
  | "none";

export interface EnterInput {
  key: string;
  shiftKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  ctrlKey: boolean;
  repeat: boolean;
  keyCode: number;
  isComposing: boolean;
}

/**
 * Enter sends, Shift+Enter adds a line, and Enter never sends:
 *  - while an input method is composing (Japanese, Chinese, Korean and others use Enter to choose a word).
 *    Browsers report it three ways, and Safari fires `compositionend` before the key press that ended
 *    the composition, with `keyCode` 229, so all three are read: the event's `isComposing`, `keyCode` 229,
 *    and `composing`, which the caller tracks from `compositionstart` and `compositionend`;
 *  - as a repeat: a key held down must not send again (the first press sent, the rest are swallowed);
 *  - on a touch screen without Ctrl or Cmd: there Enter adds a line and the Send button sends.
 */
export function enterAction(event: EnterInput, composing: boolean, touch: boolean): EnterAction {
  if (event.key !== "Enter") return "none";
  if (composing || event.isComposing || event.keyCode === 229) return "none";
  if (event.shiftKey || event.altKey) return "none";
  if (touch && !event.metaKey && !event.ctrlKey) return "none";
  return event.repeat ? "swallow" : "send";
}

/** A phone or a tablet, where Enter is a line break: the primary pointer is coarse. */
export function touchScreen(): boolean {
  try {
    return typeof window.matchMedia === "function" && window.matchMedia("(pointer: coarse)").matches;
  } catch {
    return false;
  }
}
