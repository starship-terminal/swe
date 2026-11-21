// Where the outbox keeps what it has not finished sending.
//
// IndexedDB survives a reload. It can be missing or throw (a private window,
// blocked site data, a locked-down embedded view), so the outbox always keeps
// its entries in memory too and treats the database as a write-through copy:
// when a call fails the storage turns itself off, says why, and the outbox
// carries on in memory.

/** One send, as it is stored. */
export interface OutboxEntry {
  /** Made once, when the send is typed, and kept through every retry. The hub applies it once. */
  commandId: string;
  session: string;
  text: string;
  attachments: string[];
  mode: "queue" | "steer";
  createdAt: number;
  /**
   * `pending`: no receipt yet, retried until there is one.
   * `sent`: the hub gave a receipt; waiting for the command's result.
   * `failed`: the hub refused it, or it expired; kept so the person can see why.
   */
  status: "pending" | "sent" | "failed";
  /** Posts made so far. */
  attempts: number;
  /** The hub's receipt (accepted, queued, duplicate, held), once there is one. */
  receipt?: string;
  /** Why it failed. */
  error?: string;
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
