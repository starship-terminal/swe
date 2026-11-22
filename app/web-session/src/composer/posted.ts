// What this view has posted for a request, kept so a reload, a dropped web view or a second tab still knows.
//
// The invariant (answer.ts): one decision value per request. For a machine that is asleep the hub holds each answer
// and delivers them in order when it wakes, so a reload that forgot the first answer would let the person press the
// other one, and both would be held. So the record lives where a draft does (draft.ts): `localStorage`, one key per
// session (`swe.sv.answer.v1:<session key>`), holding one small entry per request and `seq`: the decision, the label
// shown, the outcome and the hub's receipt, nothing else (no prompt, no tool input, no text the person typed).
//
// How long: an entry is dropped when its request is no longer open (`prunePosted`, run when the session's snapshot has
// loaded), when it is older than `POSTED_MAX_AGE_MS` (14 days, as a draft), and with every draft when the device's
// credential is forgotten (`clearAllPosted`, called by `clearAllDrafts`; the web app's `pairForget` removes the keys).
// A browser that refuses storage keeps the record in memory for as long as the page lives; a second device, or a
// browser that refuses storage and is then reloaded, still knows nothing of it (spec, Limits).
//
// Two tabs: a `storage` event for the key tells the tab's open dock, which draws again and finds the entry.

import type { Shown } from "./answer";

export interface Posted {
  decision: string;
  /** What the person pressed, to say what was answered. */
  label: string;
  outcome: "taken" | "unknown";
  /** The hub's receipt for a taken answer: accepted, queued, or held (that machine is asleep). */
  receipt?: string;
}

const PREFIX = "swe.sv.answer.v1:";
/** An entry older than this is dropped: 14 days, as a draft. */
export const POSTED_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;
/** Entries kept per session: a session has few open requests, and a bound keeps a hostile agent from filling storage. */
const KEPT = 40;
const LABEL_KEPT = 200;

interface Entry extends Posted {
  /** When it was written (ms). */
  at: number;
}

const memory = new Map<string, Map<string, Entry>>();
const listeners = new Map<string, Set<() => void>>();

const entryKey = (shown: Shown): string => `${shown.requestId}\u0000${shown.seq}`;

function backing(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function shape(value: unknown, now: number): Entry | null {
  if (value === null || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (typeof v.d !== "string" || typeof v.l !== "string" || typeof v.a !== "number") return null;
  if (v.o !== "taken" && v.o !== "unknown") return null;
  if (now - v.a > POSTED_MAX_AGE_MS) return null;
  return { decision: v.d, label: v.l, outcome: v.o, at: v.a, ...(typeof v.r === "string" ? { receipt: v.r } : {}) };
}

/** What storage holds for a session: its entries that are well formed and not too old. Anything else is left out. */
function stored(scope: string, now: number): Map<string, Entry> | null {
  try {
    const raw = backing()?.getItem(PREFIX + scope) ?? null;
    if (raw === null) return new Map();
    const value = JSON.parse(raw) as unknown;
    if (value === null || typeof value !== "object" || Array.isArray(value)) return new Map();
    const found = new Map<string, Entry>();
    for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
      const good = shape(entry, now);
      if (good !== null) found.set(key, good);
    }
    return found;
  } catch {
    return null;
  }
}

function save(scope: string, entries: Map<string, Entry>): void {
  try {
    const store = backing();
    if (store === null) return;
    if (entries.size === 0) {
      store.removeItem(PREFIX + scope);
      return;
    }
    const out: Record<string, unknown> = {};
    for (const [key, entry] of entries) out[key] = { d: entry.decision, l: entry.label, o: entry.outcome, a: entry.at, ...(entry.receipt === undefined ? {} : { r: entry.receipt }) };
    store.setItem(PREFIX + scope, JSON.stringify(out));
  } catch {
    // Storage is off or full: the memory copy still holds it for this page.
  }
}

function tell(scope: string): void {
  for (const listener of listeners.get(scope) ?? []) listener();
}

/** What was posted for this request, if anything: storage first (another tab may have written it), then this page's memory. */
export function readPosted(scope: string, shown: Shown, now = Date.now()): Posted | undefined {
  const key = entryKey(shown);
  const fromStorage = stored(scope, now)?.get(key);
  const found = fromStorage ?? memory.get(scope)?.get(key);
  if (found === undefined || now - found.at > POSTED_MAX_AGE_MS) return undefined;
  const { at: _at, ...posted } = found;
  return posted;
}

/** Keep what was posted for this request, in storage and in memory. */
export function writePosted(scope: string, shown: Shown, entry: Posted, now = Date.now()): void {
  const key = entryKey(shown);
  const mine = memory.get(scope) ?? new Map<string, Entry>();
  memory.set(scope, mine);
  const row: Entry = { decision: entry.decision, label: entry.label.slice(0, LABEL_KEPT), outcome: entry.outcome, at: now, ...(entry.receipt === undefined ? {} : { receipt: entry.receipt }) };
  mine.set(key, row);
  // Storage keeps what it has (another tab's entries too) with this one added, newest last, within the bound.
  const all = stored(scope, now) ?? new Map<string, Entry>();
  all.delete(key);
  all.set(key, row);
  while (all.size > KEPT) all.delete(all.keys().next().value as string);
  save(scope, all);
  tell(scope);
}

/** Forget what was posted for this request: the post was refused outright, so nothing reached the hub. */
export function forgetPosted(scope: string, shown: Shown): void {
  const key = entryKey(shown);
  memory.get(scope)?.delete(key);
  const all = stored(scope, Date.now());
  if (all !== null) {
    all.delete(key);
    save(scope, all);
  }
  tell(scope);
}

/**
 * Drop the entries whose request is no longer open. `open` is every request now in `session.pending`; call it only when
 * the session's snapshot has loaded, since before that nothing is open and every entry would go.
 */
export function prunePosted(scope: string, open: Shown[]): void {
  const keep = new Set(open.map(entryKey));
  const mine = memory.get(scope);
  if (mine !== undefined) for (const key of [...mine.keys()]) if (!keep.has(key)) mine.delete(key);
  const all = stored(scope, Date.now());
  if (all === null) return;
  let changed = false;
  for (const key of [...all.keys()]) {
    if (!keep.has(key)) {
      all.delete(key);
      changed = true;
    }
  }
  // An entry that was too old or malformed is not in `all`, so it goes with the next save; write when anything went here.
  if (changed) save(scope, all);
}

/** Tell `listener` when the record for this session changes: here, or in another tab. Returns the way to stop. */
export function subscribePosted(scope: string, listener: () => void): () => void {
  const mine = listeners.get(scope) ?? new Set<() => void>();
  listeners.set(scope, mine);
  mine.add(listener);
  const onStorage = (event: StorageEvent): void => {
    if (event.key === null || event.key === PREFIX + scope) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    mine.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** Drop every record, on this page and in storage: the device was signed out. Open docks draw again and find nothing. */
export function clearAllPosted(): void {
  const scopes = new Set([...memory.keys(), ...listeners.keys()]);
  memory.clear();
  try {
    const store = backing();
    if (store !== null) {
      const keys: string[] = [];
      for (let i = 0; i < store.length; i++) {
        const key = store.key(i);
        if (key !== null && key.startsWith(PREFIX)) keys.push(key);
      }
      for (const key of keys) {
        scopes.add(key.slice(PREFIX.length));
        store.removeItem(key);
      }
    }
  } catch {
    // Storage off.
  }
  for (const scope of scopes) tell(scope);
}

/** Tests only: forget what this page holds in memory (a new page). */
export function forgetMemoryPosted(): void {
  memory.clear();
}
