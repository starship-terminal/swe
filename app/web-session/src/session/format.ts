// Small text rules the timeline shares. Pure, so they are tested without a screen.

/** "14s", "2m 29s", "1h 3m": a span of seconds as a person says it. Under a second reads "under 1s". */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 1) return "under 1s";
  const whole = Math.round(seconds);
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) {
    const rest = whole % 60;
    return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
  }
  const hours = Math.floor(minutes / 60);
  const left = minutes % 60;
  return left === 0 ? `${hours}h` : `${hours}h ${left}m`;
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

export type DiffKind = "add" | "remove" | "hunk" | "context" | "header";

/**
 * Sort the lines of a unified diff. The `---` and `+++` of a file header are told from a changed line
 * that happens to start the same way (the source line `++counter;` is `+++counter;` in a diff) by where
 * they sit: a hunk header (`@@ -a,b +c,d @@`) says how many old and new lines follow, and only lines
 * outside a hunk are headers. A diff with no hunk header at all falls back to treating `--- ` and
 * `+++ ` as headers and every other `+` and `-` line as a change.
 */
export function diffLines(diff: string | undefined): { text: string; kind: DiffKind }[] {
  const out: { text: string; kind: DiffKind }[] = [];
  let old = 0;
  let next = 0;
  let sawHunk = false;
  const source = (diff ?? "").split("\n");
  // A diff ends with a newline: that is the end of its last line, not a blank line of its own.
  if (source.length > 1 && source[source.length - 1] === "") source.pop();
  for (const text of source) {
    const hunk = /^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/.exec(text);
    if (hunk !== null) {
      sawHunk = true;
      old = hunk[1] === undefined ? 1 : Number(hunk[1]);
      next = hunk[2] === undefined ? 1 : Number(hunk[2]);
      out.push({ text, kind: "hunk" });
    } else if (old > 0 || next > 0) {
      // Inside a hunk: the counts decide, and a "\ No newline" marker is neither side.
      if (text.startsWith("+")) {
        next--;
        out.push({ text, kind: "add" });
      } else if (text.startsWith("-")) {
        old--;
        out.push({ text, kind: "remove" });
      } else if (text.startsWith("\\")) {
        out.push({ text, kind: "context" });
      } else {
        old--;
        next--;
        out.push({ text, kind: "context" });
      }
    } else if (!sawHunk && text.startsWith("+") && !text.startsWith("+++ ")) {
      out.push({ text, kind: "add" });
    } else if (!sawHunk && text.startsWith("-") && !text.startsWith("--- ")) {
      out.push({ text, kind: "remove" });
    } else {
      out.push({ text, kind: text.startsWith("+++") || text.startsWith("---") || text.startsWith("diff ") || text.startsWith("index ") ? "header" : "context" });
    }
  }
  return out;
}

/** The lines a unified diff adds and removes. */
export function diffStat(diff: string | undefined): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of diffLines(diff)) {
    if (line.kind === "add") added++;
    else if (line.kind === "remove") removed++;
  }
  return { added, removed };
}

/** The last part of a path: "app.py" for "src/app.py". */
export function baseName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  return cut < 0 ? trimmed : trimmed.slice(cut + 1);
}

/** How much of a text, or a diff, is drawn before "not shown": at most this many characters and this many lines. */
export const SHOWN_CHARS = 6000;
export const SHOWN_LINES = 300;

/**
 * Cut a long text for drawing: at most `maxLines` lines, then at most `maxChars` characters (one very long
 * line is cut too). `hidden` counts the characters left out.
 */
export function clip(text: string, maxChars = SHOWN_CHARS, maxLines = SHOWN_LINES): { text: string; hidden: number } {
  let shown = text;
  let at = -1;
  for (let line = 0; line < maxLines; line++) {
    at = shown.indexOf("\n", at + 1);
    if (at < 0) break;
    if (line === maxLines - 1) shown = shown.slice(0, at);
  }
  if (shown.length > maxChars) shown = shown.slice(0, maxChars);
  return { text: shown, hidden: text.length - shown.length };
}

/** A reason as a sentence: a capital to start, and a full stop to end (the hub's notes arrive as they were written). */
export function sentence(text: string): string {
  const trimmed = text.trim();
  if (trimmed === "") return trimmed;
  const cased = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(cased) ? cased : `${cased}.`;
}
