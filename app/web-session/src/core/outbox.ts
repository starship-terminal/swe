// The outbox: sends the person typed, kept until the hub has them.
//
// Only sends go through here. Approvals, Stop and Restore need a live
// connection and are refused while there is none (see the store): a late
// "yes" to a request that has moved on is worse than a refusal.
//
// A send gets its `command_id` once, when it is typed, and keeps it through
// every retry, so the hub applies it once: a retry answers the stored receipt
// (`duplicate`) instead of sending the message again. An entry is written to
// IndexedDB before the first attempt (so a reload cannot lose it), retried with
// backoff until a receipt arrives, then kept as `sent` until its
// `command.result` shows up. The hub can lose a result when it restarts, so
// after a restart (or a reload) `sent` entries are asked about again under the
// same id, and the answer carries the stored result.
import { backoffDelay, DEFAULT_BACKOFF, type BackoffOptions, type Timers } from "./supervisor";
import type { OutboxEntry, OutboxStorage } from "./outbox-storage";
import { sessionPath, type JsonResponse, type Transport } from "./transport";

/** How long the hub holds a message for a sleeping machine, and so how long a send is worth retrying. */
export const OUTBOX_TTL_MS = 24 * 60 * 60 * 1000;

/** Why a send ended without an answer on a path that cannot say whether it arrived. */
export const MAYBE_SENT =
  "The connection dropped before the hub answered. This message may or may not have been sent: check the session, and send it again if it did not arrive.";

export interface OutboxView {
  entries: OutboxEntry[];
  /** Whether sends survive a reload (IndexedDB is in use). */
  persistent: boolean;
  /** Why they do not, when they do not. */
  note: string;
  /** Set while the hub refuses the credential: nothing is sent until `wake()`. */
  paused: "unauthorized" | null;
}

export interface OutboxOptions {
  session: string;
  storage: OutboxStorage;
  transport: Transport;
  timers: Timers;
  random: () => number;
  /**
   * Which path a send takes right now: `keeper` for a session a keeper holds (the hub stores a receipt and a
   * result under the command id, and a repeat is answered with them), `legacy` for any other. On the legacy path
   * the hub does not de-duplicate, so a send is never repeated: it settles on its 2xx response, and one that gets
   * no answer fails, for the person to send again.
   */
  route(): "keeper" | "legacy";
  /**
   * Whether a send may be posted now: the device is online and the session's row has been seen, so `route()` is a
   * fact and not a guess. Nothing is posted before that (the entry waits, on disk, until `wake()`).
   */
  ready?(): boolean;
  /** Makes a fresh command id: 1 to 64 characters of letters, digits, `_` and `-`. */
  newId: () => string;
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

type Outcome = "done" | "retry" | "paused";

function errorText(response: JsonResponse): string {
  const body = response.body as { error?: { message?: unknown } } | undefined;
  const message = body?.error?.message;
  return typeof message === "string" && message !== "" ? message : `HTTP ${response.status}`;
}

export class Outbox {
  private readonly options: OutboxOptions;
  private readonly backoff: BackoffOptions;
  private list: OutboxEntry[] = [];
  private recheck = new Set<string>();
  private flushing: Promise<void> | null = null;
  private again = false;
  private retryTimer: unknown = null;
  private failures = 0;
  private paused: OutboxView["paused"] = null;
  private closed = false;
  private writing: Promise<unknown> = Promise.resolve();
  /** Sends whose first write has not finished: not deliverable yet, but not to be lost by a restart. */
  private unwritten: OutboxEntry[] = [];

  constructor(options: OutboxOptions) {
    this.options = options;
    this.list = [...(options.initial ?? [])];
    this.backoff = options.backoff ?? DEFAULT_BACKOFF;
  }

  get view(): OutboxView {
    return {
      entries: this.list,
      persistent: this.options.storage.kind === "indexeddb",
      note: this.options.storage.note,
      paused: this.paused,
    };
  }

  /** Load what an earlier page left, then try to send it. */
  async init(): Promise<void> {
    const stored = await this.options.storage.load(this.options.session);
    if (this.closed) return;
    const known = new Set(this.list.map((entry) => entry.commandId));
    this.list = [...this.list, ...stored.filter((entry) => !known.has(entry.commandId))].sort(
      (a, b) => a.createdAt - b.createdAt || a.commandId.localeCompare(b.commandId),
    );
    // What an earlier outbox handed over is written again (once is enough for the storage, twice is harmless).
    for (const entry of this.list) await this.options.storage.put(entry);
    // The hub may have lost what it owed us while this page was away: ask again.
    for (const entry of this.list) if (entry.status === "sent") this.recheck.add(entry.commandId);
    this.changed();
    void this.flush();
  }

  /** Everything not yet delivered, the sends still being written included: what a restarted outbox starts from. */
  carry(): OutboxEntry[] {
    return [...this.list, ...this.unwritten];
  }

  close(): void {
    this.closed = true;
    this.clearRetry();
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
      status: "pending",
      attempts: 0,
    };
    // Nothing is sent before it is on disk: a page that closes after the hub got the message
    // but before the write would lose the id a retry needs. Writes run in the order of the sends.
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
    return entry;
  }

  /** The device is back, the page woke, or the credential changed: try now. */
  wake(): void {
    this.paused = null;
    this.clearRetry();
    this.failures = 0;
    this.changed();
    void this.flush();
  }

  /**
   * Ask again about every send the hub acknowledged, under the same ids. The hub answers a repeat with the
   * stored receipt and result. Called after a restart, and after every reconnect: a catch-up by snapshot or
   * overflow recovery carries no `command.result`, and a failed one the hub made for the stream has `seq` 0
   * and is not replayed, so a result could have been missed.
   */
  recheckSent(): void {
    for (const entry of this.list) if (entry.status === "sent") this.recheck.add(entry.commandId);
    void this.flush();
  }

  /** The `command.result` for a send came down the stream. */
  noteResult(commandId: string, result: { ok: boolean; note?: string; cancelled?: boolean }): void {
    const entry = this.list.find((e) => e.commandId === commandId);
    if (entry === undefined || entry.status === "failed") return;
    this.recheck.delete(commandId);
    if (result.ok || result.cancelled) void this.drop(commandId);
    else void this.fail(entry, result.note || "The hub could not deliver this message");
  }

  /** Forget an entry (a failed one the person dismissed, or one they no longer want sent). */
  async discard(commandId: string): Promise<void> {
    await this.drop(commandId);
  }

  /** Send a failed entry again as a new message. Resolves with the new entry, or undefined if there was none to retry. */
  async retry(commandId: string): Promise<OutboxEntry | undefined> {
    const old = this.list.find((e) => e.commandId === commandId && e.status === "failed");
    if (old === undefined) return undefined;
    await this.drop(commandId);
    return this.send({ text: old.text, attachments: old.attachments, mode: old.mode });
  }

  // -- delivery ---------------------------------------------------------

  /** Send what is waiting, oldest first. A second call while one runs is folded into it. */
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

  private async pass(): Promise<void> {
    if (this.paused !== null) return;
    if (this.options.ready !== undefined && !this.options.ready()) return;
    const now = this.options.timers.now();
    for (const entry of [...this.list]) {
      if (this.closed) return;
      if (now - entry.createdAt > OUTBOX_TTL_MS) {
        if (entry.status === "pending") await this.fail(entry, "Not sent: the hub did not answer for 24 hours");
        else if (entry.status === "sent") {
          await this.fail(entry, "No result from the hub in 24 hours: this message may not have been delivered");
        }
      }
    }
    // Sends go in the order they were typed: one that cannot go holds the ones behind it.
    for (const entry of [...this.list]) {
      if (this.closed) return;
      if (entry.status !== "pending") continue;
      const outcome = await this.post(entry);
      if (outcome === "retry") return this.scheduleRetry();
      if (outcome === "paused") return;
    }
    for (const id of [...this.recheck]) {
      const entry = this.list.find((e) => e.commandId === id);
      this.recheck.delete(id);
      if (entry === undefined || entry.status !== "sent") continue;
      // Only what the hub has proven it stores under this id, on a session a keeper still holds.
      if (entry.path !== "keeper" || this.options.route() !== "keeper") continue;
      const outcome = await this.post(entry);
      if (outcome === "retry") {
        this.recheck.add(id);
        return this.scheduleRetry();
      }
      if (outcome === "paused") {
        this.recheck.add(id); // asked about again after sign-in
        return;
      }
    }
    this.failures = 0;
  }

  private async post(entry: OutboxEntry): Promise<Outcome> {
    const route = this.options.route();
    if (entry.status === "pending" && entry.attempts > 0 && route !== "keeper") {
      // An earlier attempt may have reached the hub on a path that stores the id, and this one would not be
      // recognised as the same message: never repeat a send there.
      await this.fail(entry, MAYBE_SENT);
      return "done";
    }
    const attempt: OutboxEntry = { ...entry, attempts: entry.attempts + 1 };
    this.replace(attempt);
    let response: JsonResponse;
    try {
      response = await this.options.transport.json(sessionPath(entry.session, "message"), {
        method: "POST",
        body: {
          text: entry.text,
          mode: entry.mode,
          attachments: entry.attachments,
          // The same id both ways: a keeper-held session answers to `command_id`, and the path for any other
          // session reads only `client_id`, which is also what its `command.result` is filed under.
          command_id: entry.commandId,
          client_id: entry.commandId,
        },
      });
    } catch {
      // No answer at all. Where the hub stores the id a repeat is safe; elsewhere it could type the message twice.
      if (route === "keeper") return "retry";
      await this.fail(attempt, MAYBE_SENT);
      return "done";
    }
    if (this.closed || !this.list.some((e) => e.commandId === entry.commandId)) {
      // Closed, or the result came down the stream first and the entry is gone.
      return "done";
    }
    if (this.list.find((e) => e.commandId === entry.commandId)?.status === "failed") {
      // A failed result came down the stream before this receipt: it stays failed.
      return "done";
    }
    const { status } = response;
    if (status >= 200 && status < 300) {
      const body = (response.body ?? {}) as { receipt?: unknown; result?: { ok?: unknown; cancelled?: unknown; note?: unknown } };
      const result = body.result;
      if (result !== undefined && result !== null && typeof result === "object") {
        if (result.ok === true || result.cancelled === true) {
          await this.drop(entry.commandId);
        } else {
          await this.fail(attempt, typeof result.note === "string" && result.note !== "" ? result.note : "The hub could not deliver this message");
        }
        return "done";
      }
      if (route === "keeper" && typeof body.receipt === "string") {
        // The hub stored a receipt under this id: it will answer a repeat with it, so asking again is safe.
        const sent: OutboxEntry = { ...attempt, status: "sent", receipt: body.receipt, path: "keeper" };
        delete sent.error;
        this.replace(sent);
        await this.save(sent);
        return "done";
      }
      // Any other path does not de-duplicate and files its result under the id it was given only when
      // it is delivered: the answer is all there is, and the message is never sent again.
      await this.drop(entry.commandId);
      return "done";
    }
    if (status === 401 || status === 403) {
      this.paused = "unauthorized";
      this.changed();
      return "paused";
    }
    const unsure = status === 408 || status === 500 || status === 502 || status === 504;
    if (route !== "keeper" && unsure) {
      await this.fail(attempt, MAYBE_SENT);
      return "done";
    }
    if (status === 408 || status === 425 || status === 429 || status >= 500) {
      await this.save(this.list.find((e) => e.commandId === entry.commandId) ?? attempt);
      return "retry";
    }
    // The hub looked at it and said no: retrying the same id gets the same answer.
    await this.fail(attempt, status === 404 ? "This session no longer exists" : errorText(response));
    return "done";
  }

  // -- bookkeeping --------------------------------------------------------

  private replace(entry: OutboxEntry): void {
    this.list = this.list.map((e) => (e.commandId === entry.commandId ? entry : e));
    this.changed();
  }

  private async fail(entry: OutboxEntry, error: string): Promise<void> {
    const failed: OutboxEntry = { ...entry, status: "failed", error };
    this.recheck.delete(entry.commandId);
    this.replace(failed);
    await this.save(failed);
  }

  private async drop(commandId: string): Promise<void> {
    this.recheck.delete(commandId);
    this.list = this.list.filter((e) => e.commandId !== commandId);
    this.changed();
    await this.options.storage.remove(commandId);
    this.changed();
  }

  /** Write an entry, then publish the view again: a write that fails turns the storage into memory, and the view says so. */
  private async save(entry: OutboxEntry): Promise<void> {
    await this.options.storage.put(entry);
    this.changed();
  }

  /** Publish the view again (the storage's state may have changed). */
  refresh(): void {
    this.changed();
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
