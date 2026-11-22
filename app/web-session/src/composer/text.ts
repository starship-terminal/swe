// Text from an agent or the hub, drawn in the docks. It is shown as text and never as markup, cut to a
// size the page can afford, and never changed in its letters: a cut says it cut.
import { clip } from "../session/format";

/** How many options a request shows; a request that lists more says so, and the rest are answered elsewhere. */
export const OPTIONS_SHOWN = 20;
/** How many characters an option's label shows. */
export const LABEL_SHOWN = 300;
/** How many characters a tool's name shows. */
export const NAME_SHOWN = 120;

/** The first `max` characters of a text, never ending in half a surrogate pair. */
export function cut(text: string, max: number): { text: string; cut: boolean } {
  if (text.length <= max) return { text, cut: false };
  let end = max;
  const last = text.charCodeAt(end - 1);
  if (last >= 0xd800 && last <= 0xdbff) end--;
  return { text: text.slice(0, end), cut: true };
}

/**
 * A label for a button: its first characters, with an ellipsis when it was cut. The text is the hub's and is not
 * changed: the only thing taken off is whitespace at its two ends (`String.prototype.trim`), never anything inside
 * it; the cut, when there is one, is the only other difference and is marked.
 */
export function label(text: string): string {
  const shown = cut(text.trim(), LABEL_SHOWN);
  return shown.cut ? `${shown.text}…` : shown.text;
}

/** A tool's input as text: a string as it is, anything else as JSON; whatever cannot be read says so. */
export function inputText(input: unknown): string {
  if (typeof input === "string") return input;
  if (input === undefined) return "";
  try {
    return JSON.stringify(input, null, 2) ?? "";
  } catch {
    // Too deep to write out.
    return "";
  }
}

export { clip };

/** "1 message", "3 messages". */
export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}
