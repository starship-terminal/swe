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
import type { OutboxEntry, OutboxStorage } from "./outbox-storage";
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
  /** The entry whose post is on its way, if any. */
  inFlight: string | null;
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
  onChange(view: OutboxView): void;
}

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

type Outcome = "done" | "retry" | "paused";

const TERMINAL = new Set(["failed", "unconfirmed"]);

function errorText(response: JsonResponse): string {
  const body = response.body as { error?: { message?: unknown } } | undefined;
  const message = body?.error?.message;
  return typeof message === "string" && message !== "" ? message : `The hub answered ${response.status}`;
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
  private closed = false;
  private writing: Promise<unknown> = Promise.resolve();
  /** Sends whose first write has not finished: not deliverable yet, but not to be lost by a restart. */
  private unwritten: OutboxEntry[] = [];

  constructor(options: OutboxOptions) {
    this.options = options;
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
    // The hub may have lost what it owed us while this page was away: ask again where that is safe.
    for (const entry of this.list) if (entry.status === "sent" && entry.path === "keeper") this.recheck.add(entry.commandId);
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
    this.options.storage.close();
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

  /** The `command.result` for a send came down the stream. An empty or unknown id changes nothing. */
  noteResult(commandId: string, result: SendResultData): void {
    if (commandId === "") return;
    const entry = this.list.find((e) => e.commandId === commandId);
    if (entry === undefined) return;
    void this.settle(entry, result);
  }

  /** The session no longer exists: nothing still open can be sent. */
  sessionGone(reason: string = SESSION_GONE): void {
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
      if (outcome === "paused") return;
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
      this.changed();
    }
  }

  /** The entry, if it is still ours to update after an await: not closed, not settled or ended meanwhile. */
  private current(commandId: string): OutboxEntry | undefined {
    if (this.closed) return undefined;
    const entry = this.list.find((e) => e.commandId === commandId);
    return entry === undefined || TERMINAL.has(entry.status) ? undefined : entry;
  }

  /** A waiting entry's post: its first, or a repeat where repeating is safe. */
  private async deliver(entry: OutboxEntry): Promise<Outcome> {
    if (entry.ambiguous && !this.options.keeperHeld()) {
      // The last attempt may have been applied, and here a repeat would not be recognised as the same message.
      await this.fail(entry, MAYBE_SENT);
      return "done";
    }
    const attempted: OutboxEntry = { ...entry, attempts: entry.attempts + 1 };
    this.replace(attempted);
    const response = await this.post(attempted);
    const now = this.current(entry.commandId);
    if (now === undefined) return "done"; // a result came first, or the outbox closed
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
    if (status === 429 || status === 425 || status === 503) {
      // The hub says not now, before it queued anything: a repeat is safe on any path.
      await this.save({ ...now, lastError: errorText(response) });
      return "retry";
    }
    if (status === 408 || status >= 500) return this.unsure(now, errorText(response));
    // The hub looked at it and said no: a repeat gets the same answer.
    await this.fail(now, status === 404 ? SESSION_GONE : errorText(response));
    return "done";
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
      } else if (entry.path === "legacy") {
        if (now - (entry.sentAt ?? entry.createdAt) >= LEGACY_CONFIRM_MS) await this.end(entry, "unconfirmed", LEGACY_UNCONFIRMED);
      } else if (now - entry.createdAt >= OUTBOX_TTL_MS) {
        await this.end(entry, "unconfirmed", KEEPER_UNCONFIRMED);
      }
    }
    this.scheduleAge();
  }

  private deadline(entry: OutboxEntry): number {
    if (TERMINAL.has(entry.status)) return (entry.endedAt ?? entry.createdAt) + TERMINAL_KEPT_MS;
    if (entry.status === "sent" && entry.path === "legacy") return (entry.sentAt ?? entry.createdAt) + LEGACY_CONFIRM_MS;
    return entry.createdAt + OUTBOX_TTL_MS;
  }

  private scheduleAge(): void {
    this.clearAge();
    if (this.closed || this.list.length === 0) return;
    const next = Math.min(...this.list.map((entry) => this.deadline(entry)));
    this.ageTimer = this.options.timers.setTimeout(
      () => {
        this.ageTimer = null;
        void this.flush();
      },
      Math.max(0, next - this.options.timers.now()),
    );
  }

  private clearAge(): void {
    if (this.ageTimer !== null) this.options.timers.clearTimeout(this.ageTimer);
    this.ageTimer = null;
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
  private async save(entry: OutboxEntry): Promise<void> {
    if (!this.list.some((e) => e.commandId === entry.commandId)) return; // settled meanwhile
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
