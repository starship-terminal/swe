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
  for (const text of (diff ?? "").split("\n")) {
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

/** Cut a long text for drawing: the start, and how much was left out. */
export function clip(text: string, max: number): { text: string; hidden: number } {
  if (text.length <= max) return { text, hidden: 0 };
  return { text: text.slice(0, max), hidden: text.length - max };
}
