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
  /**
   * The page that is posting it, and since when. Written in the same transaction that counts the attempt,
   * before the request leaves, and cleared with the answer. A claim another page holds that is fresher than
   * `CLAIM_STALE_MS` means "another tab is sending"; an older one was abandoned (the page closed mid-post).
   */
  claim?: { owner: string; at: number };
}

/** How long a claim stands before it counts as abandoned: longer than a request may take (30 s). */
export const CLAIM_STALE_MS = 60_000;

/** What claiming an entry found. */
export type Claim =
  /** This page may post it: the attempt is counted and on disk. `ambiguous` is set when an earlier claim was abandoned. */
  | { kind: "claimed"; entry: OutboxEntry }
  /** Another page claimed it moments ago and is posting it. */
  | { kind: "busy"; entry: OutboxEntry }
  /** It is no longer waiting (another page sent or ended it): this is how it stands. */
  | { kind: "settled"; entry: OutboxEntry }
  /** It is gone from storage (another page saw it through). */
  | { kind: "missing" };

/**
 * An abandoned claim, resolved without posting: a stale claim is removed and the entry marked `ambiguous` (its
 * post may have reached the hub), so its own deadlines and the repeat rule apply from here. Anything else is
 * left as stored.
 */
export function decideAbandon(
  stored: OutboxEntry | undefined,
  now: number,
  staleMs: number,
): { entry: OutboxEntry | undefined; write?: OutboxEntry } {
  if (stored === undefined || stored.status !== "waiting" || stored.claim === undefined) return { entry: stored };
  if (now - stored.claim.at < staleMs) return { entry: stored };
  const { claim: _stale, ...rest } = stored;
  const abandoned: OutboxEntry = { ...rest, ambiguous: true };
  return { entry: abandoned, write: abandoned };
}

/**
 * The claim rule, the same for every storage: given the entry as stored, may `owner` post it now? An
 * abandoned claim (another page's, older than `staleMs`, or this page's own) means an earlier post may have
 * reached the hub, so the entry comes back `ambiguous` and the outbox's repeat rule decides.
 */
export function decideClaim(
  stored: OutboxEntry | undefined,
  owner: string,
  now: number,
  staleMs: number,
): { claim: Claim; write?: OutboxEntry } {
  if (stored === undefined) return { claim: { kind: "missing" } };
  // `pending` is what an earlier build of the outbox called `waiting`.
  if ((stored.status as string) === "pending") stored = { ...stored, status: "waiting" };
  if (stored.status !== "waiting") return { claim: { kind: "settled", entry: stored } };
  let entry = stored;
  if (entry.claim !== undefined) {
    // A fresh claim is a post on its way: another tab's, or this page's from before its outbox restarted.
    if (now - entry.claim.at < staleMs) return { claim: { kind: "busy", entry } };
    entry = { ...entry, ambiguous: true };
  }
  const claimed: OutboxEntry = { ...entry, attempts: entry.attempts + 1, claim: { owner, at: now } };
  return { claim: { kind: "claimed", entry: claimed }, write: claimed };
}

export interface OutboxStorage {
  /** `indexeddb`, or `memory` when there is no database to write to. */
  readonly kind: "indexeddb" | "memory";
  /** Why the database is not in use, when it is not. */
  readonly note: string;
  load(session: string): Promise<OutboxEntry[]>;
  put(entry: OutboxEntry): Promise<void>;
  remove(commandId: string): Promise<void>;
  /**
   * Claim an entry for posting, atomically: read it as stored and, by `decideClaim`, write the claim and the
   * counted attempt in the same transaction. `entry` is the outbox's own copy, which a storage with nothing
   * stored (memory) decides on.
   */
  claim(entry: OutboxEntry, owner: string, now: number, staleMs: number): Promise<Claim>;
  /** Resolve a stale claim on an entry, atomically, by `decideAbandon`. Answers the entry as it now stands. */
  abandon(entry: OutboxEntry, now: number, staleMs: number): Promise<OutboxEntry | undefined>;
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
  /** In memory there are no other pages to coordinate with: the outbox's own copy decides. */
  async claim(entry: OutboxEntry, owner: string, now: number, staleMs: number): Promise<Claim> {
    return decideClaim(entry, owner, now, staleMs).claim;
  }
  async abandon(entry: OutboxEntry, now: number, staleMs: number): Promise<OutboxEntry | undefined> {
    return decideAbandon(entry, now, staleMs).entry;
  }
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

  async claim(entry: OutboxEntry, owner: string, now: number, staleMs: number): Promise<Claim> {
    return (await this.ready).claim(entry, owner, now, staleMs);
  }

  async abandon(entry: OutboxEntry, now: number, staleMs: number): Promise<OutboxEntry | undefined> {
    return (await this.ready).abandon(entry, now, staleMs);
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

  /** One read-write transaction: IndexedDB runs them one at a time across every tab, so two pages cannot both claim. */
  async claim(entry: OutboxEntry, owner: string, now: number, staleMs: number): Promise<Claim> {
    const tx = this.db.transaction(STORE, "readwrite");
    const finished = done(tx);
    const store = tx.objectStore(STORE);
    const claim = await new Promise<Claim>((resolve, reject) => {
      const read = store.get(entry.commandId);
      read.onerror = () => reject(read.error ?? new Error("IndexedDB read failed"));
      read.onsuccess = () => {
        const decided = decideClaim(read.result as OutboxEntry | undefined, owner, now, staleMs);
        if (decided.write !== undefined) store.put(decided.write);
        resolve(decided.claim);
      };
    });
    await finished;
    return claim;
  }

  async abandon(entry: OutboxEntry, now: number, staleMs: number): Promise<OutboxEntry | undefined> {
    const tx = this.db.transaction(STORE, "readwrite");
    const finished = done(tx);
    const store = tx.objectStore(STORE);
    const result = await new Promise<OutboxEntry | undefined>((resolve, reject) => {
      const read = store.get(entry.commandId);
      read.onerror = () => reject(read.error ?? new Error("IndexedDB read failed"));
      read.onsuccess = () => {
        const decided = decideAbandon(read.result as OutboxEntry | undefined, now, staleMs);
        if (decided.write !== undefined) store.put(decided.write);
        resolve(decided.entry);
      };
    });
    await finished;
    return result;
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

  async claim(entry: OutboxEntry, owner: string, now: number, staleMs: number): Promise<Claim> {
    try {
      return await this.inner.claim(entry, owner, now, staleMs);
    } catch (error) {
      this.fail(error);
      return this.inner.claim(entry, owner, now, staleMs);
    }
  }

  async abandon(entry: OutboxEntry, now: number, staleMs: number): Promise<OutboxEntry | undefined> {
    try {
      return await this.inner.abandon(entry, now, staleMs);
    } catch (error) {
      this.fail(error);
      return this.inner.abandon(entry, now, staleMs);
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
