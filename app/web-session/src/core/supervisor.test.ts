import { describe, expect, it } from "vitest";

import {
  backoffDelay,
  DEFAULT_BACKOFF,
  HUB_DOWN_AFTER,
  Supervisor,
  type ConnectionState,
  type ConnectionStatus,
} from "./supervisor";
import { FakeEnvironment, FakeTimers, FakeTransport, sequence } from "./test-support";
import type { Frame } from "./types";

const KEY = "claude:t1";
const EVENTS = "/v1/sessions/claude%3At1/events";

const ping = (head = 0): Frame => ({ v: 2, type: "ping", session: KEY, ts: 1, data: { ts: 1, head } });
const v1Frame = { type: "session.status", seq: 3, data: { status: "active" } };

function setup(options: { random?: () => number; online?: boolean; cursor?: { last: number; epoch: number | null } } = {}) {
  const timers = new FakeTimers();
  const transport = new FakeTransport();
  const environment = new FakeEnvironment();
  environment.isOnline = options.online ?? true;
  const frames: Frame[] = [];
  const statuses: ConnectionStatus[] = [];
  let connecting = 0;
  const cursor = options.cursor ?? { last: 0, epoch: null };
  const supervisor = new Supervisor({
    transport,
    timers,
    random: options.random ?? (() => 0.5),
    environment,
    sessionKey: KEY,
    cursor: () => cursor,
    onFrame: (frame) => frames.push(frame),
    onConnecting: () => connecting++,
    onStatus: (status) => statuses.push(status),
  });
  return {
    timers,
    transport,
    environment,
    frames,
    statuses,
    cursor,
    supervisor,
    get connecting() {
      return connecting;
    },
    states: () => statuses.map((s) => s.state),
  };
}

/** Fail the open stream without a frame, as a refused connection does. */
const refuse = (t: ReturnType<typeof setup>) => t.transport.last.end({ kind: "error", message: "refused" });

describe("backoffDelay", () => {
  it("is nothing for attempt 0 and doubles from the base to the cap, with jitter in the upper half", () => {
    expect(backoffDelay(0, () => 0.9)).toBe(0);
    expect([1, 2, 3, 4].map((n) => backoffDelay(n, () => 0))).toEqual([500, 1000, 2000, 4000]);
    expect([1, 2, 3, 4].map((n) => backoffDelay(n, () => 1))).toEqual([1000, 2000, 4000, 8000]);
    expect(backoffDelay(20, () => 0)).toBe(DEFAULT_BACKOFF.capMs / 2);
    expect(backoffDelay(20, () => 1)).toBe(DEFAULT_BACKOFF.capMs);
  });

  it("uses the injected random source, so two devices do not retry together", () => {
    expect(backoffDelay(3, sequence(0.1))).not.toBe(backoffDelay(3, sequence(0.9)));
  });
});

describe("connecting and going live", () => {
  it("opens the v2 stream with no cursor the first time", () => {
    const t = setup();
    t.supervisor.start();
    expect(t.transport.streams.map((s) => s.path)).toEqual([`${EVENTS}?v=2`]);
    expect(t.supervisor.status).toMatchObject({ state: "connecting", attempt: 0 });
    expect(t.connecting).toBe(1);
  });

  it("asks with the cursor and epoch it holds", () => {
    const t = setup({ cursor: { last: 41, epoch: 3 } });
    t.supervisor.start();
    expect(t.transport.last.query).toEqual({ v: "2", last: "41", epoch: "3" });
  });

  it("leaves the cursor out when it has none, or does not know the epoch", () => {
    for (const cursor of [{ last: 0, epoch: 0 }, { last: 5, epoch: null }]) {
      const t = setup({ cursor });
      t.supervisor.start();
      expect(t.transport.last.query).toEqual({ v: "2" });
    }
  });

  it("is live from the first frame, and hands every frame on", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.open();
    expect(t.supervisor.status.state).toBe("connecting");
    t.transport.last.send(ping(4), ping(5));
    expect(t.supervisor.status).toMatchObject({ state: "live", attempt: 0, detail: "" });
    expect(t.frames).toHaveLength(2);
    expect(t.states()).toEqual(["live"]);
  });

  it("starting twice opens one stream", () => {
    const t = setup();
    t.supervisor.start();
    t.supervisor.start();
    expect(t.transport.streams).toHaveLength(1);
    expect(t.environment.subscribers).toBe(1);
  });
});

describe("silence", () => {
  it("treats 25 s without a frame as a dead connection and reconnects with the cursor", () => {
    const t = setup({ cursor: { last: 9, epoch: 1 } });
    t.supervisor.start();
    t.transport.last.send(ping());
    const first = t.transport.last;
    t.timers.advance(24_999);
    expect(t.supervisor.status.state).toBe("live");
    t.timers.advance(1);
    expect(first.closed).toBe(true);
    expect(t.supervisor.status).toMatchObject({ state: "reconnecting", attempt: 0, detail: "no frame for 25 s" });
    // The first retry after a drop is immediate.
    expect(t.transport.streams).toHaveLength(2);
    expect(t.transport.last.query).toEqual({ v: "2", last: "9", epoch: "1" });
  });

  it("restarts the 25 s with every frame, a ping included", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.send(ping());
    for (let i = 0; i < 6; i++) {
      t.timers.advance(10_000);
      t.transport.last.send(ping());
    }
    expect(t.supervisor.status.state).toBe("live");
    expect(t.transport.streams).toHaveLength(1);
  });

  it("counts a stream that never says anything as a failed attempt, not a drop", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.open();
    t.timers.advance(25_000);
    expect(t.supervisor.status).toMatchObject({ state: "connecting", attempt: 1 });
    expect(t.transport.streams).toHaveLength(1);
    t.timers.advance(750); // the jittered first wait, with a random source that says 0.5
    expect(t.transport.streams).toHaveLength(2);
  });

  it("goes live again when the new stream speaks", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.send(ping());
    t.timers.advance(25_000);
    t.transport.last.send(ping());
    expect(t.states()).toEqual(["live", "reconnecting", "live"]);
  });
});

describe("a dropped stream", () => {
  it("is retried at once, and an end after frames is not a failure", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.send(ping());
    t.timers.advance(10_000); // it stayed open for a ping interval
    t.transport.last.send(ping());
    t.transport.streams[0]?.end({ kind: "closed" });
    expect(t.supervisor.status).toMatchObject({ state: "reconnecting", attempt: 0, detail: "stream closed" });
    expect(t.transport.streams).toHaveLength(2);
  });

  it("ignores frames from a stream it has already replaced", () => {
    const t = setup();
    t.supervisor.start();
    const old = t.transport.last;
    old.send(ping());
    t.timers.advance(25_000);
    expect(t.frames).toHaveLength(1);
    old.send(ping(99));
    old.end({ kind: "closed" });
    expect(t.frames).toHaveLength(1);
    expect(t.transport.streams).toHaveLength(2);
  });
});

describe("a stream that speaks once and closes", () => {
  it("goes through the backoff every time: growing, capped waits, never two opens without a timer between", () => {
    const t = setup({ random: () => 1 });
    t.supervisor.start();
    const waits: number[] = [];
    for (let i = 0; i < 50; i++) {
      const opened = t.transport.streams.length;
      t.transport.last.send(ping()).end({ kind: "closed" });
      // No stream was opened by the end itself: a timer waits.
      expect(t.transport.streams).toHaveLength(opened);
      const wait = (t.supervisor.status.retryAt ?? 0) - t.timers.now();
      expect(t.supervisor.status.attempt).toBe(i + 1);
      expect(wait).toBeGreaterThan(0);
      waits.push(wait);
      t.timers.advance(wait - 1);
      expect(t.transport.streams).toHaveLength(opened);
      t.timers.advance(1);
      expect(t.transport.streams).toHaveLength(opened + 1);
    }
    expect(waits.slice(0, 6)).toEqual([1000, 2000, 4000, 8000, 16_000, 30_000]);
    expect(Math.max(...waits)).toBe(30_000);
    expect(waits.slice(5).every((w) => w === 30_000)).toBe(true);
    expect(t.supervisor.status.state).toBe("hub_down");
  });

  it("is forgiven once a stream has stayed open for a ping interval", () => {
    const t = setup({ random: () => 1 });
    t.supervisor.start();
    for (let i = 0; i < 3; i++) {
      t.transport.last.send(ping()).end({ kind: "closed" });
      t.timers.advance((t.supervisor.status.retryAt ?? 0) - t.timers.now());
    }
    expect(t.supervisor.status.attempt).toBe(3);
    t.transport.last.send(ping());
    t.timers.advance(10_000);
    expect(t.supervisor.status).toMatchObject({ state: "live", attempt: 0 });
  });
});

describe("failing to connect", () => {
  it("backs off with jitter, then counts the hub as down", () => {
    const t = setup({ random: () => 0 });
    t.supervisor.start();
    const seen: [ConnectionState, number, number | null][] = [];
    const delays = [500, 1000, 2000, 4000, 8000];
    for (const delay of delays) {
      refuse(t);
      const { state, attempt, retryAt } = t.supervisor.status;
      seen.push([state, attempt, retryAt === null ? null : retryAt - t.timers.now()]);
      t.timers.advance(delay - 1);
      expect(t.transport.streams.filter((s) => !s.ended)).toHaveLength(0);
      t.timers.advance(1);
      expect(t.transport.openStreams).toHaveLength(1);
    }
    expect(seen).toEqual([
      ["connecting", 1, 500],
      ["connecting", 2, 1000],
      ["hub_down", 3, 2000],
      ["hub_down", 4, 4000],
      ["hub_down", 5, 8000],
    ]);
    expect(HUB_DOWN_AFTER).toBe(3);
  });

  it("caps the wait at 30 s", () => {
    const t = setup({ random: () => 1 });
    t.supervisor.start();
    let last = 0;
    for (let i = 0; i < 12; i++) {
      refuse(t);
      last = (t.supervisor.status.retryAt ?? 0) - t.timers.now();
      t.timers.advance(last);
    }
    expect(last).toBe(30_000);
  });

  it("recovers when the hub answers: live again, attempts back to 0", () => {
    const t = setup();
    t.supervisor.start();
    refuse(t);
    refuse2(t);
    t.transport.last.send(ping());
    // Live at once, but the count only resets once the stream has stayed open.
    expect(t.supervisor.status).toMatchObject({ state: "live", attempt: 2, retryAt: null });
    t.timers.advance(10_000);
    t.transport.last.send(ping());
    expect(t.supervisor.status).toMatchObject({ state: "live", attempt: 0, retryAt: null });
    expect(t.timers.pending).toBe(1); // only the silence timer
  });

  it("calls an attempt that fails after having been live reconnecting, until it has failed three times", () => {
    const t = setup({ random: () => 0 });
    t.supervisor.start();
    t.transport.last.send(ping());
    t.timers.advance(10_000);
    t.transport.last.end({ kind: "closed" }); // dropped after a stable stream: immediate retry
    refuse(t);
    expect(t.supervisor.status).toMatchObject({ state: "reconnecting", attempt: 1 });
    t.timers.advance(500);
    refuse(t);
    expect(t.supervisor.status).toMatchObject({ state: "reconnecting", attempt: 2 });
    t.timers.advance(1000);
    refuse(t);
    expect(t.supervisor.status).toMatchObject({ state: "hub_down", attempt: 3 });
  });

  it("treats a server error like a refused connection and says what it was", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.end({ kind: "http", status: 502 });
    expect(t.supervisor.status).toMatchObject({ state: "connecting", attempt: 1, detail: "HTTP 502" });
  });

  it("closes a stream that ends before it said anything and retries", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.open().end({ kind: "closed" });
    expect(t.supervisor.status).toMatchObject({ state: "connecting", attempt: 1 });
  });
});

function refuse2(t: ReturnType<typeof setup>) {
  t.timers.advance(t.supervisor.status.retryAt === null ? 0 : t.supervisor.status.retryAt - t.timers.now());
  refuse(t);
  t.timers.advance(t.supervisor.status.retryAt === null ? 0 : t.supervisor.status.retryAt - t.timers.now());
}

describe("the states nothing retries out of", () => {
  it("unauthorized: 401 and 403 stop the retries until wake()", () => {
    for (const status of [401, 403]) {
      const t = setup();
      t.supervisor.start();
      t.transport.last.end({ kind: "http", status });
      expect(t.supervisor.status).toMatchObject({ state: "unauthorized", retryAt: null, detail: `HTTP ${status}` });
      t.timers.advance(10 * 60_000);
      expect(t.transport.streams).toHaveLength(1);
      expect(t.timers.pending).toBe(0);
      // A new credential, or the person trying again.
      t.supervisor.wake();
      expect(t.transport.streams).toHaveLength(2);
      expect(t.supervisor.status.state).toBe("connecting");
    }
  });

  it("gone: 404 and 410 mean the session does not exist", () => {
    for (const status of [404, 410]) {
      const t = setup();
      t.supervisor.start();
      t.transport.last.end({ kind: "http", status });
      expect(t.supervisor.status.state).toBe("gone");
      t.timers.advance(10 * 60_000);
      expect(t.transport.streams).toHaveLength(1);
    }
  });

  it("outdated: a hub that answers with today's frames does not speak protocol 2", () => {
    const t = setup();
    t.supervisor.start();
    const stream = t.transport.last;
    stream.send(v1Frame);
    expect(t.supervisor.status).toMatchObject({ state: "outdated", detail: "the hub does not speak protocol 2" });
    expect(stream.closed).toBe(true);
    expect(t.frames).toEqual([]);
    t.timers.advance(10 * 60_000);
    expect(t.transport.streams).toHaveLength(1);
  });

  it("outdated: HTTP 426", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.end({ kind: "http", status: 426 });
    expect(t.supervisor.status.state).toBe("outdated");
  });

  it("ignores a stray non-frame once the stream is live", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.send(ping(), { hello: 1 }, ping());
    expect(t.supervisor.status.state).toBe("live");
    expect(t.frames).toHaveLength(2);
  });
});

describe("offline", () => {
  it("does not connect while the device has no network, and connects when it is back", () => {
    const t = setup({ online: false });
    t.supervisor.start();
    expect(t.supervisor.status).toMatchObject({ state: "offline", detail: "no network" });
    expect(t.transport.streams).toHaveLength(0);
    expect(t.timers.pending).toBe(0);
    t.environment.goOnline();
    expect(t.transport.streams).toHaveLength(1);
    expect(t.supervisor.status.state).toBe("connecting");
    t.transport.last.send(ping());
    expect(t.supervisor.status.state).toBe("live");
  });

  it("closes the stream and stops every timer when the network goes", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.send(ping());
    const stream = t.transport.last;
    t.environment.goOffline();
    expect(stream.closed).toBe(true);
    expect(t.supervisor.status.state).toBe("offline");
    expect(t.timers.pending).toBe(0);
    t.timers.advance(60_000);
    expect(t.transport.streams).toHaveLength(1);
  });

  it("reconnects at once when it comes back, without counting a failure", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.send(ping());
    t.environment.goOffline();
    t.environment.goOnline();
    expect(t.supervisor.status).toMatchObject({ state: "reconnecting", attempt: 0 });
    expect(t.transport.streams).toHaveLength(2);
    t.transport.last.send(ping());
    expect(t.states()).toEqual(["live", "offline", "reconnecting", "live"]);
  });

  it("is offline, not hub down, when a failure comes while the network is gone", () => {
    const t = setup();
    t.supervisor.start();
    t.environment.isOnline = false; // the browser has not fired its event yet
    refuse(t);
    expect(t.supervisor.status.state).toBe("offline");
    expect(t.timers.pending).toBe(0);
  });

  it("starts the connection over when the network changes under a live stream", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.send(ping());
    const first = t.transport.last;
    t.environment.goOnline(); // wifi to cellular: an online event without an offline one
    expect(first.closed).toBe(true);
    expect(t.transport.streams).toHaveLength(2);
    expect(t.supervisor.status.state).toBe("reconnecting");
  });
});

describe("wake()", () => {
  it("skips the wait: an attempt starts now and the retry timer is cancelled", () => {
    const t = setup({ random: () => 1 });
    t.supervisor.start();
    refuse(t);
    expect(t.supervisor.status.retryAt).not.toBeNull();
    t.supervisor.wake();
    expect(t.transport.streams).toHaveLength(2);
    expect(t.supervisor.status.retryAt).toBeNull();
    // The cancelled timer does not open a second stream.
    t.transport.last.send(ping());
    t.timers.advance(20_000);
    expect(t.transport.streams).toHaveLength(2);
    expect(t.transport.openStreams).toHaveLength(1);
  });

  it("is the page becoming visible", () => {
    const t = setup({ random: () => 1 });
    t.supervisor.start();
    refuse(t);
    t.environment.becomeVisible();
    expect(t.transport.streams).toHaveLength(2);
    expect(t.supervisor.status.retryAt).toBeNull();
  });

  it("leaves a healthy live stream alone", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.send(ping());
    t.timers.advance(8_000);
    t.supervisor.wake();
    expect(t.transport.streams).toHaveLength(1);
    expect(t.supervisor.status.state).toBe("live");
  });

  it("replaces a live stream that went quiet while the device slept", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.send(ping());
    const old = t.transport.last;
    t.timers.skip(10 * 60_000); // asleep: no timer ran
    t.supervisor.wake();
    expect(old.closed).toBe(true);
    expect(t.transport.streams).toHaveLength(2);
    expect(t.supervisor.status.state).toBe("reconnecting");
  });

  it("does not start a second attempt while one is under way", () => {
    const t = setup();
    t.supervisor.start();
    t.supervisor.wake();
    t.supervisor.wake();
    expect(t.transport.streams).toHaveLength(1);
  });

  it("while offline stays offline", () => {
    const t = setup({ online: false });
    t.supervisor.start();
    t.supervisor.wake();
    expect(t.transport.streams).toHaveLength(0);
    expect(t.supervisor.status.state).toBe("offline");
  });

  it("does nothing before start() or after stop()", () => {
    const t = setup();
    t.supervisor.wake();
    expect(t.transport.streams).toHaveLength(0);
    t.supervisor.start();
    t.supervisor.stop();
    t.supervisor.wake();
    expect(t.transport.streams).toHaveLength(1);
  });
});

describe("stop()", () => {
  it("closes the stream, cancels every timer and stops listening", () => {
    const t = setup();
    t.supervisor.start();
    t.transport.last.send(ping());
    const stream = t.transport.last;
    t.supervisor.stop();
    expect(stream.closed).toBe(true);
    expect(t.timers.pending).toBe(0);
    expect(t.environment.subscribers).toBe(0);
    stream.send(ping(7));
    t.timers.advance(60_000);
    expect(t.frames).toHaveLength(1);
    expect(t.transport.streams).toHaveLength(1);
  });

  it("cancels a retry that is waiting", () => {
    const t = setup();
    t.supervisor.start();
    refuse(t);
    t.supervisor.stop();
    t.timers.advance(60_000);
    expect(t.transport.streams).toHaveLength(1);
  });
});

describe("status reports", () => {
  it("says when the state changed and reports a change once", () => {
    const t = setup();
    t.supervisor.start();
    const startedAt = t.timers.now();
    t.timers.advance(3000);
    t.transport.last.send(ping(), ping(), ping());
    expect(t.statuses).toHaveLength(1);
    expect(t.supervisor.status.since).toBe(startedAt + 3000);
    t.timers.advance(1000);
    t.transport.last.send(ping());
    expect(t.supervisor.status.since).toBe(startedAt + 3000);
  });
});
