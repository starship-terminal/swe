import { describe, expect, it } from "vitest";

import { emptySession, reduceAll, type Frame, type SendView, type SessionState, type TimelineItem, type TurnInfo } from "../core";
import { loadFixture } from "../core/test-support";
import { subject } from "./Timeline";
import { buildRows, rowIdentity, rowLabels, rowsEqual, visibleSends, type Row, type TimelineInput } from "./model";

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
});

describe("the breadcrumb's words", () => {
  it("names the project by the folder and the title by the row", () => {
    expect(rowLabels(replay("default").row)).toEqual({ project: "project", title: "hello" });
    expect(rowLabels(null)).toEqual({ project: null, title: null });
  });
});

// ---------------------------------------------------------------------

const tool = (seq: number, turnId: string, over: object = {}): TimelineItem =>
  ({ kind: "tool", seq, ts: seq, turnId, callId: `c${seq}`, name: "Bash", status: "ok", ...over }) as unknown as TimelineItem;
const msg = (seq: number, turnId: string, role: "user" | "assistant"): TimelineItem =>
  ({ kind: "message", seq, ts: seq, turnId, id: `m${seq}`, role, text: "x" }) as unknown as TimelineItem;
const input = (items: TimelineItem[], over: Partial<TimelineInput> = {}, turns: TurnInfo[] = []): TimelineInput => ({
  items,
  live: [],
  turns,
  stopped: [],
  openTurn: null,
  phase: "idle",
  ...over,
});
const groups = (rows: Row[]) => rows.flatMap((row) => (row.kind === "work" ? [row] : []));

describe("work group ids", () => {
  it("never gives two groups the same key, even when one turn's runs are split by another turn's item", () => {
    const rows = buildRows(input([tool(1, "t1"), msg(2, "t2", "assistant"), tool(3, "t1")]), []);
    const keys = groups(rows).map((row) => row.key);
    expect(keys).toHaveLength(2);
    expect(new Set(keys).size).toBe(2);
  });

  it("lists the seq of every entry, so a person's open choice is found again when the group grows at either end", () => {
    const small = groups(buildRows(input([tool(5, "t1"), tool(6, "t1"), msg(7, "t1", "assistant")]), []))[0];
    const grown = groups(buildRows(input([msg(3, "t1", "user"), tool(4, "t1"), tool(5, "t1"), tool(6, "t1"), tool(7, "t1"), msg(8, "t1", "assistant")]), []))[0];
    expect(small?.seqs).toEqual([5, 6]);
    expect(grown?.seqs).toEqual([4, 5, 6, 7]);
    // They share entries, which is what keeps the choice.
    expect(grown?.seqs.some((seq) => small?.seqs.includes(seq))).toBe(true);
  });
});

describe("a group whose turn ended", () => {
  it("is marked ended, with a step that never finished, and not running", () => {
    const stuck = tool(3, "t1", { status: "running" });
    const turn: TurnInfo = { id: "t1", status: "interrupted" };
    const [group] = groups(buildRows(input([msg(2, "t1", "user"), stuck], {}, [turn]), []));
    expect(group).toMatchObject({ ended: true, running: false });
    expect(group?.entries[0]).toMatchObject({ status: "running" });
  });

  it("is not marked ended while its turn is open, and then runs", () => {
    const [group] = groups(buildRows(input([tool(3, "t1", { status: "running" })], { openTurn: "t1", phase: "running" }, [{ id: "t1" }]), []));
    expect(group).toMatchObject({ ended: false, running: true, span: null });
  });
});

describe("the turn footer's files", () => {
  const edit = (path: string, diff: string) => ({ path, kind: "modify", diff });
  it("counts a file once however many times its tools edited it, and adds up every edit's lines", () => {
    const items = [
      tool(2, "t1", { fileEdits: [edit("a.py", "@@ -1 +1 @@\n-a\n+b")] }),
      tool(3, "t1", { fileEdits: [edit("a.py", "@@ -1 +1,2 @@\n-b\n+c\n+d"), edit("b.py", "@@ -1 +1 @@\n-x\n+y")] }),
    ];
    const rows = buildRows(input(items, {}, [{ id: "t1", status: "completed", endedAt: 9 }]), []);
    expect(rows.find((row) => row.kind === "footer")).toMatchObject({ files: 2, added: 4, removed: 3 });
  });

  it("has no footer for a turn that changed nothing and ended well", () => {
    expect(kinds(buildRows(input([tool(2, "t1")], {}, [{ id: "t1", status: "completed", endedAt: 9 }]), []))).not.toContain("footer");
  });
});

describe("the working row", () => {
  it("is not shown while a request waits for an answer", () => {
    const fixture = loadFixture("approval");
    const frames = (fixture.connections[0]?.frames ?? []) as Frame[];
    const asked = frames.findIndex((frame) => frame.type === "input.requested");
    const state = reduceAll(emptySession(), frames.slice(0, asked + 1)).state;
    // The turn is open, the session is running (not yet "waiting"), and the request is open.
    const rows = buildRows({ ...state, phase: "running" }, []);
    expect(rows.some((row) => row.kind === "request" && row.item.resolution === undefined)).toBe(true);
    expect(kinds(rows)).not.toContain("working");
    // The same session with the request answered is working again.
    const answered = reduceAll(emptySession(), frames.slice(0, frames.findIndex((frame) => frame.type === "approval.resolved" && frame.data.request_id === "r2") + 1)).state;
    expect(kinds(buildRows({ ...answered, phase: "running" }, [])).at(-1)).toBe("working");
  });
});

describe("rows that draw the same", () => {
  const live = { id: "a1", kind: "assistant", text: "Hel", length: 3, turnId: "t1" } as const;

  it("are equal when what they hold is the same object, and different when it is not", () => {
    const items = [msg(2, "t1", "user"), tool(3, "t1"), msg(4, "t1", "assistant")];
    const a = buildRows(input(items), []);
    const b = buildRows(input(items), []);
    expect(a).toHaveLength(b.length);
    a.forEach((row, index) => expect(rowsEqual(row, b[index] as Row)).toBe(true));
    const changed = buildRows(input([msg(2, "t1", "user"), tool(3, "t1", { status: "error" }), msg(4, "t1", "assistant")]), []);
    expect(rowsEqual(a[1] as Row, changed[1] as Row)).toBe(false);
    expect(rowsEqual(a[0] as Row, changed[0] as Row)).toBe(false); // a new item object, even with the same words
  });

  it("see a delta in the live row and nowhere else", () => {
    const items = [msg(2, "t1", "user"), msg(4, "t1", "assistant")];
    const before = buildRows(input(items, { live: [live], openTurn: "t1" }), []);
    const after = buildRows(input(items, { live: [{ ...live, text: "Hello", length: 5 }], openTurn: "t1" }), []);
    const differing = before.filter((row, index) => !rowsEqual(row, after[index] as Row)).map((row) => row.kind);
    expect(differing).toEqual(["live"]);
  });

  it("see a send change when its reason does", () => {
    const one = buildRows(input([]), [send("a")]);
    expect(rowsEqual(one[0] as Row, buildRows(input([]), [send("a")])[0] as Row)).toBe(true);
    expect(rowsEqual(one[0] as Row, buildRows(input([]), [send("a", { reason: "Other." })])[0] as Row)).toBe(false);
  });
});

describe("a reply's identity", () => {
  it("is the same while it streams and after it completes, and a different reply is another", () => {
    const live = buildRows(input([], { live: [{ id: "a1", kind: "assistant", text: "x", length: 1, turnId: "t1" }] }), [])[0] as Row;
    const done = buildRows(input([{ ...(msg(5, "t1", "assistant") as object), id: "a1" } as unknown as TimelineItem]), [])[0] as Row;
    expect(live.kind).toBe("live");
    expect(done.kind).toBe("assistant");
    expect(rowIdentity(live)).toBe(rowIdentity(done));
    expect(live.key).not.toBe(done.key);
    const other = buildRows(input([msg(6, "t1", "assistant")]), [])[0] as Row;
    expect(rowIdentity(other)).not.toBe(rowIdentity(live));
  });
});

describe("the reset key of a row's boundary", () => {
  it("changes when any step of a work group changes, not only the last one", () => {
    const first = tool(3, "t1");
    const middle = tool(4, "t1");
    const last = tool(5, "t1");
    const same = (items: TimelineItem[]) => groups(buildRows(input(items), []))[0] as Row;
    const a = subject(same([first, middle, last]));
    expect(subject(same([first, middle, last]))).toBe(a);
    expect(subject(same([first, tool(4, "t1", { status: "error" }), last]))).not.toBe(a);
    expect(subject(same([first, middle, last, tool(6, "t1")]))).not.toBe(a);
  });
});
