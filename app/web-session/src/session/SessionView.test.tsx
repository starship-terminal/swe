import { describe, expect, it } from "vitest";

import { describeSends, type Frame } from "../core";
import { loadFixture, snapshotFrame, type Fixture } from "../core/test-support";
import { all, button, click, frames, play, q, setup, settle, text, KEY } from "./test-harness";

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

describe("a long request in the timeline", () => {
  it("is cut like any text from outside, and says how much it left out", async () => {
    const t = setup();
    const prompt = "Run `ls` ".repeat(4000);
    await settle(() => void t.transport.last.send(frame("turn.started", 1, {}, { turn_id: "t1" }), frame("approval.requested", 2, { request_id: "r1", kind: "command", prompt }, { turn_id: "t1" })));
    const row = q(t.el, '[data-request="open"]') as HTMLElement;
    expect(row.querySelector("p.whitespace-pre-wrap")?.textContent?.length).toBeLessThanOrEqual(6000);
    expect(text(row)).toContain(`${(prompt.length - 6000).toLocaleString("en-US")} more characters not shown.`);
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
    expect(text(bubble)).toContain("the session cannot take a message now.");
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

  it("says why when a Retry is refused, under that send, and clears it on the next try", async () => {
    const t = await ready();
    answer(t, 409, { error: { code: "send_rejected", message: "no" } });
    await settle(async () => void (await t.store.getState().send({ text: "run it" })));
    const refusal = { ok: false as const, reason: "read_only" as const, message: "This session is read-only now." };
    await settle(() => t.store.setState({ retrySend: async () => refusal }));
    await click(button(q(t.el, '[data-send="failed"]') as HTMLElement, "Retry"));
    const bubble = q(t.el, '[data-send="failed"]') as HTMLElement;
    expect(text(q(bubble, '[role="alert"]'))).toBe("This session is read-only now.");
    expect(button(bubble, "Retry")).toBeDefined();
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

  it("shows a keeper-only repeat as waiting, not as failed: the hub may have it, and it goes again under the same id (G31)", async () => {
    const t = await ready();
    const entry = {
      commandId: "w-3",
      session: KEY,
      text: "ship it",
      attachments: [],
      mode: "queue" as const,
      createdAt: 1,
      status: "waiting" as const,
      attempts: 1,
      ambiguous: true,
      keeperOnlyAt: 1,
    };
    const show = async (over: object) => {
      const outbox = { entries: [{ ...entry, ...over }], persistent: true, note: "", paused: null, inFlight: null, owner: "me", loading: false, storageDown: false };
      await settle(() => t.store.setState({ outbox, sends: describeSends({ outbox, connection: t.store.getState().connection, loaded: true, online: true }) }));
      return q(t.el, '[data-send="waiting"]') as HTMLElement;
    };
    const quiet = await show({});
    expect(text(quiet)).toContain("The hub may have this message already. It will be sent again under the same id, which the hub never delivers twice.");
    expect(q(quiet, '[data-arrow="yellow"]')).not.toBeNull();
    // Posted once already: nothing to discard, nothing to retry while it waits.
    expect(all(quiet, "button")).toHaveLength(0);

    const dropped = await show({ lastError: "The connection dropped before the hub answered. Sending it again under the same id, which the hub never delivers twice" });
    expect(text(dropped)).toContain("The connection dropped before the hub answered.");
    expect(text(dropped)).toContain("Trying again.");
    expect(all(dropped, "button")).toHaveLength(0);
  });

  it("shows a send that was not run by a keeper as failed, with the hub's reason and the two actions (G31)", async () => {
    const t = await ready();
    const outbox = {
      entries: [{ commandId: "w-4", session: KEY, text: "ship it", attachments: [], mode: "queue" as const, createdAt: 1, status: "failed" as const, attempts: 1, ambiguous: true, error: "The session was not run by a session keeper when this was sent" }],
      persistent: true, note: "", paused: null, inFlight: null, owner: "me", loading: false, storageDown: false,
    };
    await settle(() => t.store.setState({ outbox, sends: describeSends({ outbox, connection: t.store.getState().connection, loaded: true, online: true }) }));
    const bubble = q(t.el, '[data-send="failed"]') as HTMLElement;
    expect(text(bubble)).toContain("The session was not run by a session keeper when this was sent.");
    expect(button(bubble, "Retry")).toBeDefined();
    expect(button(bubble, "Discard")).toBeDefined();
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

describe("what a refactor of the rows must not change", () => {
  it("keeps a person's open group open when older history joins it at the front", async () => {
    const t = setup();
    const body = loadFixture("default").snapshots[0]?.body as Fixture["snapshots"][number]["body"];
    const tool = (seq: number, id: string): Frame[] =>
      [
        { v: 2, type: "tool.started", session: KEY, seq, ts: 1760000000 + seq, turn_id: "t1", data: { call_id: id, name: "Read", summary: `Read ${id}` } },
        { v: 2, type: "tool.completed", session: KEY, seq: seq + 1, ts: 1760000000 + seq, turn_id: "t1", data: { call_id: id, status: "ok", name: "Read" } },
      ] as unknown as Frame[];
    const page = (seqs: number[]) => ({ ...body, messages: seqs.flatMap((seq) => tool(seq, `c${seq}`)) as never, has_more: seqs[0] !== 10, next_before: 10, state: "idle" as const, open_turn: null, head: 40 });
    await settle(() => void t.transport.last.send(snapshotFrame(page([20, 22]))));
    await click(button(t.el, "Worked"));
    expect(button(t.el, "Worked")?.getAttribute("aria-expanded")).toBe("true");
    t.transport.on("/messages", () => ({ status: 200, body: { session: KEY, messages: [...tool(10, "c10"), ...tool(12, "c12")], has_more: false, next_before: null } }));
    await click(button(t.el, "Load earlier messages"));
    // One group, now with four steps, still open.
    expect(all(t.el, "button").filter((b) => text(b).startsWith("Worked"))).toHaveLength(1);
    expect(button(t.el, "Worked")?.getAttribute("aria-expanded")).toBe("true");
    expect(text(q(t.el, ".sv-collapse"))).toContain("Read c10");
  });

  it("draws a reply that is only live text, with no turn and nothing in the journal yet", async () => {
    const t = setup();
    const body = loadFixture("default").snapshots[0]?.body as Fixture["snapshots"][number]["body"];
    await settle(() =>
      void t.transport.last.send(
        snapshotFrame({ ...body, messages: [], state: "running", open_turn: null }),
        { v: 2, type: "session.state", session: KEY, seq: 0, ts: 1, data: { state: "running", live: [{ id: "a1", kind: "assistant", text: "Starting up" }], pending: [], head: body.head, epoch: body.epoch } } as unknown as Frame,
      ),
    );
    expect(text(q(t.el, '[data-live="assistant"]'))).toBe("Starting up");
  });

  it("names the option a person chose for a question", async () => {
    const t = setup();
    const body = loadFixture("default").snapshots[0]?.body as Fixture["snapshots"][number]["body"];
    const f = (type: string, seq: number, data: unknown): Frame => ({ v: 2, type, session: KEY, seq, ts: 1760000000 + seq, turn_id: "t1", data }) as unknown as Frame;
    await settle(() =>
      void t.transport.last.send(
        snapshotFrame({ ...body, messages: [], head: 49, state: "running", open_turn: "t1" }),
        { v: 2, type: "session.state", session: KEY, seq: 0, ts: 1, data: { state: "running", live: [], pending: [], head: 49, epoch: body.epoch } } as unknown as Frame,
        f("approval.requested", 50, { request_id: "q1", kind: "question", prompt: "Which branch?", options: [{ id: "o1", label: "main" }, { id: "o2", label: "dev" }] }),
        f("approval.resolved", 51, { request_id: "q1", source: "user", option_id: "o2" }),
      ),
    );
    expect(text(q(t.el, '[data-request="closed"]'))).toContain("Chose: dev");
  });
});

// ---------------------------------------------------------------------
// The details of a work group, built by hand: the recordings hold no long outputs, stuck tools or open turns.

import { bodyOf, frame } from "./test-harness";

/** A session of idle history made of these timeline events (and an open turn, when named). */
async function history(events: Frame[], open: string | null = null) {
  const t = setup();
  const head = 200;
  await settle(
    () =>
      void t.transport.last.send(
        snapshotFrame({ ...bodyOf("default"), messages: events as never, state: open === null ? "idle" : "running", open_turn: open, head }),
        frame("session.state", 0, { state: open === null ? "idle" : "running", live: [], pending: [], head, epoch: 0 }),
      ),
  );
  return t;
}

const toolCall = (seq: number, over: { input?: unknown; output?: string; status?: string; edits?: object[]; running?: boolean } = {}): Frame[] => {
  const started = frame("tool.started", seq, { call_id: `c${seq}`, name: "Bash", summary: `step ${seq}`, ...(over.input === undefined ? {} : { input: over.input }) }, { turn_id: "t1" });
  if (over.running === true) return [started];
  return [
    started,
    frame("tool.completed", seq + 1, { call_id: `c${seq}`, status: over.status ?? "ok", name: "Bash", output: over.output ?? "", ...(over.edits === undefined ? {} : { file_edits: over.edits }) }, { turn_id: "t1" }),
  ];
};

async function openAll(t: ReturnType<typeof setup>, step: string) {
  await click(button(t.el, "Worked"));
  await click(all(t.el, "button").find((b) => text(b).includes(step)));
  return q(t.el, ".sv-collapse") as HTMLElement;
}

describe("long text in a step", () => {
  it("cuts an output at 6000 characters and says how much is not shown", async () => {
    const t = await history(toolCall(10, { output: "x".repeat(7000) }));
    const panel = await openAll(t, "step 10");
    expect(text(panel)).toContain("1000 more characters not shown.");
    expect(all(panel, "pre code").find((c) => text(c).startsWith("xxx"))?.textContent).toHaveLength(6000);
  });

  it("cuts an output at 300 lines, whatever its length", async () => {
    const t = await history(toolCall(10, { output: Array.from({ length: 400 }, (_, i) => `line ${i}`).join("\n") }));
    const panel = await openAll(t, "step 10");
    const shown = all(panel, "pre code").find((c) => text(c).startsWith("line 0")) as HTMLElement;
    expect((shown.textContent ?? "").split("\n")).toHaveLength(300);
    expect(text(panel)).toMatch(/\d+ more characters not shown\./);
  });

  it("cuts an input the same way", async () => {
    const t = await history(toolCall(10, { input: "y".repeat(9000) }));
    const panel = await openAll(t, "step 10");
    expect(text(panel)).toContain("3000 more characters not shown.");
  });
});

describe("a diff in a step", () => {
  const edit = (diff: string) => [{ path: "src/app.py", kind: "modify", diff }];
  const diffSpans = (panel: HTMLElement) => all(panel, "pre code span");

  it("has no blank row from its trailing newline", async () => {
    const t = await history(toolCall(10, { edits: edit("@@ -1 +1 @@\n-a\n+b\n") }));
    const panel = await openAll(t, "step 10");
    expect(diffSpans(panel).map((s) => s.textContent)).toEqual(["@@ -1 +1 @@", "-a", "+b"]);
  });

  it("cuts at 300 lines, and says so", async () => {
    const lines = Array.from({ length: 400 }, (_, i) => `+line ${i}`);
    const t = await history(toolCall(10, { edits: edit(`@@ -0,0 +1,400 @@\n${lines.join("\n")}`) }));
    const panel = await openAll(t, "step 10");
    expect(diffSpans(panel)).toHaveLength(300);
    expect(text(panel)).toMatch(/\d+ more characters not shown\./);
  });

  it("cuts a diff that is one very long line, by characters", async () => {
    const t = await history(toolCall(10, { edits: edit(`@@ -1 +1 @@\n-${"a".repeat(10_000)}\n+b`) }));
    const panel = await openAll(t, "step 10");
    const total = diffSpans(panel).reduce((sum, s) => sum + (s.textContent ?? "").length, 0);
    expect(total).toBeLessThanOrEqual(6000 + 2);
    expect(text(panel)).toMatch(/\d{4} more characters not shown\./);
  });
});

describe("a group that is still going", () => {
  it("starts open while its turn runs, and closes by itself once the turn is done", async () => {
    const t = await history([frame("turn.started", 9, {}, { turn_id: "t1" }), ...toolCall(10, { running: true })], "t1");
    const header = () => all(t.el, "button").find((b) => text(b).startsWith("Working")) as HTMLButtonElement;
    expect(header().getAttribute("aria-expanded")).toBe("true");
    expect(text(q(t.el, ".sv-collapse"))).toContain("step 10");

    await settle(
      () =>
        void t.transport.last.send(
          frame("tool.completed", 11, { call_id: "c10", status: "ok", name: "Bash" }, { turn_id: "t1" }),
          frame("message.completed", 12, { id: "done", kind: "assistant", text: "Done." }, { turn_id: "t1" }),
          frame("turn.ended", 13, { status: "completed", duration_s: 5 }, { turn_id: "t1" }),
        ),
    );
    const done = all(t.el, "button").find((b) => text(b).startsWith("Worked")) as HTMLButtonElement;
    expect(done.getAttribute("aria-expanded")).toBe("false");
  });

  it("keeps a running group closed once the person closed it, even as it grows", async () => {
    const t = await history([frame("turn.started", 9, {}, { turn_id: "t1" }), ...toolCall(10, { running: true })], "t1");
    const header = () => all(t.el, "button").find((b) => /^(Working|Worked)/.test(text(b))) as HTMLButtonElement;
    expect(header().getAttribute("aria-expanded")).toBe("true");
    await click(header());
    expect(header().getAttribute("aria-expanded")).toBe("false");
    await settle(() => void t.transport.last.send(...toolCall(12, { running: true })));
    expect(header().getAttribute("aria-expanded")).toBe("false");
    expect(text(header())).toContain("2 tool calls");
  });
});

describe("a step that never finished", () => {
  it("is shown stopped, not spinning, once its turn has ended", async () => {
    const t = await history([
      frame("turn.started", 95, {}, { turn_id: "t1" }),
      ...toolCall(96, { running: true }),
      frame("turn.ended", 97, { status: "interrupted" }, { turn_id: "t1" }),
    ]);
    await click(button(t.el, "Worked"));
    const row = all(t.el, "button").find((b) => text(b).includes("step 96")) as HTMLButtonElement;
    expect(text(row)).toContain("Stopped");
    expect(text(row)).not.toContain("Running");
    expect(row.querySelector(".sv-spin")).toBeNull();
  });

  it("has its own mark, not the cancelled step's", async () => {
    const markOf = (row: HTMLElement) => Array.from(row.querySelectorAll("svg path"), (path) => path.getAttribute("d")).join("|");
    const stoppedTurn = await history([
      frame("turn.started", 95, {}, { turn_id: "t1" }),
      ...toolCall(96, { running: true }),
      frame("turn.ended", 97, { status: "interrupted" }, { turn_id: "t1" }),
    ]);
    await click(button(stoppedTurn.el, "Worked"));
    const stopped = all(stoppedTurn.el, "button").find((b) => text(b).includes("step 96")) as HTMLButtonElement;
    const cancelledTurn = await history([frame("turn.started", 95, {}, { turn_id: "t1" }), ...toolCall(96, { status: "cancelled" }), frame("turn.ended", 98, { status: "completed" }, { turn_id: "t1" })]);
    await click(button(cancelledTurn.el, "Worked"));
    const cancelled = all(cancelledTurn.el, "button").find((b) => text(b).includes("step 96")) as HTMLButtonElement;
    expect(text(cancelled)).toContain("Cancelled");
    expect(markOf(stopped)).toContain("M4.5 4.5h7v7h-7v-7Z");
    expect(markOf(cancelled)).toContain("M4 4 12 12M12 4 4 12");
    expect(markOf(stopped)).not.toBe(markOf(cancelled));
  });

  it("still spins while its turn is running", async () => {
    const t = await history([frame("turn.started", 9, {}, { turn_id: "t1" }), ...toolCall(10, { running: true })], "t1");
    const row = all(t.el, "button").find((b) => text(b).includes("step 10")) as HTMLButtonElement;
    expect(row.querySelector(".sv-spin")).not.toBeNull();
    expect(text(row)).toContain("Running");
  });
});

describe("how the steps read to a screen reader", () => {
  it("makes no landmark of a disclosure panel, and ties each button to its panel", async () => {
    const t = await history([...toolCall(10), ...toolCall(12)]);
    await click(button(t.el, "Worked"));
    expect(all(t.el, '[role="region"]')).toHaveLength(0);
    for (const b of all(t.el, "button[aria-controls]")) {
      const panel = t.el.ownerDocument.getElementById(b.getAttribute("aria-controls") as string);
      expect(panel).not.toBeNull();
      expect(panel?.classList.contains("sv-collapse")).toBe(true);
    }
  });
});

describe("a reply that finishes", () => {
  it("does not enter a second time, and a new row does", async () => {
    const t = setup();
    await play(t, "streaming", 0, 4); // the snapshot, the state frame and two chunks
    expect(q(t.el, '[data-live="assistant"]')).not.toBeNull();
    const live = q(t.el, '[data-row-kind="live"]') as HTMLElement;
    expect(live.classList.contains("sv-enter")).toBe(false); // a live row of the first draw
    await play(t, "streaming", 4);
    const reply = all(t.el, '[data-row-kind="assistant"]')[0] as HTMLElement;
    expect(text(reply)).toBe("Hello world");
    expect(reply.classList.contains("sv-enter")).toBe(false);
    await settle(() => void t.transport.last.send(frame("message.completed", 30, { id: "u2", kind: "user", text: "next" }, { turn_id: "t2" })));
    const next = all(t.el, '[data-row-kind="user"]').at(-1) as HTMLElement;
    expect(next.classList.contains("sv-enter")).toBe(true);
  });
});

describe("the composer's place", () => {
  it("holds the composer (G13's tests are in src/composer), not the placeholder", async () => {
    const t = setup();
    await play(t, "default");
    expect(q(t.el, "form[aria-label='Composer']")).not.toBeNull();
    expect(q(t.el, "[data-placeholder-note]")).toBeNull();
  });

  it("floats the composer over the bottom of the timeline: it reports its height, and the timeline leaves that much padding", async () => {
    const t = setup();
    await play(t, "default");
    const host = q(t.el, "[data-composer-host]")?.parentElement as HTMLElement;
    expect(host.style.getPropertyValue("--sv-composer-h")).not.toBe("");
    expect(q(host, "main")).not.toBeNull(); // the timeline and the composer share the host the height is set on
    expect(q(t.el, '[role="log"]')?.classList.contains("sv-pad-end")).toBe(true);
    expect((q(t.el, "[data-composer-host]") as HTMLElement).classList.contains("absolute")).toBe(true);
  });

  it("draws a send the hub holds in its queue once, in the tray, not as a bubble as well", async () => {
    const t = setup();
    await play(t, "queued");
    await settle(() => void t.store.getState().send({ text: "then lint it" }));
    const sent = t.store.getState().sends[0];
    expect(sent?.shown).not.toBe(undefined);
    await settle(() => void t.transport.last.send(frame("queue.updated", 400, { items: [{ command_id: sent?.commandId ?? "", text: "then lint it", mode: "queue" }] })));
    expect(q(t.el, `[data-send][data-command-id="${sent?.commandId}"]`)).toBeNull();
    await click(q(t.el, "[data-tray] button[aria-controls]"));
    expect(q(t.el, `[data-queued="${sent?.commandId}"]`)).not.toBeNull();
  });
});
