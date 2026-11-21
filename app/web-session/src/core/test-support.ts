// Helpers for the core's tests: a clock that moves only when told, a
// transport that plays what a test scripts, an environment a test pokes, and
// the recorded fixtures. Nothing opens a socket, and the only waiting is
// `drain` and `until`, which wait on a condition (never a fixed count of ticks).
//
// Only tests import this file, so the bundle never carries it.
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { Environment, Timers } from "./supervisor";
import type {
  JsonRequest,
  JsonResponse,
  StreamEnd,
  StreamHandle,
  StreamHandlers,
  Transport,
} from "./transport";
import type { Frame, Snapshot } from "./types";

// ---------------------------------------------------------------------
// Waiting for work that finishes on its own

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

/** The most ticks `drain` and `until` wait before they fail the test. */
export const WAIT_LIMIT = 2000;

/**
 * Work that will still run without the test doing anything. The code under test takes its timers
 * from FakeTimers and its requests from FakeTransport, so the only such work is fake-indexeddb's:
 * it runs each step of a transaction from `setImmediate`. Microtasks have all run by the time a
 * tick's callback does.
 */
function queuedSteps(): number {
  return process.getActiveResourcesInfo().filter((name) => name === "Immediate").length;
}

/**
 * Waits until nothing is left that will finish on its own: every storage step done, and every
 * request a handler answers at once answered. A request a test holds open stays open. Fails the
 * test, saying what was still queued, if that takes more than `limit` ticks.
 */
export async function drain(limit = WAIT_LIMIT): Promise<void> {
  let quiet = 0;
  for (let i = 0; i < limit; i++) {
    await tick();
    quiet = queuedSteps() === 0 ? quiet + 1 : 0;
    if (quiet >= 2) return;
  }
  throw new Error(`drain: still busy after ${limit} ticks, ${queuedSteps()} IndexedDB step(s) queued`);
}

/** Waits until `condition()` holds. Fails the test, naming `what`, if it does not within `limit` ticks. */
export async function until(condition: () => boolean, what: string, limit = WAIT_LIMIT): Promise<void> {
  for (let i = 0; i < limit; i++) {
    if (condition()) return;
    await tick();
  }
  if (!condition()) throw new Error(`until: ${what} did not happen within ${limit} ticks`);
}

// ---------------------------------------------------------------------
// Time

interface Timer {
  id: number;
  at: number;
  callback: () => void;
}

/** A clock and timers that move only when `advance` is called. */
export class FakeTimers implements Timers {
  private time: number;
  private next = 1;
  private timers: Timer[] = [];

  constructor(start = 1_000_000) {
    this.time = start;
  }

  now(): number {
    return this.time;
  }

  /** Timers armed with no delay. Nothing in the core needs one; one that re-arms itself at 0 ms spins. */
  zeroDelay = 0;

  setTimeout(callback: () => void, ms: number): unknown {
    if (ms <= 0) {
      // A guard for the whole suite: every test that uses these timers fails on a zero-delay timer.
      this.zeroDelay++;
      throw new Error(`a timer was armed with a delay of ${ms} ms: a timer that re-arms at 0 ms spins`);
    }
    const timer = { id: this.next++, at: this.time + Math.max(0, ms), callback };
    this.timers.push(timer);
    return timer.id;
  }

  clearTimeout(handle: unknown): void {
    this.timers = this.timers.filter((t) => t.id !== handle);
  }

  /** How many timers are waiting. */
  get pending(): number {
    return this.timers.length;
  }

  /** Milliseconds until the next timer, or undefined when none waits. */
  get nextIn(): number | undefined {
    const at = Math.min(...this.timers.map((t) => t.at));
    return this.timers.length === 0 ? undefined : at - this.time;
  }

  /**
   * Move the clock without running any timer: a device that slept, whose timers
   * did not fire while it was off.
   */
  skip(ms: number): void {
    this.time += ms;
  }

  /** Move time forward, running every timer that falls due, in order. */
  advance(ms: number): void {
    const target = this.time + ms;
    for (;;) {
      const due = this.timers.filter((t) => t.at <= target).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (due === undefined) break;
      this.timers = this.timers.filter((t) => t !== due);
      this.time = Math.max(this.time, due.at);
      due.callback();
    }
    this.time = target;
  }
}

/** A random source that returns the given values in turn (then the last one forever). */
export function sequence(...values: number[]): () => number {
  let i = 0;
  return () => values[Math.min(i++, values.length - 1)] as number;
}

// ---------------------------------------------------------------------
// Environment

export class FakeEnvironment implements Environment {
  isOnline = true;
  private listeners = new Set<{ online(): void; offline(): void; visible(): void }>();

  online(): boolean {
    return this.isOnline;
  }

  subscribe(listener: { online(): void; offline(): void; visible(): void }): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  get subscribers(): number {
    return this.listeners.size;
  }

  goOffline(): void {
    this.isOnline = false;
    for (const l of this.listeners) l.offline();
  }

  goOnline(): void {
    this.isOnline = true;
    for (const l of this.listeners) l.online();
  }

  becomeVisible(): void {
    for (const l of this.listeners) l.visible();
  }
}

// ---------------------------------------------------------------------
// Transport

/** One stream a test opened, with the controls to feed it. */
export class FakeStream implements StreamHandle {
  closed = false;
  opened = false;
  ended = false;

  constructor(
    readonly path: string,
    private readonly handlers: StreamHandlers,
  ) {}

  /** The query of the stream's URL as an object. */
  get query(): Record<string, string> {
    return Object.fromEntries(new URLSearchParams(this.path.split("?")[1] ?? ""));
  }

  open(): this {
    this.opened = true;
    this.handlers.onOpen();
    return this;
  }

  /** Deliver frames as the server would (opening the stream first if it is not). */
  send(...frames: unknown[]): this {
    if (!this.opened) this.open();
    for (const frame of frames) this.handlers.onFrame(frame);
    return this;
  }

  end(end: StreamEnd): this {
    this.ended = true;
    this.handlers.onEnd(end);
    return this;
  }

  close(): void {
    this.closed = true;
  }
}

export type JsonHandler = (path: string, request: JsonRequest) => JsonResponse | Promise<JsonResponse>;

export interface JsonCall {
  path: string;
  request: JsonRequest;
}

/** A transport a test scripts: streams are handed back to the test; JSON requests go to handlers. */
export class FakeTransport implements Transport {
  streams: FakeStream[] = [];
  calls: JsonCall[] = [];
  private handlers: { match: (path: string, request: JsonRequest) => boolean; handler: JsonHandler }[] = [];
  /** Makes every JSON request reject, as when there is no network. */
  down = false;

  open(path: string, handlers: StreamHandlers): StreamHandle {
    const stream = new FakeStream(path, handlers);
    this.streams.push(stream);
    return stream;
  }

  get last(): FakeStream {
    return this.streams[this.streams.length - 1] as FakeStream;
  }

  /** Streams that have been opened and not closed or ended. */
  get openStreams(): FakeStream[] {
    return this.streams.filter((s) => !s.closed && !s.ended);
  }

  /** Answer JSON requests whose path contains `match` (and, optionally, whose method is `method`). */
  on(match: string, handler: JsonHandler, method?: "GET" | "POST"): void {
    this.handlers.unshift({
      match: (path, request) => path.includes(match) && (method === undefined || (request.method ?? "GET") === method),
      handler,
    });
  }

  async json(path: string, request: JsonRequest = {}): Promise<JsonResponse> {
    this.calls.push({ path, request });
    if (this.down) throw new TypeError("network down");
    const found = this.handlers.find((h) => h.match(path, request));
    if (found === undefined) return { status: 404, body: { error: { code: "not_found", message: `no handler for ${path}` } } };
    return found.handler(path, request);
  }

  /** The bodies of the POSTs made to a path containing `match`. */
  posts(match: string): Record<string, unknown>[] {
    return this.calls
      .filter((c) => c.path.includes(match) && c.request.method === "POST")
      .map((c) => c.request.body as Record<string, unknown>);
  }
}

// ---------------------------------------------------------------------
// Fixtures

const here = dirname(fileURLToPath(import.meta.url));
export const FIXTURE_DIR = join(here, "../../../../tests/fixtures/session-view");

export interface Fixture {
  scenario: string;
  about: string;
  connections: { request: { last: number | null; epoch: number | null }; frames: Frame[] }[];
  snapshots: { after: [number, number]; body: Snapshot }[];
}

export function fixtureNames(): string[] {
  return readdirSync(FIXTURE_DIR)
    .filter((name) => name.endsWith(".json"))
    .map((name) => name.slice(0, -".json".length))
    .sort();
}

export function loadFixture(name: string): Fixture {
  return JSON.parse(readFileSync(join(FIXTURE_DIR, `${name}.json`), "utf8")) as Fixture;
}

/** The snapshot frame the hub would send for a snapshot body. */
export function snapshotFrame(body: Snapshot, ts = 0): Frame {
  return { v: 2, type: "session.snapshot", session: body.session, ts, data: body };
}
