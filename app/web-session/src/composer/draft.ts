// The message being typed, kept so a reload, a crash or a closed tab does not lose it.
//
// Where: `localStorage`, one key per session (`swe.sv.draft.v1:<session key>`). The outbox (IndexedDB) holds
// messages the person has sent; a draft is text they have not, which the box needs to read at once, when
// the page first draws (IndexedDB answers later, and the box would flash empty and then fill), and which is
// small. A browser that refuses storage (a private window, a custom-scheme page, a full quota) keeps the
// draft in memory for as long as the page lives and `writeDraft` says so, so the box can tell the person it
// will not survive a reload.
//
// A draft is cleared only after the outbox has the message, never when a send is pressed.
//
// How long: a draft is dropped when it was last edited more than `DRAFT_MAX_AGE_MS` (14 days) ago, on read and in a
// sweep when the box opens; all of them are dropped when the device's credential is forgotten (the web app's
// `pairForget`, and a `storage` event from another tab for the credential key being removed), so a draft typed in a
// signed-in session does not outlive it on a shared browser.

export interface StagedFile {
  ref: string;
  name: string;
  size: number;
}

export interface Draft {
  text: string;
  files: StagedFile[];
}

export const EMPTY_DRAFT: Draft = { text: "", files: [] };

const PREFIX = "swe.sv.draft.v1:";
/** The saved pairing the web app keeps (and the name it had before); removing it is signing out. */
const CREDENTIAL_KEYS = ["swe.remote", "quiver.remote"];
/** A draft not edited for this long is dropped: 14 days. */
export const DRAFT_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;
/** A draft longer than this is kept in memory only: it would not fit a browser's storage quota. */
export const DRAFT_KEPT_CHARS = 200_000;
export const FILES_KEPT = 10;

const memory = new Map<string, Draft>();
/** When each draft in `memory` was last written: a page that stays open for weeks must not bring one back. */
const edited = new Map<string, number>();
/** Mounted boxes, told when every draft was dropped (the device signed out) so none writes its text back. */
const wipes = new Set<() => void>();

/** Tell `listener` whenever every draft is dropped. Returns the way to stop. */
export function onDraftsCleared(listener: () => void): () => void {
  wipes.add(listener);
  return () => void wipes.delete(listener);
}

function memoryFresh(session: string): boolean {
  const at = edited.get(session);
  if (at !== undefined && Date.now() - at <= DRAFT_MAX_AGE_MS) return true;
  memory.delete(session);
  unsaved.delete(session);
  edited.delete(session);
  return false;
}
/** Sessions whose latest draft is in `memory` only: storage holds an older one, or none. */
const unsaved = new Set<string>();

function backing(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function parse(raw: string, now: number): Draft | null {
  try {
    const value = JSON.parse(raw) as { t?: unknown; f?: unknown; a?: unknown } | null;
    if (value === null || typeof value !== "object" || typeof value.t !== "string") return null;
    // Last edited when; a draft with no time on it, or older than the limit, is gone.
    if (typeof value.a !== "number" || now - value.a > DRAFT_MAX_AGE_MS) return null;
    const files: StagedFile[] = [];
    if (Array.isArray(value.f)) {
      for (const entry of value.f.slice(0, FILES_KEPT)) {
        const item = entry as { ref?: unknown; name?: unknown; size?: unknown } | null;
        if (item !== null && typeof item === "object" && typeof item.ref === "string" && typeof item.name === "string") {
          files.push({ ref: item.ref, name: item.name, size: typeof item.size === "number" ? item.size : 0 });
        }
      }
    }
    return { text: value.t, files };
  } catch {
    return null;
  }
}

/**
 * The draft kept for a session: storage first, then what this page holds in memory. When the latest write
 * could not reach storage, memory is the newer copy and wins: storage still holds an older one.
 */
export function readDraft(session: string): Draft {
  if (unsaved.has(session) && memoryFresh(session)) {
    const newer = memory.get(session);
    if (newer !== undefined) return newer;
  }
  try {
    const raw = backing()?.getItem(PREFIX + session) ?? null;
    if (raw !== null) {
      const found = parse(raw, Date.now());
      if (found !== null) return found;
    }
  } catch {
    // Unreadable storage: fall through to memory.
  }
  return memoryFresh(session) ? (memory.get(session) ?? EMPTY_DRAFT) : EMPTY_DRAFT;
}

export type Kept = "storage" | "memory";

/** Keep a draft. An empty one removes what was kept. Says where it ended up. */
export function writeDraft(session: string, draft: Draft): Kept {
  const empty = draft.text === "" && draft.files.length === 0;
  if (empty) {
    memory.delete(session);
    edited.delete(session);
    unsaved.delete(session);
    try {
      backing()?.removeItem(PREFIX + session);
    } catch {
      // Nothing to remove, or storage is off.
    }
    return "storage";
  }
  memory.set(session, draft);
  edited.set(session, Date.now());
  if (draft.text.length > DRAFT_KEPT_CHARS) {
    unsaved.add(session);
    return "memory";
  }
  try {
    const store = backing();
    if (store === null) {
      unsaved.add(session);
      return "memory";
    }
    store.setItem(PREFIX + session, JSON.stringify({ t: draft.text, f: draft.files.slice(0, FILES_KEPT), a: Date.now() }));
    unsaved.delete(session);
    return "storage";
  } catch {
    unsaved.add(session);
    return "memory";
  }
}

/** Tests only: forget what this page holds in memory. */
export function forgetMemoryDrafts(): void {
  memory.clear();
  edited.clear();
  unsaved.clear();
}

function draftKeys(store: Storage): string[] {
  const keys: string[] = [];
  for (let i = 0; i < store.length; i++) {
    const key = store.key(i);
    if (key !== null && key.startsWith(PREFIX)) keys.push(key);
  }
  return keys;
}

/** Drop every kept draft last edited more than the limit ago. Run when the box opens. */
export function sweepDrafts(now = Date.now()): void {
  try {
    const store = backing();
    if (store === null) return;
    for (const key of draftKeys(store)) {
      if (parse(store.getItem(key) ?? "", now) === null) store.removeItem(key);
    }
  } catch {
    // Storage off: nothing is kept to sweep.
  }
}

/** Drop every kept draft, on this page and in storage: the device was signed out. */
export function clearAllDrafts(): void {
  memory.clear();
  edited.clear();
  unsaved.clear();
  for (const listener of wipes) listener();
  try {
    const store = backing();
    if (store === null) return;
    for (const key of draftKeys(store)) store.removeItem(key);
  } catch {
    // Storage off.
  }
}

/** Clear every draft when another tab removes the saved pairing (signs out). Returns the way to stop listening. */
export function dropDraftsOnSignOut(): () => void {
  const onStorage = (event: StorageEvent): void => {
    // `key === null` is `localStorage.clear()`; a pairing removed has no new value.
    if (event.key === null || (CREDENTIAL_KEYS.includes(event.key) && event.newValue === null)) clearAllDrafts();
  };
  window.addEventListener("storage", onStorage);
  return () => window.removeEventListener("storage", onStorage);
}
