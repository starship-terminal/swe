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
  /**
   * An earlier post may have been applied (no answer, a 5xx, an abandoned claim). It is posted again only when it is
   * keeper-only (`isKeeperOnly`) and the hub lists `require_keeper`, and then only with `require_keeper: true`.
   */
  ambiguous?: boolean;
  /**
   * The `attempts` count of the claim whose post carried `require_keeper: true` (G31), written in that claim's
   * transaction. It counts only while it equals `attempts` (`isKeeperOnly`): a claim by a build that does not know
   * the field counts an attempt without moving it, so its plain post leaves the entry not keeper-only.
   */
  keeperOnlyAt?: number;
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

/**
 * Whether the entry's latest claim was keeper-only, and so (on an `ambiguous` entry) every post of it that may have
 * been applied carried `require_keeper: true`.
 */
export function isKeeperOnly(entry: OutboxEntry): boolean {
  return entry.keeperOnlyAt !== undefined && entry.keeperOnlyAt === entry.attempts;
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
  | { kind: "missing" }
  /**
   * The database did not answer, and it may know this send: a claim other pages cannot see is not a claim, so
   * this page must not post it now. It stays waiting and is tried again.
   */
  | { kind: "unrecorded"; entry: OutboxEntry };

/**
 * An abandoned claim, resolved without posting: a stale claim is removed and the entry marked `ambiguous` (its
 * post may have reached the hub), so it is never posted again and its own deadlines apply. Anything else is
 * left as stored.
 */
export function decideAbandon(
  stored: OutboxEntry | undefined,
  now: number,
  staleMs: number,
): { entry: OutboxEntry | undefined; write?: OutboxEntry } {
  if (stored !== undefined) stored = normalizeEntry(stored);
  if (stored === undefined || stored.status !== "waiting" || stored.claim === undefined) return { entry: stored };
  if (now - stored.claim.at < staleMs) return { entry: stored };
  const { claim: _stale, ...rest } = stored;
  const abandoned: OutboxEntry = { ...rest, ambiguous: true };
  return { entry: abandoned, write: abandoned };
}

const STAGE: Record<OutboxEntry["status"], number> = { waiting: 0, sent: 2, failed: 3, unconfirmed: 3 };

/** An entry as an earlier build of the outbox stored it, in today's terms: `waiting` was called `pending`. */
export function normalizeEntry(entry: OutboxEntry): OutboxEntry {
  return (entry.status as string) === "pending" ? { ...entry, status: "waiting" } : entry;
}

/**
 * A copy's stage. `failed` and `ambiguous` (ended as "may or may not have been sent") is its own stage, between
 * `waiting` and `sent`: evidence that it arrived (a receipt) moves it on to `sent`. One whose status this build
 * does not know is below every known one, so it never wins.
 */
function stageOf(entry: OutboxEntry): number {
  if (entry.status === "failed" && entry.ambiguous === true) return 1;
  return STAGE[entry.status] ?? -1;
}

/**
 * Which of two copies of one send is further along its lifecycle: above 0 when `a` is, below 0 when `b` is, 0 when
 * the order cannot tell them apart (docs/specs/G11-client-core.md, "Two copies of one send"). In order: terminal over
 * `sent` over "may or may not have been sent" over `waiting`; more `attempts`; at equal attempts, no claim over a claim (a claim is only ever written
 * with attempts+1, so a claim-free copy at the same count is the released one); `ambiguous` over not.
 */
export function furtherAlong(a: OutboxEntry, b: OutboxEntry): number {
  return (
    stageOf(a) - stageOf(b) ||
    a.attempts - b.attempts ||
    Number(a.claim === undefined) - Number(b.claim === undefined) ||
    Number(a.ambiguous === true) - Number(b.ambiguous === true)
  );
}

/**
 * The claim rule, the same for every storage: given the entry as stored, may `owner` post it now? An
 * abandoned claim (another page's, older than `staleMs`, or this page's own) means an earlier post may have
 * reached the hub, so the entry comes back `ambiguous`: the outbox posts it again only keeper-only, or ends it.
 *
 * `keeperOnly` says whether this post will carry `require_keeper: true`. It is recorded with the claim
 * (`keeperOnlyAt`), except on an `ambiguous` entry, which stays keeper-only only if its latest claim was (the outbox
 * posts such an entry only then): every post of it that may have been applied must have gone keeper-only.
 */
export function decideClaim(
  stored: OutboxEntry | undefined,
  owner: string,
  now: number,
  staleMs: number,
  keeperOnly = false,
): { claim: Claim; write?: OutboxEntry } {
  if (stored === undefined) return { claim: { kind: "missing" } };
  stored = normalizeEntry(stored);
  if (stored.status !== "waiting") return { claim: { kind: "settled", entry: stored } };
  // Read before a takeover: a stale claim's post was keeper-only when its claim recorded it.
  const wasKeeperOnly = isKeeperOnly(stored);
  let entry = stored;
  if (entry.claim !== undefined) {
    // A fresh claim is a post on its way: another tab's, or this page's from before its outbox restarted.
    if (now - entry.claim.at < staleMs) return { claim: { kind: "busy", entry } };
    entry = { ...entry, ambiguous: true };
  }
  const claimed: OutboxEntry = { ...entry, attempts: entry.attempts + 1, claim: { owner, at: now } };
  if (claimed.ambiguous === true ? wasKeeperOnly : keeperOnly) claimed.keeperOnlyAt = claimed.attempts;
  else delete claimed.keeperOnlyAt;
  return { claim: { kind: "claimed", entry: claimed }, write: claimed };
}

export interface OutboxStorage {
  /** `indexeddb`, or `memory` when there is no database to write to. */
  readonly kind: "indexeddb" | "memory";
  /** Why the database is not in use, when it is not. */
  readonly note: string;
  /** A database exists but has stopped answering (only a fallback storage says so). */
  readonly down?: boolean;
  load(session: string): Promise<OutboxEntry[]>;
  put(entry: OutboxEntry): Promise<void>;
  remove(commandId: string): Promise<void>;
  /**
   * Claim an entry for posting, atomically: read it as stored and, by `decideClaim`, write the claim, the
   * counted attempt and `keeperOnlyAt` in the same transaction. `entry` is the outbox's own copy, which a storage
   * with nothing stored (memory) decides on.
   */
  claim(entry: OutboxEntry, owner: string, now: number, staleMs: number, keeperOnly?: boolean): Promise<Claim>;
  /** Resolve a stale claim on an entry, atomically, by `decideAbandon`. Answers the entry as it now stands. */
  abandon(entry: OutboxEntry, now: number, staleMs: number): Promise<OutboxEntry | undefined>;
  /**
   * Compare and write, atomically: read the stored copy, write `entry` only if it is further along (or nothing is
   * stored), and answer the copy that stands. Every write but a send's first goes through this: a copy chosen from
   * an earlier read must never overwrite what another page wrote since.
   */
  reconcile(entry: OutboxEntry): Promise<OutboxEntry>;
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
  async claim(entry: OutboxEntry, owner: string, now: number, staleMs: number, keeperOnly = false): Promise<Claim> {
    return decideClaim(entry, owner, now, staleMs, keeperOnly).claim;
  }
  async abandon(entry: OutboxEntry, now: number, staleMs: number): Promise<OutboxEntry | undefined> {
    return decideAbandon(entry, now, staleMs).entry;
  }
  async reconcile(entry: OutboxEntry): Promise<OutboxEntry> {
    return normalizeEntry(entry);
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

  get down(): boolean {
    return this.inner.down === true;
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

  async claim(entry: OutboxEntry, owner: string, now: number, staleMs: number, keeperOnly = false): Promise<Claim> {
    return (await this.ready).claim(entry, owner, now, staleMs, keeperOnly);
  }

  async abandon(entry: OutboxEntry, now: number, staleMs: number): Promise<OutboxEntry | undefined> {
    return (await this.ready).abandon(entry, now, staleMs);
  }

  async reconcile(entry: OutboxEntry): Promise<OutboxEntry> {
    return (await this.ready).reconcile(entry);
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
    return (all as OutboxEntry[]).map(normalizeEntry).sort((a, b) => a.createdAt - b.createdAt || a.commandId.localeCompare(b.commandId));
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
  async claim(entry: OutboxEntry, owner: string, now: number, staleMs: number, keeperOnly = false): Promise<Claim> {
    const tx = this.db.transaction(STORE, "readwrite");
    const finished = done(tx);
    const store = tx.objectStore(STORE);
    const claim = await new Promise<Claim>((resolve, reject) => {
      const read = store.get(entry.commandId);
      read.onerror = () => reject(read.error ?? new Error("IndexedDB read failed"));
      read.onsuccess = () => {
        const decided = decideClaim(read.result as OutboxEntry | undefined, owner, now, staleMs, keeperOnly);
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

  async reconcile(entry: OutboxEntry): Promise<OutboxEntry> {
    const tx = this.db.transaction(STORE, "readwrite");
    const finished = done(tx);
    const store = tx.objectStore(STORE);
    const standing = await new Promise<OutboxEntry>((resolve, reject) => {
      const read = store.get(entry.commandId);
      read.onerror = () => reject(read.error ?? new Error("IndexedDB read failed"));
      read.onsuccess = () => {
        const raw = read.result as OutboxEntry | undefined;
        const stored = raw === undefined ? undefined : normalizeEntry(raw);
        if (stored === undefined || furtherAlong(entry, stored) > 0) {
          store.put(entry);
          resolve(entry);
        } else {
          resolve(stored);
        }
      };
    });
    await finished;
    return standing;
  }

  close(): void {
    this.db.close();
  }
}

/**
 * Wraps the database (docs/specs/G11-client-core.md, "When storage fails"). An operation that fails is tried once
 * more on a fresh connection (`reopen`); if that fails too the storage is in fallback: what it was asked to write is
 * held in memory, and every later operation tries the database first. The first one that succeeds replays what was
 * held (the copy further along wins, see `furtherAlong`) and the storage is the database again.
 *
 * While in fallback every claim is `unrecorded`: another page reading the database would not see a claim made
 * here, and a page can never know that a write it heard fail did not land, so no id is certain to be only in
 * memory. The outbox posts nothing then, on any row.
 */
export class FallbackStorage implements OutboxStorage {
  private inner: OutboxStorage;
  private failure = "";
  /** Writes and removals the database has not taken yet, in the order asked. */
  private readonly held = new Map<string, OutboxEntry>();
  private readonly heldRemovals = new Set<string>();
  private closed = false;

  constructor(
    inner: OutboxStorage,
    /** Told when the storage falls back (with why) and when it is the database again (with ""). */
    private readonly onChange: (note: string) => void = () => {},
    /** A fresh connection to the same database, or null when none opens. Without one, `inner` is tried again. */
    private readonly reopen?: () => Promise<OutboxStorage | null>,
  ) {
    this.inner = inner;
  }

  get kind(): "indexeddb" | "memory" {
    return this.failure === "" ? this.inner.kind : "memory";
  }

  get note(): string {
    return this.failure || this.inner.note;
  }

  /** A database exists but has stopped answering: claims cannot be recorded. */
  get down(): boolean {
    return this.failure !== "";
  }

  /** Run `op` on the database: on the current connection, then once on a fresh one. Undefined when neither answers. */
  private async disk<T>(op: (db: OutboxStorage) => Promise<T>): Promise<{ value: T } | undefined> {
    let lastError: unknown;
    if (this.failure === "") {
      try {
        return { value: await op(this.inner) };
      } catch (error) {
        lastError = error;
      }
    }
    if (this.closed) return undefined;
    let fresh: OutboxStorage | null = this.inner;
    if (this.reopen !== undefined) {
      try {
        fresh = await this.reopen();
      } catch (error) {
        lastError = error;
        fresh = null;
      }
    }
    if (fresh !== null) {
      try {
        if (this.failure !== "" || this.held.size > 0 || this.heldRemovals.size > 0) await this.replay(fresh);
        const value = await op(fresh);
        this.useAgain(fresh);
        return { value };
      } catch (error) {
        lastError = error;
        if (fresh !== this.inner) this.quietClose(fresh);
      }
    }
    this.fallBack(lastError);
    return undefined;
  }

  /** What was held, into the database: a held copy is written where the database has none or an older one. */
  private async replay(db: OutboxStorage): Promise<void> {
    // Compare and write each held copy: another page may have moved the entry on while this one could not write.
    for (const entry of [...this.held.values()]) {
      await db.reconcile(entry);
      this.held.delete(entry.commandId);
    }
    for (const id of [...this.heldRemovals]) {
      await db.remove(id);
      this.heldRemovals.delete(id);
    }
  }

  private useAgain(db: OutboxStorage): void {
    if (db !== this.inner) {
      this.quietClose(this.inner);
      this.inner = db;
    }
    if (this.failure !== "") {
      this.failure = "";
      this.onChange("");
    }
  }

  private fallBack(error: unknown): void {
    const text = error instanceof Error ? error.message : String(error);
    const first = this.failure === "";
    this.failure =
      `IndexedDB failed (${text}). Sends saved before are not sent until it answers again, ` +
      "and new ones are kept in memory and will not survive a reload until then";
    if (first) this.onChange(this.failure);
  }

  private quietClose(db: OutboxStorage): void {
    try {
      db.close();
    } catch {
      // Already broken.
    }
  }

  async load(session: string): Promise<OutboxEntry[]> {
    const answer = await this.disk((db) => db.load(session));
    if (answer === undefined) return [...this.held.values()].filter((entry) => entry.session === session);
    return answer.value;
  }

  async put(entry: OutboxEntry): Promise<void> {
    const answer = await this.disk((db) => db.put(entry));
    if (answer !== undefined) return;
    this.heldRemovals.delete(entry.commandId);
    this.held.set(entry.commandId, entry);
  }

  async remove(commandId: string): Promise<void> {
    const answer = await this.disk((db) => db.remove(commandId));
    this.held.delete(commandId);
    // Held even for an id the database may never have had: a write heard failing may have landed. A removal the
    // database never takes is a Limit: a later page still finds the entry there.
    if (answer === undefined) this.heldRemovals.add(commandId);
  }

  async claim(entry: OutboxEntry, owner: string, now: number, staleMs: number, keeperOnly = false): Promise<Claim> {
    const answer = await this.disk((db) => db.claim(entry, owner, now, staleMs, keeperOnly));
    if (answer !== undefined) return answer.value;
    return { kind: "unrecorded", entry: this.held.get(entry.commandId) ?? entry };
  }

  async abandon(entry: OutboxEntry, now: number, staleMs: number): Promise<OutboxEntry | undefined> {
    const answer = await this.disk((db) => db.abandon(entry, now, staleMs));
    if (answer !== undefined) return answer.value;
    const decided = decideAbandon(this.held.get(entry.commandId) ?? entry, now, staleMs);
    if (decided.write !== undefined) this.held.set(entry.commandId, decided.write);
    return decided.entry;
  }

  async reconcile(entry: OutboxEntry): Promise<OutboxEntry> {
    const answer = await this.disk((db) => db.reconcile(entry));
    if (answer !== undefined) return answer.value;
    const held = this.held.get(entry.commandId);
    const standing = held !== undefined && furtherAlong(held, entry) >= 0 ? held : entry;
    this.heldRemovals.delete(entry.commandId);
    this.held.set(entry.commandId, standing);
    return standing;
  }

  close(): void {
    this.closed = true;
    this.quietClose(this.inner);
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
  const openDb = () =>
    new Promise<IDBDatabase>((resolve, reject) => {
      const open = factory.open(DB_NAME, 1);
      open.onupgradeneeded = () => {
        const created = open.result.createObjectStore(STORE, { keyPath: "commandId" });
        created.createIndex("session", "session");
      };
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(open.error ?? new Error("IndexedDB could not be opened"));
      open.onblocked = () => reject(new Error("IndexedDB is blocked"));
    });
  try {
    const db = await openDb();
    const reopen = async () => {
      try {
        return new IndexedDbStorage(await openDb());
      } catch {
        return null;
      }
    };
    return new FallbackStorage(new IndexedDbStorage(db), onFallback, reopen);
  } catch (error) {
    const text = error instanceof Error ? error.message : String(error);
    const note = `IndexedDB is not available (${text}); sends are kept in memory and will not survive a reload`;
    onFallback?.(note);
    return new MemoryStorage(note);
  }
}
