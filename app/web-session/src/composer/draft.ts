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
/** A draft longer than this is kept in memory only: it would not fit a browser's storage quota. */
export const DRAFT_KEPT_CHARS = 200_000;
export const FILES_KEPT = 10;

const memory = new Map<string, Draft>();
/** Sessions whose latest draft is in `memory` only: storage holds an older one, or none. */
const unsaved = new Set<string>();

function backing(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function parse(raw: string): Draft | null {
  try {
    const value = JSON.parse(raw) as { t?: unknown; f?: unknown } | null;
    if (value === null || typeof value !== "object" || typeof value.t !== "string") return null;
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
  if (unsaved.has(session)) {
    const newer = memory.get(session);
    if (newer !== undefined) return newer;
  }
  try {
    const raw = backing()?.getItem(PREFIX + session) ?? null;
    if (raw !== null) {
      const found = parse(raw);
      if (found !== null) return found;
    }
  } catch {
    // Unreadable storage: fall through to memory.
  }
  return memory.get(session) ?? EMPTY_DRAFT;
}

export type Kept = "storage" | "memory";

/** Keep a draft. An empty one removes what was kept. Says where it ended up. */
export function writeDraft(session: string, draft: Draft): Kept {
  const empty = draft.text === "" && draft.files.length === 0;
  if (empty) {
    memory.delete(session);
    unsaved.delete(session);
    try {
      backing()?.removeItem(PREFIX + session);
    } catch {
      // Nothing to remove, or storage is off.
    }
    return "storage";
  }
  memory.set(session, draft);
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
    store.setItem(PREFIX + session, JSON.stringify({ t: draft.text, f: draft.files.slice(0, FILES_KEPT) }));
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
  unsaved.clear();
}
