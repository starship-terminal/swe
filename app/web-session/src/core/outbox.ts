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
// - A post is repeated only where the hub de-duplicates by id: the entry's
//   path is `keeper`, or it has none yet and the row says a keeper holds the
//   session, and the row still says so at the moment of the repeat. Anywhere
//   else an answer that may mean "applied" (none at all, 408, a 5xx) ends the
//   send as failed, "may or may not have been sent".
// - Deadlines run on their own timer, whether or not anything can be posted.
import { backoffDelay, DEFAULT_BACKOFF, type BackoffOptions, type Timers } from "./supervisor";
import { CLAIM_STALE_MS, type Claim, type OutboxEntry, type OutboxStorage } from "./outbox-storage";
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
  "The hub took this message but no result came in 24 hours: check the session, and send it again if it is not there.";
export const SESSION_GONE = "Not sent: this session no longer exists.";
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
  /** Whether the row says a keeper holds the session (`row.mode` is `keeper`): where a repeat is safe. */
  keeperHeld(): boolean;
  backoff?: BackoffOptions;
  /** Entries an earlier outbox of this page held (a store that was stopped and started again). */
  initial?: OutboxEntry[];
  /** This page's claim owner (default: random). Two tabs must never share one. */
  owner?: string;
  /** The answer to a post that was out when this outbox closed: `settled` when it removed the entry. */
  onLate?(entry: OutboxEntry, settled: boolean): void;
  /** The `command.result` the session already holds for an id, if any: applied to the sends storage loads. */
  knownResult?(commandId: string): SendResultData | undefined;
  onChange(view: OutboxView): void;
}

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

function errorCode(response: JsonResponse): string {
  const code = (response.body as { error?: { code?: unknown } } | undefined)?.error?.code;
  return typeof code === "string" ? code : "";
}

/** An entry stored by an earlier build of this file, where `waiting` was called `pending`. */
function normalize(entry: OutboxEntry): OutboxEntry {
  return (entry.status as string) === "pending" ? { ...entry, status: "waiting" } : entry;
}

export class Outbox {
  private readonly options: OutboxOptions;
  private readonly backoff: BackoffOptions;
  private list: OutboxEntry[] = [];
  private recheck = new Set<string>();
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
  /** Storage has been loaded: until then a result for an id not in the list may be for a stored send. */
  private loaded = false;
  private early = new Map<string, SendResultData>();

  constructor(options: OutboxOptions) {
    this.options = options;
    this.owner = options.owner ?? randomOwner();
    this.backoff = options.backoff ?? DEFAULT_BACKOFF;
    this.list = (options.initial ?? []).map(normalize);
  }

  get view(): OutboxView {
    return {
      entries: this.list,
      persistent: this.options.storage.kind === "indexeddb",
      note: this.options.storage.note,
      paused: this.paused,
      inFlight: this.inFlight,
      owner: this.owner,
    };
  }

  /** Load what an earlier page left, then age it and try to send it. */
  async init(): Promise<void> {
    // What an earlier outbox handed over is written again (once is enough for the storage, twice is harmless).
    for (const entry of this.list) await this.options.storage.put(entry);
    const stored = await this.options.storage.load(this.options.session);
    if (this.closed) return;
    const known = new Set(this.list.map((entry) => entry.commandId));
    this.list = [...this.list, ...stored.filter((entry) => !known.has(entry.commandId)).map(normalize)].sort(
      (a, b) => a.createdAt - b.createdAt || a.commandId.localeCompare(b.commandId),
    );
    this.loaded = true;
    // Results that came while storage was loading, and those the session held already, settle what was loaded,
    // before anything is posted: a legacy send is never asked about again, so a result missed here would be lost.
    const early = this.early;
    this.early = new Map();
    for (const entry of [...this.list]) {
      const result = early.get(entry.commandId) ?? this.options.knownResult?.(entry.commandId);
      if (result !== undefined) await this.settle(entry, result);
      if (this.closed) return;
    }
    // The hub may have lost what it owed us while this page was away: ask again where that is safe.
    for (const entry of this.list) if (entry.status === "sent" && entry.path === "keeper") this.recheck.add(entry.commandId);
    // A 404 (or an outdated hub) that came before the stored entries were loaded applies to them too.
    if (this.gone !== null) this.sessionGone(this.gone);
    this.changed();
    void this.flush();
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
   * A catch-up just ended: ask again, under the same ids, about every send the keeper path took. The hub
   * answers a repeat with the stored receipt and result. A catch-up by snapshot or overflow recovery carries
   * no `command.result`, and a failed one the hub made for the stream has `seq` 0 and is not replayed, so a
   * result could have been missed. Legacy sends are never asked about: that path does not de-duplicate.
   */
  recheckSent(): void {
    for (const entry of this.list) if (entry.status === "sent" && entry.path === "keeper") this.recheck.add(entry.commandId);
    void this.flush();
  }

  /**
   * The `command.result` for a send came down the stream. An empty id changes nothing; an unknown one is kept
   * until storage is loaded (it may be a stored send's), and changes nothing after that.
   */
  noteResult(commandId: string, result: SendResultData): void {
    if (commandId === "") return;
    const entry = this.list.find((e) => e.commandId === commandId);
    if (entry !== undefined) {
      void this.settle(entry, result);
      return;
    }
    if (this.loaded) return;
    this.early.delete(commandId);
    if (this.early.size >= EARLY_RESULTS_KEPT) this.early.delete(this.early.keys().next().value as string);
    this.early.set(commandId, result);
  }

  /** The session no longer exists: nothing still open can be sent. */
  sessionGone(reason: string = SESSION_GONE): void {
    this.gone = reason;
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
    for (const id of [...this.recheck]) {
      if (this.closed || !this.canPost()) return;
      this.recheck.delete(id);
      const entry = this.list.find((e) => e.commandId === id);
      if (entry === undefined || entry.status !== "sent" || entry.path !== "keeper") continue;
      // The row must still say a keeper holds the session: a detached session's path no longer de-duplicates.
      if (!this.options.keeperHeld()) continue;
      const outcome = await this.ask(entry);
      if (outcome === "paused") {
        this.recheck.add(id); // asked about again after sign-in
        return;
      }
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

  /** The entry is not going to be posted after all: it can be discarded again. */
  private notPosting(): void {
    this.inFlight = null;
    this.changed();
  }

  /** The entry, if it is still ours to update after an await: not closed, not settled or ended meanwhile. */
  private current(commandId: string): OutboxEntry | undefined {
    if (this.closed) return undefined;
    const entry = this.list.find((e) => e.commandId === commandId);
    return entry === undefined || TERMINAL.has(entry.status) ? undefined : entry;
  }

  /** A waiting entry's post: its first, or a repeat where repeating is safe. */
  private async deliver(entry: OutboxEntry): Promise<Outcome> {
    // Claim it first: the counted attempt and this page's claim are on disk before the request leaves, in one
    // transaction, so a reload or a second tab never makes a "first" post of something already posted.
    // It is in flight from here: the view offers no Discard while the claim is being written.
    this.inFlight = entry.commandId;
    this.changed();
    let claim: Claim;
    try {
      claim = await this.options.storage.claim(entry, this.owner, this.options.timers.now(), CLAIM_STALE_MS);
    } catch (error) {
      this.notPosting();
      throw error;
    }
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
    if (attempted.ambiguous && !this.options.keeperHeld()) {
      this.notPosting();
      // An earlier post may have been applied, and here a repeat would not be recognised as the same message.
      await this.fail(attempted, MAYBE_SENT);
      return "done";
    }
    const response = await this.post(attempted);
    if (this.closed) {
      await this.late(attempted, response);
      return "done";
    }
    const now = this.current(entry.commandId);
    if (now === undefined) return "done"; // a result came first
    if (response === null) return this.unsure(now, "The hub did not answer");
    const { status } = response;
    if (status >= 200 && status < 300) {
      const body = (response.body ?? {}) as { receipt?: unknown; result?: SendResultData };
      if (body.result !== undefined && body.result !== null && typeof body.result === "object") {
        await this.settle(now, body.result);
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
    if (status === 408 || status >= 500) return this.unsure(now, errorText(response));
    if (status === 409 && errorCode(response) === "duplicate_client_id") {
      // The legacy path still holds a send under this id: an earlier post of ours got there. It is sent.
      await this.save({ ...now, status: "sent", path: "legacy", sentAt: this.options.timers.now() });
      return "done";
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
  private async late(attempted: OutboxEntry, response: JsonResponse | null): Promise<void> {
    const { claim: _released, ...entry } = attempted;
    let outcome: OutboxEntry | null = { ...entry, ambiguous: true };
    if (response !== null && response.status >= 200 && response.status < 300) {
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
    try {
      if (outcome === null) await this.options.storage.remove(entry.commandId);
      else await this.options.storage.put(outcome);
    } finally {
      if (this.closeAfterLate) this.options.storage.close();
      this.options.onLate?.(outcome ?? { ...entry, status: "sent" }, outcome === null);
    }
  }

  /** An entry the outbox that this one replaced finished posting after it closed. */
  adoptLate(entry: OutboxEntry, settled: boolean): void {
    if (this.closed || !this.list.some((e) => e.commandId === entry.commandId)) return;
    if (settled) {
      void this.drop(entry.commandId);
      return;
    }
    this.replace(entry);
    this.scheduleAge();
    void this.flush();
  }

  /** An answer that may mean the hub applied the send: repeat it only where the hub de-duplicates. */
  private async unsure(entry: OutboxEntry, why: string): Promise<Outcome> {
    if (!this.options.keeperHeld()) {
      await this.fail(entry, MAYBE_SENT);
      return "done";
    }
    await this.save({ ...entry, ambiguous: true, lastError: why });
    return "retry";
  }

  /** A keeper-path send asked about again under its own id. It stays `sent` unless the answer settles it. */
  private async ask(entry: OutboxEntry): Promise<Outcome> {
    const response = await this.post(entry);
    const now = this.current(entry.commandId);
    if (now === undefined || response === null) return "done"; // asked again at the next catch-up
    if (response.status >= 200 && response.status < 300) {
      const body = (response.body ?? {}) as { result?: SendResultData };
      if (body.result !== undefined && body.result !== null && typeof body.result === "object") {
        await this.settle(now, body.result);
      }
      return "done";
    }
    if (response.status === 401 || response.status === 403) {
      this.paused = "unauthorized";
      this.changed();
      return "paused";
    }
    if (response.status === 404) await this.fail(now, SESSION_GONE);
    return "done";
  }

  // -- bookkeeping --------------------------------------------------------

  /** A result for the entry: ok, or cancelled by a queue action, removes it; anything else fails it. */
  private async settle(entry: OutboxEntry, result: SendResultData): Promise<void> {
    if (result.ok === true || result.cancelled === true) {
      await this.drop(entry.commandId);
      return;
    }
    if (entry.status === "failed") return; // the first failure stands
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
        if (now - entry.createdAt >= OUTBOX_TTL_MS) await this.fail(entry, NOT_SENT_IN_TIME);
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
    this.recheck.delete(entry.commandId);
    await this.save(ended);
    this.scheduleAge();
  }

  private async drop(commandId: string): Promise<void> {
    this.recheck.delete(commandId);
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
    await this.options.storage.put(entry);
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

  private changed(): void {
    if (!this.closed) this.options.onChange(this.view);
  }
}
