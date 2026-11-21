// The outbox: sends the person typed, kept until the hub says what became of them.
//
// Only sends go through here. Approvals, Stop and Restore need a live
// connection and are refused while there is none (see the store).
//
// The life of a send is the state table in docs/specs/G11-client-core.md,
// "The send lifecycle". In short:
//
// - An entry leaves the outbox in exactly two ways: a `command.result` for
//   its id that says ok (or that a queue action cancelled it), or the person
//   dismissing it. Everything else ends in a terminal state with a reason the
//   view can show (`failed`, `unconfirmed`). A 2xx answer never removes an
//   entry, and nothing still open is dropped by age.
// - The first post is always safe: it carries one id as `command_id` and as
//   `client_id`. The hub's answer decides the path: a 2xx with a `receipt`
//   means the hub stored the command under that id (`keeper`), one without
//   means it did not (`legacy`).
// - The hub picks the route when a post arrives, and the route that is not
//   the keeper does not de-duplicate. So an answer that may mean "applied"
//   (none at all, 408, a 5xx, an abandoned claim) ends a plain post's send as
//   failed, "may or may not have been sent". An answer that says nothing was
//   stored (429, 425; 401 and 403 after the pause) is tried again.
// - With a hub that lists `require_keeper` (G31), a send on a keeper row is
//   posted with `require_keeper: true`: the hub takes it on the keeper path
//   or refuses it (409 `not_kept`), never on the legacy one. Such a send whose
//   answer may mean "applied" is posted again under the same id, keeper-only,
//   and the keeper's store answers a repeat with what it did the first time.
// - With a hub that lists `command_lookup`, sends the hub took and sends in
//   doubt are looked up (`GET .../commands/{id}`) at start and after each
//   catch-up, and settled from what the hub stored.
// - Deadlines run on their own timer, whether or not anything can be posted.
import { backoffDelay, DEFAULT_BACKOFF, type BackoffOptions, type Timers } from "./supervisor";
import { CLAIM_STALE_MS, furtherAlong, isKeeperOnly, normalizeEntry, type Claim, type OutboxEntry, type OutboxStorage } from "./outbox-storage";

export { furtherAlong };
import { sessionPath, type JsonResponse, type Transport } from "./transport";

/** How long a send that the hub has not taken is worth trying: the time the hub holds a message for a sleeping machine. */
export const OUTBOX_TTL_MS = 24 * 60 * 60 * 1000;

/**
 * How long a send on the legacy path may wait for its result. That path types the message in once the
 * session is idle and gives up after 30 minutes busy (`protocol.IDLE_WAIT_MAX_S`), reporting it as a
 * `command.result`; a result can still be missed (a catch-up by snapshot carries none), so after this
 * long the send ends as `unconfirmed`.
 */
export const LEGACY_CONFIRM_MS = 45 * 60 * 1000;

/** How long a failed or unconfirmed entry stays for the person to see, when nobody dismisses it. */
export const TERMINAL_KEPT_MS = 7 * 24 * 60 * 60 * 1000;

/** How many failed or unconfirmed entries a session keeps at most: the oldest go first, never an open one. */
export const TERMINAL_KEPT_MAX = 50;

/** Why a send ended without an answer on a path that cannot say whether it arrived. */
export const MAYBE_SENT =
  "The connection dropped before the hub answered. This message may or may not have been sent: check the session, and send it again if it did not arrive.";
export const NOT_SENT_IN_TIME = "Not sent: the hub could not be reached for 24 hours.";
export const LEGACY_UNCONFIRMED =
  "The hub took this message but never said whether it reached the session: check the session, and send it again if it is not there.";
export const KEEPER_UNCONFIRMED =
  "The hub took this message, but its result was not seen on this device. Check the session before sending it again.";
export const SESSION_GONE = "Not sent: this session no longer exists.";
/** Why a keeper-only send whose answer was lost waits: it is posted again under the same id. */
export const REPEATING =
  "The connection dropped before the hub answered. Sending it again under the same id, which the hub never delivers twice";
/** Why a keeper-only first post the hub refused with 409 `not_kept` waits: nothing was stored, and it is tried again. */
export const NOT_KEPT = "The session was not run by a session keeper when this was sent";

/** The `/v1/status` features the outbox relies on (G31). */
export const FEATURE_REQUIRE_KEEPER = "require_keeper";
export const FEATURE_COMMAND_LOOKUP = "command_lookup";

/** What the hub serving the live stream lists, read from `/v1/status`. */
export interface OutboxFeatures {
  /** A message post with `require_keeper: true` is taken on the keeper path or refused with 409 `not_kept`. */
  requireKeeper: boolean;
  /** `GET /v1/sessions/{key}/commands/{id}` answers the stored receipt and result. */
  lookup: boolean;
}
export const UNDELIVERED = "The hub could not deliver this message.";

export interface OutboxView {
  entries: OutboxEntry[];
  /** Whether sends survive a reload (IndexedDB is in use). */
  persistent: boolean;
  /** Why they do not, when they do not. */
  note: string;
  /** Set while the hub refuses the credential: nothing is posted until `wake()`. */
  paused: "unauthorized" | null;
  /** The entry being claimed for its post, or whose post is on its way, if any: it can be neither discarded nor retried. */
  inFlight: string | null;
  /** This page's claim owner: a claim with another owner is another tab's. */
  owner: string;
  /** Storage is still being loaded: what `entries` shows may change, so no action should be offered on it. */
  loading: boolean;
  /** The database has stopped answering: no send is posted until it answers again. */
  storageDown: boolean;
}

export interface OutboxOptions {
  session: string;
  storage: OutboxStorage;
  transport: Transport;
  timers: Timers;
  random: () => number;
  /** Makes a fresh command id: 1 to 64 characters of letters, digits, `_` and `-`. */
  newId: () => string;
  /**
   * Whether anything may be posted now: the device is online, the session's row has been seen, and the
   * connection is not in a state that refuses (unauthorized, gone, outdated). Deadlines run regardless.
   */
  canPost?(): boolean;
  backoff?: BackoffOptions;
  /** Entries an earlier outbox of this page held (a store that was stopped and started again). */
  initial?: OutboxEntry[];
  /** This page's claim owner (default: random). Two tabs must never share one. */
  owner?: string;
  /** The answer to a post that was out when this outbox closed: `settled` when it removed the entry. */
  onLate?(entry: OutboxEntry, settled: boolean): void;
  /** The `command.result` the session already holds for an id, if any: applied to the sends storage loads. */
  knownResult?(commandId: string): SendResultData | undefined;
  /** The `command.receipt` the session already holds for an id, if any: evidence that the hub took the send. */
  knownReceipt?(commandId: string): { status: string; reason?: string } | undefined;
  /**
   * What the hub lists, or null while it is not known (the stream is not live, or `/v1/status` not read yet).
   * Default: never known, which is the outbox as G11 merged it.
   */
  features?(): OutboxFeatures | null;
  /** Whether the session's row says `mode: keeper`: a post then asks for the keeper path. */
  keeperRow?(): boolean;
  /**
   * Asked right before every keeper-only post: whether the hub answering now still lists `require_keeper` (and is
   * the process the features were read from). False holds the send until the features are known again.
   */
  confirmKeeperOnly?(): Promise<boolean>;
  onChange(view: OutboxView): void;
}

/** Why a send waits while the database cannot record its claim. */
export const STORAGE_DOWN = "Not sent yet: this device's storage did not answer, and sending now could send it twice";

/** How many results for ids it does not hold yet the outbox keeps until its stored sends are loaded. */
export const EARLY_RESULTS_KEPT = 200;

export interface SendInput {
  text: string;
  attachments?: string[];
  mode?: "queue" | "steer";
}

export interface SendResultData {
  ok: boolean;
  note?: string;
  cancelled?: boolean;
}

type Outcome = "done" | "retry" | "paused" | "held";

const TERMINAL = new Set(["failed", "unconfirmed"]);


function errorText(response: JsonResponse): string {
  const body = response.body as { error?: { message?: unknown } } | undefined;
  const message = body?.error?.message;
  return typeof message === "string" && message !== "" ? message : `The hub answered ${response.status}`;
}

/** A claim owner for one page: random, never shared between tabs. */
export function randomOwner(): string {
  const bytes = new Uint8Array(8);
  const crypto = globalThis.crypto as Crypto | undefined;
  if (crypto?.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  return `p-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

/** A definite refusal: a 4xx that is not "not now" (408, 425, 429) or a credential to fix (401, 403). */
function refuses(response: JsonResponse | null): boolean {
  if (response === null || response.status < 400 || response.status >= 500) return false;
  return ![401, 403, 408, 425, 429].includes(response.status);
}

/** A 2xx answer that carries the stored result: it settles the send on its own. */
function carriesResult(response: JsonResponse | null): boolean {
  if (response === null || response.status < 200 || response.status >= 300) return false;
  const result = (response.body as { result?: unknown } | undefined)?.result;
  return result !== undefined && result !== null && typeof result === "object";
}

/** What a receipt makes of a send: `sent` on the keeper path, or `failed` with the hub's reason when refused. */
function receiptOutcome(entry: OutboxEntry, receipt: { receipt: string; reason?: string }, now: number): OutboxEntry {
  const { claim: _claim, error: _error, endedAt: _ended, lastError: _last, ambiguous: _ambiguous, ...rest } = entry;
  if (receipt.receipt === "rejected") {
    return { ...rest, status: "failed", error: receipt.reason !== undefined && receipt.reason !== "" ? receipt.reason : UNDELIVERED, endedAt: now };
  }
  return { ...rest, status: "sent", path: "keeper", receipt: receipt.receipt, sentAt: now };
}

/** The sends a lookup can settle: taken by the hub on the keeper path, or in doubt. */
function lookedUp(entry: OutboxEntry): boolean {
  if (entry.status === "failed") return entry.ambiguous === true;
  return (entry.status === "sent" || entry.status === "unconfirmed") && entry.path === "keeper";
}

/** A stored result, in the outbox's terms. */
function storedResult(result: Record<string, unknown>): SendResultData {
  return {
    ok: result.ok === true,
    ...(typeof result.note === "string" ? { note: result.note } : {}),
    ...(result.cancelled === true ? { cancelled: true } : {}),
  };
}

function errorCode(response: JsonResponse): string {
  const code = (response.body as { error?: { code?: unknown } } | undefined)?.error?.code;
  return typeof code === "string" ? code : "";
}


export class Outbox {
  private readonly options: OutboxOptions;
  private readonly backoff: BackoffOptions;
  private list: OutboxEntry[] = [];
  private flushing: Promise<void> | null = null;
  private again = false;
  private retryTimer: unknown = null;
  private ageTimer: unknown = null;
  private failures = 0;
  private paused: OutboxView["paused"] = null;
  private inFlight: string | null = null;
  /** A request is out (not just a claim being written): its answer is still written after `close()`. */
  private posting = false;
  private closed = false;
  private writing: Promise<unknown> = Promise.resolve();
  /** Sends whose first write has not finished: not deliverable yet, but not to be lost by a restart. */
  private unwritten: OutboxEntry[] = [];
  private readonly owner: string;
  /** Close the storage once the answer of the post that was out at `close()` is written. */
  private closeAfterLate = false;
  /** Set once the session is known not to exist (or the hub too old): entries loaded later end at once. */
  private gone: string | null = null;
  /**
   * `init()` has finished: storage is loaded and merged. Until then nothing is posted or aged, and results, late
   * answers and a gone session are held, so nothing can be written over what the load is about to read.
   */
  private ready = false;
  private early = new Map<string, SendResultData>();
  private earlyReceipts = new Map<string, { receipt: string; reason?: string }>();
  /** A receipt that came for the send this page is claiming or posting: the post's end settles against it. */
  private receiptInFlight: { commandId: string; receipt: string; reason?: string } | null = null;
  private lateWhileLoading = new Map<string, { entry: OutboxEntry; settled: boolean }>();
  private checking: Promise<void> | null = null;
  private checkAgain = false;
  /** Ids this outbox removed while loading (discarded, retried, settled): never taken back from the load or written again. */
  private removed = new Set<string>();
  /** Ids an earlier outbox of this page handed over. */
  private readonly carried: Set<string>;

  constructor(options: OutboxOptions) {
    this.options = options;
    this.owner = options.owner ?? randomOwner();
    this.backoff = options.backoff ?? DEFAULT_BACKOFF;
    this.list = (options.initial ?? []).map(normalizeEntry);
    this.carried = new Set(this.list.map((entry) => entry.commandId));
  }

  get view(): OutboxView {
    return {
      entries: this.list,
      persistent: this.options.storage.kind === "indexeddb",
      note: this.options.storage.note,
      paused: this.paused,
      inFlight: this.inFlight,
      owner: this.owner,
      loading: !this.ready,
      storageDown: this.options.storage.down === true,
    };
  }

  /**
   * Load what storage holds, merge what an earlier outbox of this page handed over, then age it and try to send
   * it. Where storage and the handed-over list both hold a send, the copy further along wins (`furtherAlong`), and
   * storage is corrected when it is the handed-over one. A handed-over entry storage lacks (its first write never
   * finished, or the storage is memory) is written once. Nothing removed meanwhile comes back.
   */
  async init(): Promise<void> {
    try {
      await this.load();
    } catch {
      // The storage failed while loading (only a storage that throws, not a fallback one): what memory holds stands.
    }
    if (this.closed) return;
    try {
      await this.applyHeld();
    } catch {
      // A storage that throws on writes too: what memory holds stands, and loading still ends.
    }
    if (this.closed) return;
    this.ready = true;
    this.removed.clear(); // only the merge reads it
    // A 404 (or an outdated hub) that came before the stored entries were loaded applies to them too.
    if (this.gone !== null) this.sessionGone(this.gone);
    this.changed();
    void this.flush();
    void this.recheck();
  }

  /** `init()`'s merge, then what was held while loading. */
  private async load(): Promise<void> {
    const loaded = await this.options.storage.load(this.options.session);
    if (this.closed) return;
    const stored = loaded.map(normalizeEntry).filter((entry) => !this.removed.has(entry.commandId));
    // Of two copies of one send, the one further along wins, wherever it is: storage can hold an older copy (a write
    // that failed and fell back to memory) as well as a newer one (an answer written after `stop()`). Ties keep the
    // stored copy. A copy only in memory (a handed-over one storage lacks, a send made while loading whose write the
    // load missed) is kept as it is.
    const memory = new Map(this.list.map((entry) => [entry.commandId, entry]));
    const toWrite: OutboxEntry[] = [];
    const merged = stored.map((copy) => {
      const mine = memory.get(copy.commandId);
      memory.delete(copy.commandId);
      if (mine === undefined || furtherAlong(mine, copy) <= 0) return copy;
      toWrite.push(mine); // storage learns it now, so the next start does not depend on what is handed over
      return mine;
    });
    for (const entry of memory.values()) if (this.carried.has(entry.commandId)) toWrite.push(entry); // a send made here wrote itself
    this.list = [...merged, ...memory.values()].sort((a, b) => a.createdAt - b.createdAt || a.commandId.localeCompare(b.commandId));
    for (const entry of toWrite) {
      // One the person removed meanwhile stays removed.
      if (this.removed.has(entry.commandId) || !this.list.includes(entry)) continue;
      // Compare and write: another page may have moved it on since the load. Its copy then stands, here too.
      const standing = await this.options.storage.reconcile(entry);
      if (this.closed) return;
      if (standing !== entry && this.list.includes(entry)) this.replace(standing);
    }
  }

  /**
   * Answers, results that came while loading, and results the session held already, before anything is posted:
   * a legacy send is never asked about again, so a result missed here would be lost.
   */
  private async applyHeld(): Promise<void> {
    for (const entry of [...this.list]) {
      const result = this.options.knownResult?.(entry.commandId);
      if (result !== undefined && !this.early.has(entry.commandId)) this.early.set(entry.commandId, result);
      const known = this.options.knownReceipt?.(entry.commandId);
      if (known !== undefined && !this.earlyReceipts.has(entry.commandId)) {
        this.earlyReceipts.set(entry.commandId, { receipt: known.status, ...(known.reason === undefined ? {} : { reason: known.reason }) });
      }
    }
    while (this.lateWhileLoading.size > 0 || this.earlyReceipts.size > 0 || this.early.size > 0) {
      for (const [id, { entry, settled }] of this.takeAll(this.lateWhileLoading)) {
        if (!this.list.some((e) => e.commandId === id)) continue;
        if (settled) await this.drop(id);
        else this.replace(entry);
      }
      // Receipts before results: a result then settles what a receipt made `sent`.
      for (const [id, held] of this.takeAll(this.earlyReceipts)) await this.received(id, held.receipt, held.reason);
      for (const [id, result] of this.takeAll(this.early)) {
        const entry = this.list.find((e) => e.commandId === id);
        if (entry !== undefined) await this.settle(entry, result);
      }
      if (this.closed) return;
    }
  }

  /** Everything not yet finished, the sends still being written included: what a restarted outbox starts from. */
  carry(): OutboxEntry[] {
    return [...this.list, ...this.unwritten];
  }

  close(): void {
    this.closed = true;
    this.clearRetry();
    this.clearAge();
    // A post on its way still gets its answer written (see `deliver`); the storage closes after that.
    if (!this.posting) this.options.storage.close();
    else this.closeAfterLate = true;
  }

  /** Put a send in the outbox. It has its id, and is on disk if there is a disk, when this resolves. */
  async send(input: SendInput): Promise<OutboxEntry> {
    const entry: OutboxEntry = {
      commandId: this.options.newId(),
      session: this.options.session,
      text: input.text,
      attachments: [...(input.attachments ?? [])],
      mode: input.mode ?? "queue",
      createdAt: this.options.timers.now(),
      status: "waiting",
      attempts: 0,
    };
    // Nothing is posted before it is on disk: a page that closes after the hub got the message but
    // before the write would lose the id that ties the hub's answer to it. Writes run in send order.
    this.unwritten = [...this.unwritten, entry];
    const written = this.writing.then(() => this.options.storage.put(entry));
    this.writing = written.catch(() => {});
    await written;
    this.unwritten = this.unwritten.filter((e) => e !== entry);
    // Closed meanwhile: `carry()` handed the entry to the outbox that replaces this one.
    if (this.closed) return entry;
    this.list = [...this.list, entry];
    this.changed();
    if (this.retryTimer === null) void this.flush();
    else this.scheduleAge();
    return entry;
  }

  /** The device is back, the page woke, the credential changed, or the session became known: try now. */
  wake(): void {
    this.paused = null;
    this.gone = null;
    this.clearRetry();
    this.failures = 0;
    this.changed();
    void this.flush();
  }


  /**
   * The `command.result` for a send came down the stream. An empty id changes nothing. While storage loads, every
   * result is kept and applied once loading ends (it may be a stored send's); after that, an unknown id changes nothing.
   */
  noteResult(commandId: string, result: SendResultData): void {
    if (commandId === "") return;
    if (this.ready) {
      const entry = this.list.find((e) => e.commandId === commandId);
      if (entry !== undefined) void this.settle(entry, result);
      return;
    }
    this.early.delete(commandId);
    if (this.early.size >= EARLY_RESULTS_KEPT) this.early.delete(this.early.keys().next().value as string);
    this.early.set(commandId, result);
  }

  /**
   * The `command.receipt` for a send came down the stream: the hub took it. A send this page still holds as
   * waiting, or ended as "may or may not have been sent", is `sent` on this evidence. Held, like a result, while
   * storage loads.
   */
  noteReceipt(commandId: string, receipt: string, reason?: string): void {
    if (commandId === "") return;
    if (this.inFlight === commandId) {
      // Its post is out (or its claim being written): kept, and applied however the post ends.
      this.receiptInFlight = { commandId, receipt, ...(reason === undefined ? {} : { reason }) };
      return;
    }
    if (!this.ready) {
      this.earlyReceipts.delete(commandId);
      if (this.earlyReceipts.size >= EARLY_RESULTS_KEPT) this.earlyReceipts.delete(this.earlyReceipts.keys().next().value as string);
      this.earlyReceipts.set(commandId, { receipt, ...(reason === undefined ? {} : { reason }) });
      return;
    }
    void this.received(commandId, receipt, reason);
  }

  private async received(commandId: string, receipt: string, reason?: string): Promise<void> {
    const entry = this.list.find((e) => e.commandId === commandId);
    if (entry === undefined || this.inFlight === commandId) return; // held by `noteReceipt`, applied when the post ends
    const doubt = entry.status === "failed" && entry.ambiguous === true;
    if (receipt === "rejected") {
      // The hub refused it, even after taking it (a keeper whose log write failed: `accepted`, then `not_recorded`).
      if (entry.status === "waiting" || doubt || entry.status === "sent") await this.byReceipt(entry, { receipt, ...(reason === undefined ? {} : { reason }) });
      return;
    }
    if (entry.status !== "waiting" && !doubt) return;
    await this.byReceipt(entry, { receipt });
  }


  /** The session no longer exists: nothing still open can be sent. */
  sessionGone(reason: string = SESSION_GONE): void {
    this.gone = reason;
    if (!this.ready) return; // applied once loading ends
    for (const entry of this.list) if (!TERMINAL.has(entry.status)) void this.fail(entry, reason);
  }

  /** Forget an entry (one the person dismissed, or no longer wants sent). */
  async discard(commandId: string): Promise<void> {
    await this.drop(commandId);
  }

  /** Send a failed or unconfirmed entry again as a new message. Undefined if there was none to retry. */
  async retry(commandId: string): Promise<OutboxEntry | undefined> {
    const old = this.list.find((e) => e.commandId === commandId && TERMINAL.has(e.status));
    if (old === undefined) return undefined;
    await this.drop(commandId);
    return this.send({ text: old.text, attachments: old.attachments, mode: old.mode });
  }

  /** Publish the view again (the storage's state may have changed). */
  refresh(): void {
    this.changed();
  }

  /** The hub's features became known, or changed: a send waiting on them may go, and lookups may run. */
  featuresChanged(): void {
    // A send waiting on its backoff (a keeper-only post the hub did not confirm, say) goes when that runs out.
    if (this.retryTimer === null) void this.flush();
    void this.recheck();
  }

  /** A catch-up's state frame arrived: a result it did not replay can be looked up now. */
  caughtUp(): void {
    void this.recheck();
  }

  // -- lookups (G31) ------------------------------------------------------

  /**
   * Ask the hub what became of every send it took (`sent` or `unconfirmed` on the keeper path) and every send in
   * doubt (`failed`, `ambiguous`), oldest first, one at a time, when it lists `command_lookup`. An answer is
   * applied as the stream's frame would be. A call while one runs is folded into it. Never a message post.
   */
  recheck(): Promise<void> {
    if (this.closed || !this.ready || this.options.features?.()?.lookup !== true) return Promise.resolve();
    if (this.checking !== null) {
      this.checkAgain = true;
      return this.checking;
    }
    this.checking = (async () => {
      try {
        do {
          this.checkAgain = false;
          await this.lookups();
        } while (this.checkAgain && !this.closed);
      } finally {
        this.checking = null;
      }
    })();
    return this.checking;
  }

  private async lookups(): Promise<void> {
    for (const { commandId } of [...this.list]) {
      if (this.closed || this.options.features?.()?.lookup !== true) return;
      const entry = this.list.find((e) => e.commandId === commandId);
      if (entry === undefined || !lookedUp(entry)) continue;
      let response: JsonResponse;
      try {
        response = await this.options.transport.json(sessionPath(entry.session, `commands/${encodeURIComponent(commandId)}`));
      } catch {
        continue; // asked again after the next catch-up
      }
      if (this.closed) return;
      if (response.status !== 200) continue; // 404: the store does not have it, which proves nothing
      const body = (response.body ?? {}) as { command_id?: unknown; receipt?: unknown; reason?: unknown; result?: unknown };
      if (body.command_id !== commandId) continue;
      const result = body.result;
      if (result !== undefined && result !== null && typeof result === "object") {
        const now = this.list.find((e) => e.commandId === commandId);
        if (now !== undefined) await this.settle(now, storedResult(result as Record<string, unknown>));
      } else if (typeof body.receipt === "string" && body.receipt !== "") {
        await this.received(commandId, body.receipt, typeof body.reason === "string" ? body.reason : undefined);
      }
    }
  }

  // -- delivery ---------------------------------------------------------

  /** Age what is held, then send what may go, oldest first. A second call while one runs is folded into it. */
  flush(): Promise<void> {
    if (this.closed) return Promise.resolve();
    if (this.flushing !== null) {
      this.again = true;
      return this.flushing;
    }
    this.flushing = (async () => {
      try {
        do {
          this.again = false;
          await this.pass();
          // A failed attempt has a retry waiting on its backoff: the next pass is that timer's.
        } while (this.again && !this.closed && this.retryTimer === null);
      } finally {
        this.flushing = null;
      }
    })();
    return this.flushing;
  }

  private canPost(): boolean {
    return this.paused === null && (this.options.canPost?.() ?? true);
  }

  private async pass(): Promise<void> {
    if (!this.ready) return; // `init()` runs the first pass
    await this.age();
    if (!this.canPost()) return;
    // Sends go in the order they were typed: one that cannot go holds the ones behind it.
    for (const entry of [...this.list]) {
      if (this.closed || !this.canPost()) return;
      const current = this.list.find((e) => e.commandId === entry.commandId);
      if (current === undefined || current.status !== "waiting") continue;
      const outcome = await this.deliver(current);
      if (outcome === "retry") return this.scheduleRetry();
      if (outcome === "paused" || outcome === "held") return;
    }
    this.failures = 0;
  }

  private async post(entry: OutboxEntry): Promise<JsonResponse | null> {
    this.inFlight = entry.commandId;
    this.posting = true;
    this.changed();
    try {
      return await this.options.transport.json(sessionPath(entry.session, "message"), {
        method: "POST",
        body: {
          text: entry.text,
          mode: entry.mode,
          attachments: entry.attachments,
          // One id both ways: a keeper-held session answers to `command_id`; the path for any other session
          // reads only `client_id`, which is also the id its `command.result` is filed under.
          command_id: entry.commandId,
          client_id: entry.commandId,
          // The keeper path or nothing (G31): set on every post of a send claimed as keeper-only.
          ...(isKeeperOnly(entry) ? { require_keeper: true } : {}),
        },
      });
    } catch {
      return null;
    } finally {
      this.inFlight = null;
      this.posting = false;
      this.changed();
    }
  }

  /** The entry is not going to be posted after all: it can be discarded again. A receipt held for it applies now. */
  private notPosting(): void {
    const id = this.inFlight;
    this.inFlight = null;
    this.changed();
    const held = id === null ? null : this.takeReceipt(id);
    const entry = id === null ? undefined : this.list.find((e) => e.commandId === id);
    if (held !== null && entry !== undefined && (entry.status === "waiting" || (entry.status === "failed" && entry.ambiguous === true))) {
      void this.byReceipt(entry, held);
    }
  }

  /** The entry, if it is still ours to update after an await: not closed, not settled or ended meanwhile. */
  private current(commandId: string): OutboxEntry | undefined {
    if (this.closed) return undefined;
    const entry = this.list.find((e) => e.commandId === commandId);
    return entry === undefined || TERMINAL.has(entry.status) ? undefined : entry;
  }

  /** `require_keeper` as the hub lists it: true, false, or null while not known. */
  private requireKeeper(): boolean | null {
    const features = this.options.features?.() ?? null;
    return features === null ? null : features.requireKeeper;
  }

  /**
   * A waiting entry's post: its first, again after an answer that said nothing was stored (429, 425, 409
   * `not_kept`), or again keeper-only after an answer that may mean it was applied (G31).
   */
  private async deliver(entry: OutboxEntry): Promise<Outcome> {
    // A keeper-only send in doubt is posted again only once the hub is known to list `require_keeper`: it waits,
    // unclaimed, until then (its deadline still runs).
    if (entry.ambiguous === true && isKeeperOnly(entry) && this.requireKeeper() === null) return "held";
    // Claim it first: the counted attempt and this page's claim are on disk before the request leaves, in one
    // transaction, so a reload or a second tab never makes a "first" post of something already posted.
    // It is in flight from here: the view offers no Discard while the claim is being written.
    this.inFlight = entry.commandId;
    this.changed();
    // Whether this post asks for the keeper path: recorded with the claim (an ambiguous entry keeps its own).
    const keeperOnly = this.requireKeeper() === true && this.options.keeperRow?.() === true;
    let claim: Claim;
    try {
      claim = await this.options.storage.claim(entry, this.owner, this.options.timers.now(), CLAIM_STALE_MS, keeperOnly);
    } catch (error) {
      this.notPosting();
      throw error;
    }
    // Another page saw it through: a receipt held for it describes a send storage no longer keeps, so it is dropped.
    if (claim.kind === "missing") this.takeReceipt(entry.commandId);
    if (claim.kind !== "claimed" || this.closed) this.notPosting();
    if (this.closed) return "done";
    if (claim.kind === "missing") {
      this.list = this.list.filter((e) => e.commandId !== entry.commandId);
      this.changed();
      return "done";
    }
    if (claim.kind === "settled") {
      this.adopt(claim.entry);
      return "done";
    }
    if (claim.kind === "unrecorded") {
      // The database did not take the claim. A send is posted only under a claim the database recorded, on every
      // row (a keeper row can stop being one before another page posts it again): not now. Shown, not written (a write would drop the claim the entry may hold, which other pages
      // rely on), and tried again; its deadline still ends it.
      const now = this.current(entry.commandId);
      if (now !== undefined) this.replace({ ...now, lastError: STORAGE_DOWN });
      return "retry";
    }
    if (claim.kind === "busy") {
      // Another tab is posting it: leave it alone, and look again once its claim would be stale.
      this.adopt(claim.entry);
      this.scheduleAge();
      return "held";
    }
    if (this.current(entry.commandId)?.status !== "waiting") {
      // Discarded, or ended, while the claim was being written: never post it. A discard's removal and an
      // end's write both come after the claim's transaction, so storage keeps no claim for it.
      this.notPosting();
      return "done";
    }
    const attempted = claim.entry;
    this.adopt(attempted);
    const before = this.takeReceipt(entry.commandId);
    if (before !== null) {
      // The hub has it already (another page's post): settled by the receipt, not posted.
      this.notPosting();
      await this.byReceipt(attempted, before);
      return "done";
    }
    const repeat = attempted.ambiguous === true;
    if (repeat && !(isKeeperOnly(attempted) && this.requireKeeper() === true)) {
      this.notPosting();
      if (isKeeperOnly(attempted) && this.requireKeeper() === null) {
        // Not known yet whether this hub takes `require_keeper`: the claim is released, and it waits.
        await this.save(attempted);
        return "held";
      }
      // An earlier post may have been applied, and a plain repeat could reach the route that does not
      // de-duplicate: the hub picks the route when the post arrives, and a session can leave its keeper.
      await this.fail(attempted, MAYBE_SENT);
      return "done";
    }
    if (isKeeperOnly(attempted) && this.options.confirmKeeperOnly !== undefined) {
      // A hub that restarted as an older version would ignore the field and could take the legacy route: the
      // hub that answers now is asked first. Its claim is released if it does not confirm, and the send waits.
      let confirmed = false;
      this.posting = true; // a close() meanwhile leaves the storage open for the release below
      try {
        confirmed = await this.options.confirmKeeperOnly();
      } catch {
        confirmed = false;
      } finally {
        this.posting = false;
      }
      if (this.closed) {
        // Nothing was posted: the claim is released, so the next outbox does not wait it out and find it in doubt.
        const { claim: _unposted, ...released } = attempted;
        try {
          await this.options.storage.reconcile(released);
        } finally {
          if (this.closeAfterLate) this.options.storage.close();
          this.options.onLate?.(released, false);
        }
        return "done";
      }
      if (!confirmed || this.current(entry.commandId)?.status !== "waiting") {
        this.notPosting();
        const still = this.current(entry.commandId);
        if (still?.status === "waiting") await this.save(still);
        // On the backoff: a hub whose status flaps must not be asked at round-trip speed.
        return "retry";
      }
    }
    const response = await this.post(attempted);
    const receipt = this.takeReceipt(entry.commandId);
    if (this.closed) {
      await this.late(attempted, response, receipt);
      return "done";
    }
    const now = this.current(entry.commandId);
    if (now === undefined) return "done"; // a result came first
    if (receipt !== null && !carriesResult(response) && (receipt.receipt === "rejected" || !refuses(response) || repeat)) {
      // The stream said the hub took it (or refused it) while the post was out: that settles it when the HTTP
      // answer leaves the outcome open (lost, 408, a 5xx), says "not now" (425, 429, 401, 403), or is a 2xx.
      // A definite refusal (another 4xx) keeps its own outcome below, except on a repeat, where a refusal says
      // nothing about the earlier post that the receipt is for.
      await this.byReceipt(now, receipt);
      return "done";
    }
    if (response === null) return this.unsure(now);
    const askedKeeper = isKeeperOnly(attempted);
    const { status } = response;
    if (status >= 200 && status < 300) {
      const body = (response.body ?? {}) as { receipt?: unknown; result?: SendResultData; reason?: unknown };
      if (body.result !== undefined && body.result !== null && typeof body.result === "object") {
        await this.settle(now, body.result);
        return "done";
      }
      if (body.receipt === "rejected") {
        // A keeper stored it as refused (a satellite's 429 backlog_full, answered again to a repeat): not taken.
        await this.fail(now, typeof body.reason === "string" && body.reason !== "" ? body.reason : UNDELIVERED);
        return "done";
      }
      const keeper = typeof body.receipt === "string";
      const sent: OutboxEntry = {
        ...now,
        status: "sent",
        path: keeper ? "keeper" : "legacy",
        sentAt: this.options.timers.now(),
        ...(keeper ? { receipt: body.receipt as string } : {}),
      };
      delete sent.ambiguous;
      delete sent.lastError;
      await this.save(sent);
      return "done";
    }
    if (status === 401 || status === 403) {
      // Refused before anything was applied: wait for a credential.
      this.paused = "unauthorized";
      await this.save({ ...now, lastError: "This device is not signed in to the hub" });
      return "paused";
    }
    if (status === 429 || status === 425) {
      // The hub said not now before it stored or queued anything: a repeat is safe on any path.
      await this.save({ ...now, lastError: errorText(response) });
      return "retry";
    }
    if (status === 408 || status >= 500) return this.unsure(now);
    if (status === 409 && errorCode(response) === "duplicate_client_id") {
      // The legacy path still holds a send under this id: an earlier post of ours got there. It is sent.
      await this.save({ ...now, status: "sent", path: "legacy", sentAt: this.options.timers.now() });
      return "done";
    }
    if (repeat) {
      // A refusal of a repeat says nothing about the earlier post: the routes refuse before they read the store.
      await this.fail(now, MAYBE_SENT);
      return "done";
    }
    if (askedKeeper && status === 409 && errorCode(response) === "not_kept") {
      // No keeper holds the session, and nothing was stored or queued: tried again, and the next claim reads the
      // row again (a plain post once the row no longer says keeper).
      await this.save({ ...now, lastError: NOT_KEPT });
      return "retry";
    }
    // The hub looked at it and said no: a repeat gets the same answer.
    await this.fail(now, status === 404 ? SESSION_GONE : errorText(response));
    return "done";
  }

  /**
   * The answer to a post that was out when the outbox closed (the store stopped). Nothing in this outbox's state
   * changes, but storage learns what the hub said, the claim is released, and `onLate` hands the entry to the
   * outbox that replaced this one, so the next start sees the truth at once instead of waiting out a stale claim.
   */
  private async late(
    attempted: OutboxEntry,
    response: JsonResponse | null,
    receipt: { receipt: string; reason?: string } | null = null,
  ): Promise<void> {
    const { claim: _released, ...entry } = attempted;
    // A keeper-only post's answer that says nothing was stored leaves it as it was (G31); for a plain post every
    // answer that is not 2xx reads as "may have been applied", as merged.
    const storedNothing =
      response !== null &&
      ([401, 403, 425, 429].includes(response.status) || (response.status === 409 && errorCode(response) === "not_kept"));
    let outcome: OutboxEntry | null = isKeeperOnly(entry) && storedNothing ? { ...entry } : { ...entry, ambiguous: true };
    const repeat = attempted.ambiguous === true;
    if (receipt !== null && !carriesResult(response) && (receipt.receipt === "rejected" || !refuses(response) || repeat)) {
      outcome = receiptOutcome(entry, receipt, this.options.timers.now());
    } else if (response !== null && response.status >= 200 && response.status < 300) {
      const body = (response.body ?? {}) as { receipt?: unknown; result?: SendResultData };
      const result = body.result;
      if (result !== undefined && result !== null && typeof result === "object" && (result.ok === true || result.cancelled === true)) {
        outcome = null;
      } else {
        const keeper = typeof body.receipt === "string";
        outcome = { ...entry, status: "sent", path: keeper ? "keeper" : "legacy", sentAt: this.options.timers.now() };
        if (keeper) outcome.receipt = body.receipt as string;
        delete outcome.ambiguous;
      }
    }
    let standing: OutboxEntry | null = outcome;
    try {
      if (outcome === null) await this.options.storage.remove(entry.commandId);
      else standing = await this.options.storage.reconcile(outcome);
    } finally {
      if (this.closeAfterLate) this.options.storage.close();
      this.options.onLate?.(standing ?? { ...entry, status: "sent" }, standing === null);
    }
  }

  /** The receipt held for this id while its post was out, if any; taken, so every exit of the post sees it once. */
  private takeReceipt(commandId: string): { receipt: string; reason?: string } | null {
    const held = this.receiptInFlight;
    if (held === null || held.commandId !== commandId) return null;
    this.receiptInFlight = null;
    return held;
  }

  /** A receipt settles the send: `sent` on the keeper path, or `failed` with the hub's reason when it was refused. */
  private async byReceipt(entry: OutboxEntry, receipt: { receipt: string; reason?: string }): Promise<void> {
    const outcome = receiptOutcome(entry, receipt, this.options.timers.now());
    if (outcome.status === "failed") await this.fail(entry, outcome.error ?? UNDELIVERED);
    else await this.save(outcome);
  }

  /** An entry the outbox that this one replaced finished posting after it closed. */
  adoptLate(entry: OutboxEntry, settled: boolean): void {
    if (this.closed) return;
    if (!this.ready) {
      this.lateWhileLoading.set(entry.commandId, { entry, settled });
      return;
    }
    if (!this.list.some((e) => e.commandId === entry.commandId)) return;
    if (settled) {
      void this.drop(entry.commandId);
      return;
    }
    this.replace(entry);
    this.scheduleAge();
    void this.flush();
  }

  /**
   * An answer that may mean the hub applied the send. A plain post's send ends there, and is never posted again.
   * A keeper-only one waits, `ambiguous`, to be posted again keeper-only under the same id on the backoff.
   */
  private async unsure(entry: OutboxEntry): Promise<Outcome> {
    if (isKeeperOnly(entry)) {
      await this.save({ ...entry, ambiguous: true, lastError: REPEATING });
      return "retry";
    }
    await this.fail(entry, MAYBE_SENT);
    return "done";
  }

  // -- bookkeeping --------------------------------------------------------

  /** A result for the entry: ok, or cancelled by a queue action, removes it; anything else fails it. */
  private async settle(entry: OutboxEntry, result: SendResultData): Promise<void> {
    if (result.ok === true || result.cancelled === true) {
      await this.drop(entry.commandId);
      return;
    }
    if (entry.status === "failed" && entry.ambiguous !== true) return; // the first definite failure stands
    await this.fail(entry, typeof result.note === "string" && result.note !== "" ? result.note : UNDELIVERED);
  }

  /** Every deadline, checked on every pass and on its own timer, whether or not anything can be posted. */
  private async age(): Promise<void> {
    const now = this.options.timers.now();
    for (const entry of [...this.list]) {
      if (this.closed) return;
      if (TERMINAL.has(entry.status)) {
        if (now - (entry.endedAt ?? entry.createdAt) >= TERMINAL_KEPT_MS) await this.drop(entry.commandId);
      } else if (entry.status === "waiting") {
        if (now - entry.createdAt >= OUTBOX_TTL_MS) await this.fail(entry, entry.ambiguous === true ? MAYBE_SENT : NOT_SENT_IN_TIME);
        else if (entry.claim !== undefined && now - entry.claim.at >= CLAIM_STALE_MS && this.inFlight !== entry.commandId) {
          // An abandoned claim is resolved here, whether or not this page may post: the entry becomes
          // `ambiguous` (its post may have reached the hub) and its own deadlines apply from now on.
          const resolved = await this.options.storage.abandon(entry, now, CLAIM_STALE_MS);
          if (this.closed) return;
          if (resolved === undefined) {
            this.list = this.list.filter((e) => e.commandId !== entry.commandId);
            this.changed();
          } else {
            this.adopt(resolved);
            // Its post may have reached the hub. A keeper-only one is posted again by the next pass (unless the hub
            // is known not to take `require_keeper`); any other is never posted again: it ends here.
            const standing = this.current(resolved.commandId);
            const again = standing !== undefined && isKeeperOnly(standing) && this.requireKeeper() !== false;
            if (standing?.status === "waiting" && standing.ambiguous === true && !again) await this.fail(standing, MAYBE_SENT);
          }
        }
      } else if (entry.path === "legacy") {
        if (now - (entry.sentAt ?? entry.createdAt) >= LEGACY_CONFIRM_MS) await this.end(entry, "unconfirmed", LEGACY_UNCONFIRMED);
      } else if (now - entry.createdAt >= OUTBOX_TTL_MS) {
        await this.end(entry, "unconfirmed", KEEPER_UNCONFIRMED);
      }
    }
    // A cap on what is kept for the person to see: the oldest terminal entries go first, never an open one.
    const terminal = this.list
      .filter((entry) => TERMINAL.has(entry.status))
      .sort((a, b) => (a.endedAt ?? a.createdAt) - (b.endedAt ?? b.createdAt));
    for (const entry of terminal.slice(0, Math.max(0, terminal.length - TERMINAL_KEPT_MAX))) await this.drop(entry.commandId);
    this.scheduleAge();
  }

  private deadline(entry: OutboxEntry): number {
    if (entry.status === "waiting" && entry.claim !== undefined) {
      // A claim: look again when it would be stale (or at the send's own deadline, if sooner).
      return Math.min(entry.claim.at + CLAIM_STALE_MS, entry.createdAt + OUTBOX_TTL_MS);
    }
    if (TERMINAL.has(entry.status)) return (entry.endedAt ?? entry.createdAt) + TERMINAL_KEPT_MS;
    if (entry.status === "sent" && entry.path === "legacy") return (entry.sentAt ?? entry.createdAt) + LEGACY_CONFIRM_MS;
    return entry.createdAt + OUTBOX_TTL_MS;
  }

  /**
   * Arm the timer for the next deadline strictly in the future. A deadline at or before now is one the pass that
   * just ran could not act on (a claim this page is posting, say): it is looked at again on the next pass, and
   * never re-armed at 0 ms, which would spin.
   */
  private scheduleAge(): void {
    this.clearAge();
    if (this.closed || this.list.length === 0) return;
    const now = this.options.timers.now();
    const future = this.list.map((entry) => this.deadline(entry)).filter((at) => at > now);
    if (future.length === 0) return;
    this.ageTimer = this.options.timers.setTimeout(() => {
      this.ageTimer = null;
      void this.flush();
    }, Math.min(...future) - now);
  }

  private clearAge(): void {
    if (this.ageTimer !== null) this.options.timers.clearTimeout(this.ageTimer);
    this.ageTimer = null;
  }

  /** Take the entry as storage holds it (another page may have moved it on). */
  private adopt(entry: OutboxEntry): void {
    if (this.list.some((e) => e.commandId === entry.commandId)) this.replace(entry);
  }

  private replace(entry: OutboxEntry): void {
    this.list = this.list.map((e) => (e.commandId === entry.commandId ? entry : e));
    this.changed();
  }

  private async fail(entry: OutboxEntry, error: string): Promise<void> {
    await this.end(entry, "failed", error);
  }

  private async end(entry: OutboxEntry, status: "failed" | "unconfirmed", error: string): Promise<void> {
    const ended: OutboxEntry = { ...entry, status, error, endedAt: this.options.timers.now() };
    delete ended.lastError;
    // "May or may not have been sent" is the one end evidence can still move on (see `furtherAlong`).
    if (error === MAYBE_SENT) ended.ambiguous = true;
    else delete ended.ambiguous;
    await this.save(ended);
    this.scheduleAge();
  }

  private async drop(commandId: string): Promise<void> {
    if (!this.ready) this.removed.add(commandId);
    this.list = this.list.filter((e) => e.commandId !== commandId);
    this.changed();
    await this.options.storage.remove(commandId);
    this.changed();
    this.scheduleAge();
  }

  /** Update an entry and write it, then publish the view again: a write that fails turns the storage into memory, and the view says so. */
  private async save(update: OutboxEntry): Promise<void> {
    if (!this.list.some((e) => e.commandId === update.commandId)) return; // settled meanwhile
    // Anything written after an answer releases this page's claim.
    const { claim: _released, ...entry } = update;
    this.replace(entry);
    // Compare and write: if another page moved it further along meanwhile, its copy stands, here too.
    const standing = await this.options.storage.reconcile(entry);
    if (standing !== entry && this.list.some((e) => e === entry)) this.replace(standing);
    this.changed();
    this.scheduleAge(); // its deadline may have changed with its status
  }

  private scheduleRetry(): void {
    if (this.closed) return;
    this.failures++;
    this.clearRetry();
    const delay = backoffDelay(this.failures, this.options.random, this.backoff);
    this.retryTimer = this.options.timers.setTimeout(() => {
      this.retryTimer = null;
      void this.flush();
    }, delay);
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) this.options.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  /** Empty a map and hand back what it held. */
  private takeAll<V>(map: Map<string, V>): [string, V][] {
    const all = [...map];
    map.clear();
    return all;
  }

  private changed(): void {
    if (!this.closed) this.options.onChange(this.view);
  }
}
