import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { createSessionStore, describeSends, type Frame, type SessionStore } from "../core";
import { MemoryStorage } from "../core/outbox-storage";
import {
  FakeEnvironment,
  FakeTimers,
  FakeTransport,
  drain,
  loadFixture,
  snapshotFrame,
  type Fixture,
} from "../core/test-support";
import { SessionView } from "./SessionView";

declare global {
  // React reads this to know act() wraps the updates.
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

const KEY = "claude:session-1";

let root: Root | undefined;
let host: HTMLElement | undefined;
const stores: SessionStore[] = [];

afterEach(() => {
  act(() => root?.unmount());
  for (const store of stores.splice(0)) store.getState().stop();
  host?.remove();
  root = undefined;
  host = undefined;
});

function setup(over: { online?: boolean } = {}) {
  const timers = new FakeTimers();
  const transport = new FakeTransport();
  const environment = new FakeEnvironment();
  environment.isOnline = over.online ?? true;
  let ids = 0;
  const store = createSessionStore({
    sessionKey: KEY,
    transport,
    timers,
    random: () => 0,
    environment,
    openStorage: async () => new MemoryStorage("no database in tests"),
    newId: () => `id${++ids}`,
  });
  stores.push(store);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  act(() => {
    root?.render(<SessionView now={() => timers.now()} sessionKey={KEY} store={store} />);
    store.getState().start();
  });
  return { store, transport, timers, environment, el: host };
}

/** Run something that changes the store, and let the view and the store's own async work settle. */
async function settle(work: () => unknown = () => {}): Promise<void> {
  await act(async () => {
    await work();
    await drain();
  });
}

const frames = (fixture: Fixture, connection = 0): Frame[] => (fixture.connections[connection]?.frames ?? []) as Frame[];

async function play(t: ReturnType<typeof setup>, name: string, from = 0, to?: number): Promise<Fixture> {
  const fixture = loadFixture(name);
  await settle(() => void t.transport.last.send(...frames(fixture).slice(from, to)));
  return fixture;
}

const text = (el: Element | null | undefined) => (el?.textContent ?? "").replace(/\s+/g, " ").trim();
const q = (el: Element, selector: string) => el.querySelector(selector);
const all = (el: Element, selector: string) => Array.from(el.querySelectorAll(selector));

function button(el: Element, label: string): HTMLButtonElement | undefined {
  return all(el, "button").find((b) => text(b).includes(label)) as HTMLButtonElement | undefined;
}

async function click(target: Element | undefined | null): Promise<void> {
  if (target === undefined || target === null) throw new Error("nothing to click");
  await settle(() => (target as HTMLElement).click());
}

describe("the recorded session, drawn", () => {
  it("draws the user's words, the reply as prose, and the steps between them in a collapsed work group", async () => {
    const t = setup();
    await play(t, "default");
    const log = q(t.el, '[role="log"]') as HTMLElement;

    expect(text(q(log, '[data-bubble="user"]'))).toBe("hello");
    // The reply is Markdown: its `src/app.py` is a code element, not backticks.
    const reply = all(log, ".sv-prose").find((p) => text(p).startsWith("Done."));
    expect(text(reply)).toBe("Done. I changed the greeting in src/app.py.");
    expect(text(reply?.querySelector("code"))).toBe("src/app.py");

    const header = button(log, "Worked for") as HTMLButtonElement;
    expect(text(header)).toMatch(/^Worked for \d+s · 3 tool calls, 1 thought 1 failed$/);
    expect(header.getAttribute("aria-expanded")).toBe("false");
    // Closed: its steps are not drawn yet, and its panel is out of the tab order.
    expect(text(q(log, ".sv-collapse"))).toBe("");
    expect(q(log, ".sv-collapse")?.hasAttribute("inert")).toBe(true);

    // The turn's footer: one file, one line each way.
    expect(text(q(log, '[data-row="footer"]'))).toBe("1 file +1 −1");
    // The second turn.
    expect(all(log, '[data-bubble="user"]').map(text)).toEqual(["hello", "once more"]);
    expect(text(log)).toContain("Second reply");
  });

  it("opens a work group on click and shows each step; a tool opens again for its input, output and diff", async () => {
    const t = setup();
    await play(t, "default");
    const log = q(t.el, '[role="log"]') as HTMLElement;
    await click(button(log, "Worked for"));

    const panel = q(log, ".sv-collapse") as HTMLElement;
    expect(panel.getAttribute("data-open")).toBe("true");
    expect(panel.hasAttribute("inert")).toBe(false);
    for (const title of ["Read src/app.py", "Edit src/app.py", "npm test", "Thought for 2s"]) {
      expect(text(panel)).toContain(title);
    }
    expect(text(panel)).toContain("Checkpoint saved · 1 file · before the edit");
    expect(text(panel)).toContain("Restored files to a checkpoint");

    await click(all(panel, "button").find((b) => text(b).includes("Edit src/app.py")));
    expect(text(panel)).toContain("Changed src/app.py +1 −1");
    const diffLines = all(panel, "pre code span").map(text);
    expect(diffLines.some((line) => line.startsWith("+"))).toBe(true);
    expect(diffLines.some((line) => line.startsWith("-"))).toBe(true);

    await click(all(panel, "button").find((b) => text(b).includes("npm test")));
    expect(text(panel)).toContain("Error output");
    expect(text(panel)).toContain("1 failing");

    // Closing it again keeps the person's choice, and the group does not reopen by itself.
    await click(button(log, "Worked for"));
    expect(q(log, ".sv-collapse")?.getAttribute("data-open")).toBe("false");
  });

  it("is an empty state for a loaded session with nothing in it", async () => {
    const t = setup();
    const fixture = loadFixture("default");
    const body = { ...(fixture.snapshots[0]?.body as NonNullable<Fixture["snapshots"][number]>["body"]), messages: [], state: "idle" as const, open_turn: null };
    await settle(() => void t.transport.last.send(snapshotFrame(body)));
    expect(text(q(t.el, '[data-state="empty"]'))).toBe("No messages yet");
  });

  it("is a loading state until the first frame, then the breadcrumb comes from the session's row", async () => {
    const t = setup();
    expect(q(t.el, '[data-state="loading"]')).not.toBeNull();
    expect(text(q(t.el, 'nav[aria-label="Breadcrumb"]'))).toBe("claude/session-1");
    await play(t, "default");
    expect(q(t.el, '[data-state="loading"]')).toBeNull();
    expect(text(q(t.el, 'nav[aria-label="Breadcrumb"]'))).toBe("project/hello");
  });
});

describe("a reply being written", () => {
  it("shows the text as the deltas apply it, with a caret, and swaps it for the message once, not twice", async () => {
    const t = setup();
    const fixture = loadFixture("streaming");
    const all_ = frames(fixture);
    const deltas = all_.map((f, i) => (f.type === "message.delta" ? i : -1)).filter((i) => i >= 0);
    await settle(() => void t.transport.last.send(...all_.slice(0, (deltas[2] as number) + 1)));
    const live = q(t.el, '[data-live="assistant"]') as HTMLElement;
    expect(live).not.toBeNull();
    expect(live.getAttribute("aria-busy")).toBe("true");
    expect(text(live)).toBe("Hello wor");
    expect(live.classList.contains("sv-live")).toBe(true);

    await settle(() => void t.transport.last.send(...all_.slice((deltas[2] as number) + 1)));
    expect(q(t.el, '[data-live="assistant"]')).toBeNull();
    expect(all(t.el, ".sv-prose").filter((p) => text(p) === "Hello world")).toHaveLength(1);
  });

  it("says so when a hole in the deltas stopped the text, and goes on from the snapshot's text", async () => {
    const t = setup();
    const fixture = loadFixture("streaming");
    const all_ = frames(fixture);
    const hole = fixture.snapshots[0] as Fixture["snapshots"][number];
    let answer: (r: { status: number; body: unknown }) => void = () => {};
    t.transport.on("/snapshot", () => new Promise((resolve) => (answer = resolve)));
    const deltas = all_.map((f, i) => (f.type === "message.delta" ? i : -1)).filter((i) => i >= 0);
    // The first and the third delta: the second is lost.
    await settle(() => void t.transport.last.send(...all_.slice(0, (deltas[0] as number) + 1), all_[deltas[2] as number] as Frame));
    expect(text(q(t.el, '[data-live="assistant"]'))).toContain("Catching up with the rest of this reply.");
    await settle(() => answer({ status: 200, body: hole.body }));
    expect(text(q(t.el, '[data-live="assistant"]'))).not.toContain("Catching up");
    expect(text(q(t.el, '[data-live="assistant"]'))).toContain("Hello wor");
  });
});

describe("the connection", () => {
  it("says live, and nothing else, when all is well", async () => {
    const t = setup();
    await play(t, "default");
    expect(text(q(t.el, "[data-connection]"))).toBe("Live");
    expect(q(t.el, "[data-banner]")).toBeNull();
  });

  it("explains each way it cannot reach the hub, in the header and under it", async () => {
    const t = setup();
    await play(t, "default");

    await settle(() => t.environment.goOffline());
    expect(text(q(t.el, "[data-connection]"))).toBe("Offline");
    expect(text(q(t.el, '[data-banner="offline"]'))).toContain("messages you send wait on this device");
    // What was loaded stays on screen.
    expect(text(q(t.el, '[role="log"]'))).toContain("Second reply");

    await settle(() => t.environment.goOnline());
    await settle(() => t.transport.last.send(...frames(loadFixture("default"), 0).slice(0, 2)));
    expect(text(q(t.el, "[data-connection]"))).toBe("Live");
    expect(q(t.el, "[data-banner]")).toBeNull();
  });

  it("says the session is gone, and says it instead of a spinner when nothing loaded", async () => {
    const t = setup();
    await settle(() => t.transport.last.end({ kind: "http", status: 404 }));
    expect(text(q(t.el, "[data-connection]"))).toBe("Session gone");
    expect(text(q(t.el, '[data-state="unavailable"]'))).toBe("Session gone");
    expect(q(t.el, '[data-state="loading"]')).toBeNull();
  });

  it("says the hub is too old for a hub that answers 426, once, where the timeline would be", async () => {
    const t = setup();
    await settle(() => t.transport.last.end({ kind: "http", status: 426 }));
    expect(text(q(t.el, '[data-state="unavailable"]'))).toContain("too old");
    expect(q(t.el, "[data-banner]")).toBeNull();
  });

  it("says it is catching up between the stream opening and the state frame", async () => {
    const t = setup();
    const fixture = loadFixture("default");
    await settle(() => void t.transport.last.send(frames(fixture)[0] as Frame));
    expect(text(q(t.el, "[data-connection]"))).toBe("Catching up");
    await settle(() => void t.transport.last.send(frames(fixture)[1] as Frame));
    expect(text(q(t.el, "[data-connection]"))).toBe("Live");
  });
});

describe("a request in the timeline", () => {
  it("says it waits while open and how it ended once answered", async () => {
    const t = setup();
    const fixture = loadFixture("approval");
    const all_ = frames(fixture);
    const resolved = all_.findIndex((f) => f.type === "approval.resolved");
    await settle(() => void t.transport.last.send(...all_.slice(0, resolved)));
    const open = q(t.el, '[data-request="open"]') as HTMLElement;
    expect(text(open)).toContain("Waiting for your answer");
    await settle(() => void t.transport.last.send(...all_.slice(resolved)));
    expect(q(t.el, '[data-request="open"]')).toBeNull();
    expect(all(t.el, '[data-request="closed"]').map(text).join(" ")).toContain("Allowed");
  });
});

describe("sends from the outbox", () => {
  async function ready(over: { online?: boolean } = {}) {
    const t = setup(over);
    await play(t, "default");
    return t;
  }

  /** What the hub answers a posted message with. */
  const answer = (t: ReturnType<typeof setup>, status: number, body: unknown) =>
    t.transport.on("/message", () => ({ status, body }), "POST");

  it("shows a message waiting for the network with its reason and a Discard, and no Retry", async () => {
    const t = await ready();
    await settle(() => t.environment.goOffline());
    await settle(async () => void (await t.store.getState().send({ text: "fix the test" })));
    const bubble = q(t.el, '[data-send="waiting"]') as HTMLElement;
    expect(text(bubble)).toContain("fix the test");
    expect(text(bubble)).toContain("Waiting: you are offline. It will be sent when you are back.");
    expect(button(bubble, "Discard")).toBeDefined();
    expect(button(bubble, "Retry")).toBeUndefined();

    // Discarding takes it away.
    await click(button(bubble, "Discard"));
    expect(q(t.el, "[data-send]")).toBeNull();
    expect(t.store.getState().sends).toEqual([]);
  });

  it("shows a message the hub queued as sent, with no action", async () => {
    const t = await ready();
    answer(t, 202, { receipt: "queued" });
    await settle(async () => void (await t.store.getState().send({ text: "then lint it" })));
    const bubble = q(t.el, '[data-send="sent"]') as HTMLElement;
    expect(text(bubble)).toContain("Queued: it will be sent when the current turn ends.");
    expect(q(bubble, '[data-arrow="yellow"]')).not.toBeNull();
    expect(all(bubble, "button")).toHaveLength(0);
  });

  it("shows a refused message as not sent, with the hub's words, and Retry sends it again as a new one", async () => {
    const t = await ready();
    answer(t, 409, { error: { code: "send_rejected", message: "the session cannot take a message now" } });
    await settle(async () => void (await t.store.getState().send({ text: "run it" })));
    const bubble = q(t.el, '[data-send="failed"]') as HTMLElement;
    expect(text(bubble)).toContain("the session cannot take a message now");
    expect(q(bubble, '[data-arrow="red"]')).not.toBeNull();
    expect(button(bubble, "Discard")).toBeDefined();
    expect(t.transport.posts("/message")).toHaveLength(1);

    answer(t, 202, { receipt: "accepted" });
    await click(button(bubble, "Retry"));
    expect(t.transport.posts("/message")).toHaveLength(2);
    expect(t.transport.posts("/message")[1]).toMatchObject({ text: "run it" });
    // The failed one is gone; the new one stands in its place.
    expect(all(t.el, "[data-send]").map((b) => b.getAttribute("data-send"))).toEqual(["sent"]);
  });

  it("shows a send that may have reached the session as in doubt, and never posts it again by itself", async () => {
    const t = await ready();
    answer(t, 503, { error: { code: "keeper_failed", message: "keeper did not answer" } });
    await settle(async () => void (await t.store.getState().send({ text: "ship it" })));
    const bubble = q(t.el, '[data-send="failed"]') as HTMLElement;
    expect(text(bubble)).toContain("may or may not have been sent");
    expect(q(bubble, '[data-arrow="grey"]')).not.toBeNull();
    expect(button(bubble, "Retry")).toBeDefined();
    await settle(() => t.timers.advance(120_000));
    expect(t.transport.posts("/message")).toHaveLength(1);
  });

  it("offers no action while the outbox loads what the device kept, and says why", async () => {
    const t = await ready();
    const entry = {
      commandId: "w-1",
      session: KEY,
      text: "kept from before",
      attachments: [],
      mode: "queue" as const,
      createdAt: 1,
      status: "failed" as const,
      attempts: 1,
      error: "Not sent",
    };
    const outbox = { entries: [entry], persistent: true, note: "", paused: null, inFlight: null, owner: "me", loading: true, storageDown: false };
    await settle(() =>
      t.store.setState({
        outbox,
        sends: describeSends({ outbox, connection: t.store.getState().connection, loaded: true, online: true }),
      }),
    );
    const bubble = q(t.el, "[data-send]") as HTMLElement;
    expect(text(bubble)).toContain("Loading the sends kept on this device.");
    expect(all(bubble, "button")).toHaveLength(0);
    // Not red: nothing about it is settled while it loads.
    expect(q(bubble, '[data-arrow="red"]')).toBeNull();
  });

  it("says when the device's storage does not answer, and that nothing is sent meanwhile", async () => {
    const t = await ready();
    const entry = {
      commandId: "w-2",
      session: KEY,
      text: "do not lose me",
      attachments: [],
      mode: "queue" as const,
      createdAt: 1,
      status: "waiting" as const,
      attempts: 0,
    };
    const outbox = { entries: [entry], persistent: false, note: "IndexedDB failed (lost)", paused: null, inFlight: null, owner: "me", loading: false, storageDown: true };
    await settle(() =>
      t.store.setState({
        outbox,
        sends: describeSends({ outbox, connection: t.store.getState().connection, loaded: true, online: true }),
      }),
    );
    expect(text(q(t.el, '[data-send="waiting"]'))).toContain("sending now could send it twice");
    expect(text(q(t.el, "[data-outbox-note]"))).toContain("IndexedDB failed");
  });

  it("shows a delivered message once: the timeline's own, not the outbox's, while the entry waits for its result", async () => {
    const t = await ready();
    answer(t, 202, { receipt: "accepted" });
    await settle(async () => void (await t.store.getState().send({ text: "one more thing" })));
    const entry = t.store.getState().sends[0];
    expect(all(t.el, "[data-send]")).toHaveLength(1);

    // The journal now has the user's message, sent under that command id.
    const turn = (type: string, seq: number, data: unknown, extra: object = {}): Frame =>
      ({ v: 2, type, session: KEY, seq, ts: 1760000200, data, ...extra }) as unknown as Frame;
    await settle(() =>
      void t.transport.last.send(
        turn("turn.started", 40, {}, { turn_id: "t9", command_id: entry?.commandId }),
        turn("message.completed", 41, { id: "m9", kind: "user", text: "one more thing" }, { turn_id: "t9", command_id: entry?.commandId }),
      ),
    );
    // The entry is still the outbox's (no result yet) but the view draws the message once.
    expect(t.store.getState().sends).toHaveLength(1);
    expect(all(t.el, "[data-send]")).toHaveLength(0);
    expect(all(t.el, '[data-bubble="user"]').filter((b) => text(b) === "one more thing")).toHaveLength(1);

    await settle(() =>
      void t.transport.last.send(turn("command.result", 42, { op: "send", ok: true }, { command_id: entry?.commandId })),
    );
    expect(t.store.getState().sends).toEqual([]);
    expect(all(t.el, '[data-bubble="user"]').filter((b) => text(b) === "one more thing")).toHaveLength(1);
  });
});

describe("what a reply's text cannot do", () => {
  it("draws markup and unsafe links as text", async () => {
    const t = setup();
    const body = loadFixture("default").snapshots[0]?.body as Fixture["snapshots"][number]["body"];
    const reply = (text: string): Frame =>
      ({ v: 2, type: "message.completed", session: KEY, seq: 99, ts: 1760000300, turn_id: "t1", data: { id: "mx", kind: "assistant", text } }) as unknown as Frame;
    await settle(() =>
      void t.transport.last.send(
        snapshotFrame({ ...body, head: 98 }),
        { v: 2, type: "session.state", session: KEY, seq: 0, ts: 1, data: { state: "idle", live: [], pending: [], head: 98, epoch: 0 } } as Frame,
        reply('<img src=x onerror="alert(1)"> and [click](javascript:alert(1)) and [ok](https://example.test/a)'),
      ),
    );
    const prose = all(t.el, ".sv-prose").find((p) => text(p).includes("onerror")) as HTMLElement;
    expect(q(prose, "img")).toBeNull();
    expect(text(prose)).toContain('<img src=x onerror="alert(1)">');
    const links = all(prose, "a");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://example.test/a"]);
    expect(links[0]?.getAttribute("rel")).toBe("noopener noreferrer");
  });
});

describe("reading history", () => {
  it("offers a way back to the newest message once the person scrolls up, and takes them there", async () => {
    const t = setup();
    await play(t, "default");
    const scroller = q(t.el, 'main[aria-label="Timeline"]') as HTMLElement;
    expect(button(t.el, "Jump to latest")).toBeUndefined();
    Object.defineProperty(scroller, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(scroller, "clientHeight", { configurable: true, value: 400 });
    scroller.scrollTop = 100;
    await settle(() => void scroller.dispatchEvent(new Event("scroll")));
    const jump = button(t.el, "Jump to latest");
    expect(jump).toBeDefined();
    await click(jump);
    expect(scroller.scrollTop).toBe(2000);
  });

  it("loads earlier messages on request, and says so while it does", async () => {
    const t = setup();
    const body = loadFixture("default").snapshots[0]?.body as Fixture["snapshots"][number]["body"];
    await settle(() => void t.transport.last.send(snapshotFrame({ ...body, has_more: true, next_before: 4 })));
    let finish: (r: { status: number; body: unknown }) => void = () => {};
    t.transport.on("/messages", () => new Promise((resolve) => (finish = resolve)));
    const more = button(t.el, "Load earlier messages") as HTMLButtonElement;
    expect(more).toBeDefined();
    await click(more);
    expect(t.transport.calls.some((c) => c.path.includes("/messages") && c.path.includes("before=4"))).toBe(true);
    expect(button(t.el, "Loading")?.disabled).toBe(true);
    await settle(() => finish({ status: 200, body: { session: KEY, messages: [], has_more: false, next_before: null } }));
    expect(button(t.el, "Load earlier messages")).toBeUndefined();
  });
});
