// Where the outbox keeps what it has not finished sending.
//
// IndexedDB survives a reload. It can be missing or throw (a private window,
// blocked site data, a locked-down embedded view), so the outbox always keeps
// its entries in memory too and treats the database as a write-through copy:
// when a call fails the storage turns itself off, says why, and the outbox
// carries on in memory.

/**
 * Where a send is in its life (docs/specs/G11-client-core.md, "The send lifecycle").
 *
 * - `waiting`: not delivered to the hub yet. Never posted (`attempts` 0), or posted and due to be tried
 *   again (`lastError` says why).
 * - `sent`: the hub answered 2xx; `path` says which kind of hub path took it. Waits for its `command.result`.
 * - `failed`: terminal. The hub refused it, or it could not be delivered, or it may or may not have been
 *   sent; `error` says which.
 * - `unconfirmed`: terminal. The hub took it but no result came in the time one should have; `error` says so.
 *
 * An entry leaves the outbox only on a `command.result` that says ok (or that a queue action cancelled it),
 * or when the person dismisses it. Terminal entries are kept for `TERMINAL_KEPT_MS` after they end.
 */
export type SendStatus = "waiting" | "sent" | "failed" | "unconfirmed";

/** One send, as it is stored. */
export interface OutboxEntry {
  /** Made once, when the send is typed, and kept through every attempt: sent as `command_id` and as `client_id`. */
  commandId: string;
  session: string;
  text: string;
  attachments: string[];
  mode: "queue" | "steer";
  createdAt: number;
  status: SendStatus;
  /** Posts made so far. */
  attempts: number;
  /**
   * Which kind of hub path took it, from the hub's 2xx answer: `keeper` when the answer carried a receipt
   * (the hub stored the command under its id and answers a repeat with the stored receipt and result),
   * `legacy` when it did not (a session no keeper holds: the hub does not de-duplicate).
   */
  path?: "keeper" | "legacy";
  /** The hub's receipt (accepted, queued, duplicate, held), on the keeper path. */
  receipt?: string;
  /** When the hub's 2xx answer came. */
  sentAt?: number;
  /** The last attempt got no answer, or one that may mean the hub applied it: a repeat is safe only where the hub de-duplicates. */
  ambiguous?: boolean;
  /** Why the last attempt did not land, while it waits to be tried again. */
  lastError?: string;
  /** Why it failed or is unconfirmed. */
  error?: string;
  /** When it reached `failed` or `unconfirmed`. */
  endedAt?: number;
}

export interface OutboxStorage {
  /** `indexeddb`, or `memory` when there is no database to write to. */
  readonly kind: "indexeddb" | "memory";
  /** Why the database is not in use, when it is not. */
  readonly note: string;
  load(session: string): Promise<OutboxEntry[]>;
  put(entry: OutboxEntry): Promise<void>;
  remove(commandId: string): Promise<void>;
  close(): void;
}

/** A storage that keeps nothing beyond the outbox's own memory. */
export class MemoryStorage implements OutboxStorage {
  readonly kind = "memory" as const;

  constructor(readonly note: string = "") {}

  async load(): Promise<OutboxEntry[]> {
    return [];
  }
  async put(): Promise<void> {}
  async remove(): Promise<void> {}
  close(): void {}
}

/** A storage that is still opening: calls wait for it, and until it is open it reports memory. */
export class DeferredStorage implements OutboxStorage {
  private inner: OutboxStorage = new MemoryStorage();
  private closed = false;
  private readonly ready: Promise<OutboxStorage>;

  constructor(opening: Promise<OutboxStorage>) {
    this.ready = opening.then((storage) => {
      if (this.closed) storage.close();
      else this.inner = storage;
      return storage;
    });
  }

  get kind(): "indexeddb" | "memory" {
    return this.inner.kind;
  }

  get note(): string {
    return this.inner.note;
  }

  async load(session: string): Promise<OutboxEntry[]> {
    return (await this.ready).load(session);
  }

  async put(entry: OutboxEntry): Promise<void> {
    await (await this.ready).put(entry);
  }

  async remove(commandId: string): Promise<void> {
    await (await this.ready).remove(commandId);
  }

  close(): void {
    this.closed = true;
    this.inner.close();
  }
}

const DB_NAME = "swe-session";
const STORE = "outbox";

function request<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("IndexedDB request failed"));
  });
}

function done(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error ?? new Error("IndexedDB transaction aborted"));
    tx.onerror = () => reject(tx.error ?? new Error("IndexedDB transaction failed"));
  });
}

class IndexedDbStorage implements OutboxStorage {
  readonly kind = "indexeddb" as const;
  readonly note = "";

  constructor(private readonly db: IDBDatabase) {}

  async load(session: string): Promise<OutboxEntry[]> {
    const tx = this.db.transaction(STORE, "readonly");
    const all = await request(tx.objectStore(STORE).index("session").getAll(session));
    return (all as OutboxEntry[]).sort((a, b) => a.createdAt - b.createdAt || a.commandId.localeCompare(b.commandId));
  }

  async put(entry: OutboxEntry): Promise<void> {
    const tx = this.db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(entry);
    await done(tx);
  }

  async remove(commandId: string): Promise<void> {
    const tx = this.db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(commandId);
    await done(tx);
  }

  close(): void {
    this.db.close();
  }
}

/** Wraps a storage that can fail: the first failure turns it into a memory storage that says why. */
export class FallbackStorage implements OutboxStorage {
  private inner: OutboxStorage;
  private failure = "";

  constructor(
    inner: OutboxStorage,
    private readonly onFallback: (note: string) => void = () => {},
  ) {
    this.inner = inner;
  }

  get kind(): "indexeddb" | "memory" {
    return this.inner.kind;
  }

  get note(): string {
    return this.failure || this.inner.note;
  }

  private fail(error: unknown): void {
    if (this.inner.kind === "memory") return;
    const text = error instanceof Error ? error.message : String(error);
    this.failure = `IndexedDB failed (${text}); sends are kept in memory and will not survive a reload`;
    try {
      this.inner.close();
    } catch {
      // Already broken.
    }
    this.inner = new MemoryStorage(this.failure);
    this.onFallback(this.failure);
  }

  async load(session: string): Promise<OutboxEntry[]> {
    try {
      return await this.inner.load(session);
    } catch (error) {
      this.fail(error);
      return [];
    }
  }

  async put(entry: OutboxEntry): Promise<void> {
    try {
      await this.inner.put(entry);
    } catch (error) {
      this.fail(error);
    }
  }

  async remove(commandId: string): Promise<void> {
    try {
      await this.inner.remove(commandId);
    } catch (error) {
      this.fail(error);
    }
  }

  close(): void {
    this.inner.close();
  }
}

/** The page's `indexedDB`. Reading it throws in some sandboxed frames, and it is undefined in others. */
function pageIndexedDb(): IDBFactory | undefined {
  try {
    return typeof indexedDB === "undefined" ? undefined : indexedDB;
  } catch {
    return undefined;
  }
}

/**
 * Open the outbox's database, or fall back to memory. `factory` is the
 * `indexedDB` to use (tests pass one, or one that throws); by default the
 * page's own, which may be missing.
 */
export async function openOutboxStorage(
  factory: IDBFactory | undefined = pageIndexedDb(),
  onFallback?: (note: string) => void,
): Promise<OutboxStorage> {
  if (factory === undefined) {
    const note = "IndexedDB is not available here; sends are kept in memory and will not survive a reload";
    onFallback?.(note);
    return new MemoryStorage(note);
  }
  try {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const open = factory.open(DB_NAME, 1);
      open.onupgradeneeded = () => {
        const created = open.result.createObjectStore(STORE, { keyPath: "commandId" });
        created.createIndex("session", "session");
      };
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(open.error ?? new Error("IndexedDB could not be opened"));
      open.onblocked = () => reject(new Error("IndexedDB is blocked"));
    });
    return new FallbackStorage(new IndexedDbStorage(db), onFallback);
  } catch (error) {
    const text = error instanceof Error ? error.message : String(error);
    const note = `IndexedDB is not available (${text}); sends are kept in memory and will not survive a reload`;
    onFallback?.(note);
    return new MemoryStorage(note);
  }
}
