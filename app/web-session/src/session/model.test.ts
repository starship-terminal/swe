import { describe, expect, it } from "vitest";

import { describeSends, emptySession, reduceAll, type Frame, type SendView, type SessionState } from "../core";
import { loadFixture } from "../core/test-support";
import { buildRows, rowLabels, visibleSends, type Row } from "./model";

function replay(name: string, upTo?: number): SessionState {
  const fixture = loadFixture(name);
  const frames = (fixture.connections[0]?.frames ?? []) as Frame[];
  return reduceAll(emptySession(), upTo === undefined ? frames : frames.slice(0, upTo)).state;
}

const kinds = (rows: Row[]) => rows.map((row) => row.kind);

function send(commandId: string, over: Partial<SendView> = {}): SendView {
  return {
    commandId,
    text: `text of ${commandId}`,
    attachments: [],
    mode: "queue",
    createdAt: 0,
    shown: "sent",
    reason: "Sent. Waiting for the session to take it.",
    canRetry: false,
    canDiscard: false,
    ...over,
  };
}

describe("the rows of the recorded default session", () => {
  const rows = buildRows(replay("default"), []);

  it("puts the steps between prose in work groups, and keeps the prose and the user's words as rows", () => {
    expect(kinds(rows)).toEqual([
      "user", // hello
      "work", // thinking, read, edit, checkpoint, bash, restore
      "assistant", // Done. I changed ...
      "footer", // 1 file
      "user", // once more
      "assistant", // Second reply
    ]);
  });

  it("counts a group's tool calls and thoughts, and its failures", () => {
    const group = rows.find((row) => row.kind === "work");
    if (group?.kind !== "work") throw new Error("no work group");
    expect(group.tools).toBe(3);
    expect(group.thoughts).toBe(1);
    expect(group.failed).toBe(1);
    expect(group.running).toBe(false);
    expect(group.span).toBeGreaterThan(0);
    expect(group.entries.map((entry) => entry.kind)).toEqual(["thinking", "tool", "tool", "checkpoint", "tool", "checkpoint"]);
  });

  it("totals the files a turn's tools changed once, with the lines they added and removed", () => {
    const footer = rows.find((row) => row.kind === "footer");
    if (footer?.kind !== "footer") throw new Error("no footer");
    expect(footer).toMatchObject({ files: 1, added: 1, removed: 1 });
    expect(footer.turn.id).toBe("t1");
  });
});

describe("work that is still going", () => {
  it("marks the last group of an open turn as running, with no span yet", () => {
    // Up to the Bash call's start: its turn is open and nothing was written after it.
    const fixture = loadFixture("default");
    const frames = (fixture.connections[0]?.frames ?? []) as Frame[];
    const at = frames.findIndex((frame) => frame.type === "tool.started" && frame.data.call_id === "call-9");
    const rows = buildRows(reduceAll(emptySession(), frames.slice(0, at + 1)).state, []);
    const last = rows[rows.length - 1];
    expect(last).toMatchObject({ kind: "work", running: true, span: null });
    expect(kinds(rows)).not.toContain("footer");
  });

  it("shows a working row between steps, and not while text streams or a request waits", () => {
    const fixture = loadFixture("default");
    const frames = (fixture.connections[0]?.frames ?? []) as Frame[];
    // After the second turn's user message: the turn is open and nothing else has happened.
    const at = frames.findIndex((frame) => frame.type === "message.completed" && frame.data.id === "m4");
    const rows = buildRows(reduceAll(emptySession(), frames.slice(0, at + 1)).state, []);
    expect(rows[rows.length - 1]?.kind).toBe("working");
    const streaming = frames.findIndex((frame) => frame.type === "message.delta" && frame.data.id === "m5");
    expect(kinds(buildRows(reduceAll(emptySession(), frames.slice(0, streaming + 1)).state, [])).at(-1)).toBe("live");
  });
});

describe("streaming text", () => {
  it("is a live row after its turn's items, and the message replaces it when it completes", () => {
    const fixture = loadFixture("streaming");
    const frames = (fixture.connections[0]?.frames ?? []) as Frame[];
    const third = frames.findIndex((frame) => frame.type === "message.delta" && frame.data.offset > 0);
    const mid = buildRows(reduceAll(emptySession(), frames.slice(0, third + 1)).state, []);
    const live = mid[mid.length - 1];
    expect(live?.kind).toBe("live");
    const done = buildRows(reduceAll(emptySession(), frames).state, []);
    expect(kinds(done)).not.toContain("live");
    expect(done.filter((row) => row.kind === "assistant")).toHaveLength(1);
  });
});

describe("a request in the flow", () => {
  it("is its own row, open until it is answered", () => {
    const fixture = loadFixture("approval");
    const frames = (fixture.connections[0]?.frames ?? []) as Frame[];
    const state = reduceAll(emptySession(), frames).state;
    const rows = buildRows(state, []);
    const requests = rows.filter((row) => row.kind === "request");
    expect(requests.length).toBeGreaterThan(0);
    expect(requests.every((row) => row.kind === "request" && row.item.resolution !== undefined)).toBe(true);
  });
});

describe("failed turns, errors and compaction", () => {
  it("draws notices as their own rows and ends a failed turn with a footer", () => {
    const rows = buildRows(replay("failed_turn"), []);
    expect(kinds(rows)).toContain("notice");
    const footer = rows.find((row) => row.kind === "footer");
    expect(footer).toMatchObject({ kind: "footer", turn: { status: "failed" } });
    const notices = rows.flatMap((row) => (row.kind === "notice" ? [row.item.kind] : []));
    expect(notices).toEqual(expect.arrayContaining(["error", "compacted"]));
  });
});

describe("sends that are not in the timeline yet", () => {
  const state = replay("default");

  it("hides a send whose message the timeline already holds, by its command id", () => {
    // The recorded second turn was sent under command id m2.
    const rows = buildRows(state, [send("m2"), send("another")]);
    const sends = rows.filter((row) => row.kind === "send");
    expect(sends.map((row) => (row.kind === "send" ? row.send.commandId : ""))).toEqual(["another"]);
  });

  it("keeps a send the hub only queued: the tray for those is another milestone's", () => {
    expect(visibleSends([send("q1", { reason: "Queued: it will be sent when the current turn ends." })], state.items)).toHaveLength(1);
  });

  it("shows every unfinished send last, oldest first", () => {
    const rows = buildRows(state, [send("a"), send("b")]);
    expect(kinds(rows).slice(-2)).toEqual(["send", "send"]);
    expect(rows.slice(-2).map((row) => (row.kind === "send" ? row.send.commandId : ""))).toEqual(["a", "b"]);
  });

  it("never offers an action the selector did not", () => {
    const view = describeSends({
      outbox: { entries: [], persistent: true, note: "", paused: null, inFlight: null, owner: "me", loading: false, storageDown: false },
      connection: { state: "live", attempt: 0, since: 0, retryAt: null, detail: "" },
      loaded: true,
      online: true,
    });
    expect(view).toEqual([]);
  });
});

describe("the breadcrumb's words", () => {
  it("names the project by the folder and the title by the row", () => {
    expect(rowLabels(replay("default").row)).toEqual({ project: "project", title: "hello" });
    expect(rowLabels(null)).toEqual({ project: null, title: null });
  });
});
