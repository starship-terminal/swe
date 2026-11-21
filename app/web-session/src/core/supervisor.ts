// The connection supervisor: keeps one session's v2 stream open.
//
// States:
//
//   connecting    opening the first stream, or still failing for the first time
//   live          a frame has arrived on the open stream
//   reconnecting  the stream dropped (or went silent); trying again
//   offline       the device has no network; waits for it, no timer runs
//   hub_down      the network is up and the hub does not answer (three failures in a row)
//   unauthorized  the hub answered 401 or 403; waits for `wake()` (a new credential)
//   gone          the hub answered 404 or 410: the session does not exist
//   outdated      the hub does not speak protocol 2 (a v1 frame came, or 426)
//
// A stream that sends nothing for 25 s (the hub pings every 10 s) is dead
// and is closed. A new connection asks with `last=<cursor>&epoch=<epoch>`.
// Retries back off with jitter; a stream that stays open resets the count.
//
// Nothing here reads the clock, the random source or the network itself:
// timers, randomness, the transport and the environment (visibility, network
// events) are all injected, so a test moves time by hand and never waits.
import { PROTOCOL } from "./generated/session-v2";
import type { StreamEnd, StreamHandle, Transport } from "./transport";
import { sessionPath } from "./transport";
import { isFrame, type Frame } from "./types";

export type ConnectionState =
  | "connecting"
  | "live"
  | "reconnecting"
  | "offline"
  | "hub_down"
  | "unauthorized"
  | "gone"
  | "outdated";

export interface ConnectionStatus {
  state: ConnectionState;
  /** Consecutive failed attempts since the stream was last live. */
  attempt: number;
  /** When the state last changed, in the timers' milliseconds. */
  since: number;
  /** When the next attempt starts, or null when none is waiting on a timer. */
  retryAt: number | null;
  /** Why, in a few words, for a tooltip: "stream went silent", "HTTP 502", a network error's message. */
  detail: string;
}

/** Timers and time, injected. A test's implementation moves only when told. */
export interface Timers {
  now(): number;
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const realTimers: Timers = {
  now: () => Date.now(),
  setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** What wakes the supervisor besides its timers: the network and the page coming back. */
export interface Environment {
  online(): boolean;
  /** Subscribe to changes; returns the unsubscribe. */
  subscribe(listener: { online(): void; offline(): void; visible(): void }): () => void;
}

/** The browser's: `navigator.onLine`, the window's online and offline events, and the page becoming visible. */
export function browserEnvironment(): Environment {
  return {
    online: () => typeof navigator === "undefined" || navigator.onLine !== false,
    subscribe(listener) {
      if (typeof window === "undefined") return () => {};
      const onVisible = () => {
        if (document.visibilityState === "visible") listener.visible();
      };
      window.addEventListener("online", listener.online);
      window.addEventListener("offline", listener.offline);
      document.addEventListener("visibilitychange", onVisible);
      // A page restored from the back-forward cache gets no visibilitychange on some browsers.
      window.addEventListener("pageshow", onVisible);
      return () => {
        window.removeEventListener("online", listener.online);
        window.removeEventListener("offline", listener.offline);
        document.removeEventListener("visibilitychange", onVisible);
        window.removeEventListener("pageshow", onVisible);
      };
    },
  };
}

export interface BackoffOptions {
  baseMs: number;
  capMs: number;
}

export const DEFAULT_BACKOFF: BackoffOptions = { baseMs: 1000, capMs: 30_000 };

/** Failed attempts in a row after which the hub counts as down. */
export const HUB_DOWN_AFTER = 3;

/**
 * The wait before attempt number `attempt` (1 is the first retry after a failure).
 * Exponential from `baseMs` to `capMs`, then half of it fixed and half random
 * ("equal jitter"), so a hub that restarts is not met by every device at once.
 */
export function backoffDelay(attempt: number, random: () => number, options: BackoffOptions = DEFAULT_BACKOFF): number {
  if (attempt <= 0) return 0;
  const ceiling = Math.min(options.capMs, options.baseMs * 2 ** (attempt - 1));
  return Math.round(ceiling / 2 + random() * (ceiling / 2));
}

export interface SupervisorOptions {
  transport: Transport;
  timers: Timers;
  random: () => number;
  environment: Environment;
  sessionKey: string;
  /** What to ask the stream for: the cursor and epoch the client holds. */
  cursor(): { last: number; epoch: number | null };
  onFrame(frame: Frame): void;
  /** A connection opened (before its first frame): the store marks the catch-up as not done. */
  onConnecting?(): void;
  onStatus(status: ConnectionStatus): void;
  backoff?: BackoffOptions;
  silenceMs?: number;
}

export class Supervisor {
  private readonly options: SupervisorOptions;
  private readonly silenceMs: number;
  private readonly backoff: BackoffOptions;
  private current: ConnectionStatus;
  private stream: StreamHandle | null = null;
  private generation = 0;
  private retryTimer: unknown = null;
  private silenceTimer: unknown = null;
  private unsubscribe: (() => void) | null = null;
  private running = false;
  private everLive = false;
  private gotFrame = false;
  private lastFrameAt = 0;

  constructor(options: SupervisorOptions) {
    this.options = options;
    this.silenceMs = options.silenceMs ?? PROTOCOL.silence_s * 1000;
    this.backoff = options.backoff ?? DEFAULT_BACKOFF;
    this.current = { state: "connecting", attempt: 0, since: options.timers.now(), retryAt: null, detail: "" };
  }

  get status(): ConnectionStatus {
    return this.current;
  }

  /** Begin connecting and watching the network. A second call does nothing. */
  start(): void {
    if (this.running) return;
    this.running = true;
    this.unsubscribe = this.options.environment.subscribe({
      online: () => this.onOnline(),
      offline: () => this.onOffline(),
      visible: () => this.wake(),
    });
    if (!this.options.environment.online()) {
      this.set("offline", { attempt: 0, detail: "no network" });
      return;
    }
    this.connect();
  }

  /** Stop for good: close the stream, cancel the timers, stop listening. */
  stop(): void {
    this.running = false;
    this.generation++;
    this.closeStream();
    this.clearRetry();
    this.clearSilence();
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  /**
   * Check the connection now. The page became visible, the network changed, or
   * the native shell woke the app: whatever was waiting on a timer or a
   * credential tries at once, and a stream that has been silent too long is
   * replaced (a device that slept holds a socket that is already dead).
   */
  wake(): void {
    if (!this.running) return;
    if (!this.options.environment.online()) {
      this.goOffline();
      return;
    }
    if (this.stream !== null) {
      // An attempt or a live stream: replace it only if it has been silent too long.
      if (this.options.timers.now() - this.lastFrameAt >= this.silenceMs) this.silent();
      return;
    }
    // Waiting on a retry timer, or in a state nothing retries out of: try now.
    this.connect();
  }

  // -- events ----------------------------------------------------------

  private onOnline(): void {
    if (!this.running) return;
    // The network came back, or changed under a stream that may be dead: start over, and
    // do not count it as a failure.
    this.set(this.everLive ? "reconnecting" : "connecting", { attempt: 0, detail: "network came back" });
    this.connect();
  }

  private onOffline(): void {
    if (!this.running) return;
    this.goOffline();
  }

  private goOffline(): void {
    this.generation++;
    this.closeStream();
    this.clearRetry();
    this.clearSilence();
    if (this.current.state !== "offline") this.set("offline", { attempt: 0, detail: "no network" });
  }

  private connect(): void {
    this.clearRetry();
    this.closeStream();
    const generation = ++this.generation;
    const { last, epoch } = this.options.cursor();
    const query = ["v=2"];
    if (last > 0 && epoch !== null) query.push(`last=${last}`, `epoch=${epoch}`);
    this.gotFrame = false;
    this.lastFrameAt = this.options.timers.now();
    // What to show while the attempt runs: a hub already counted as down stays so until it answers.
    const showing = this.current.state;
    const next: ConnectionState =
      showing === "hub_down" || showing === "reconnecting" || showing === "connecting"
        ? showing
        : this.everLive
          ? "reconnecting"
          : "connecting";
    this.set(next, { retryAt: null });
    this.options.onConnecting?.();
    this.armSilence(generation);
    this.stream = this.options.transport.open(`${sessionPath(this.options.sessionKey, "events")}?${query.join("&")}`, {
      onOpen: () => {
        if (generation !== this.generation) return;
        this.armSilence(generation);
      },
      onFrame: (value) => {
        if (generation !== this.generation) return;
        this.frame(value, generation);
      },
      onEnd: (end) => {
        if (generation !== this.generation) return;
        this.ended(end);
      },
    });
  }

  private frame(value: unknown, generation: number): void {
    this.lastFrameAt = this.options.timers.now();
    this.armSilence(generation);
    if (!isFrame(value)) {
      if (!this.gotFrame) {
        // A hub that does not know protocol 2 answers with today's frames.
        this.terminal("outdated", "the hub does not speak protocol 2");
      }
      return;
    }
    if (!this.gotFrame) {
      this.gotFrame = true;
      this.everLive = true;
      this.set("live", { attempt: 0, retryAt: null, detail: "" });
    }
    this.options.onFrame(value);
  }

  private ended(end: StreamEnd): void {
    this.stream = null;
    this.clearSilence();
    if (end.kind === "http") {
      if (end.status === 401 || end.status === 403) return this.terminal("unauthorized", `HTTP ${end.status}`);
      if (end.status === 404 || end.status === 410) return this.terminal("gone", `HTTP ${end.status}`);
      if (end.status === 426) return this.terminal("outdated", "HTTP 426");
      return this.failed(`HTTP ${end.status}`);
    }
    if (end.kind === "error") return this.failed(end.message);
    // A stream that ends cleanly after frames is a drop; one that never said anything is a failure.
    if (this.gotFrame) return this.dropped("stream closed");
    return this.failed("stream closed before any frame");
  }

  private silent(): void {
    this.generation++;
    this.closeStream();
    this.clearSilence();
    if (this.gotFrame) this.dropped("no frame for 25 s");
    else this.failed("no frame for 25 s");
  }

  /** A live stream broke. The first retry is immediate. */
  private dropped(detail: string): void {
    if (!this.running) return;
    if (!this.options.environment.online()) return this.goOffline();
    this.set("reconnecting", { attempt: 0, detail });
    this.connect();
  }

  /** An attempt did not work: back off and try again. */
  private failed(detail: string): void {
    if (!this.running) return;
    if (!this.options.environment.online()) return this.goOffline();
    const attempt = this.current.attempt + 1;
    const state: ConnectionState =
      attempt >= HUB_DOWN_AFTER ? "hub_down" : this.everLive ? "reconnecting" : "connecting";
    const delay = backoffDelay(attempt, this.options.random, this.backoff);
    const retryAt = this.options.timers.now() + delay;
    this.set(state, { attempt, detail, retryAt });
    this.retryTimer = this.options.timers.setTimeout(() => {
      this.retryTimer = null;
      if (this.running) this.connect();
    }, delay);
  }

  /** A state nothing retries out of until `wake()`. */
  private terminal(state: ConnectionState, detail: string): void {
    this.generation++;
    this.closeStream();
    this.clearSilence();
    this.clearRetry();
    this.set(state, { attempt: 0, retryAt: null, detail });
  }

  // -- plumbing --------------------------------------------------------

  private armSilence(generation: number): void {
    this.clearSilence();
    this.silenceTimer = this.options.timers.setTimeout(() => {
      this.silenceTimer = null;
      if (generation === this.generation && this.running) this.silent();
    }, this.silenceMs);
  }

  private clearSilence(): void {
    if (this.silenceTimer !== null) this.options.timers.clearTimeout(this.silenceTimer);
    this.silenceTimer = null;
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) this.options.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private closeStream(): void {
    const stream = this.stream;
    this.stream = null;
    stream?.close();
  }

  private set(state: ConnectionState, patch: Partial<Omit<ConnectionStatus, "state" | "since">>): void {
    const next: ConnectionStatus = {
      ...this.current,
      ...patch,
      state,
      since: state === this.current.state ? this.current.since : this.options.timers.now(),
    };
    const same =
      next.state === this.current.state &&
      next.attempt === this.current.attempt &&
      next.retryAt === this.current.retryAt &&
      next.detail === this.current.detail;
    this.current = next;
    if (!same) this.options.onStatus(next);
  }
}
