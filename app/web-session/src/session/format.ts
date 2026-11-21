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

/** The lines a unified diff adds and removes (its `+++` and `---` headers do not count). */
export function diffStat(diff: string | undefined): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of (diff ?? "").split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added++;
    else if (line.startsWith("-")) removed++;
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
