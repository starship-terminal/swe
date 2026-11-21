// A streamed delta must cost the live row, not the whole history: with thousands of rows, drawing them all
// again for every delta made each one take a third of a second. The rows' components are counted here.
import { describe, expect, it, vi } from "vitest";

import { snapshotFrame } from "../core/test-support";
import { all, bodyOf, frame, q, setup, settle, text } from "./test-harness";

const drawn = vi.hoisted(() => ({ user: 0, assistant: 0, parsed: 0 }));

vi.mock("./Bubbles", async (original) => {
  const real = await original<typeof import("./Bubbles")>();
  return {
    ...real,
    UserMessage: (props: Parameters<typeof real.UserMessage>[0]) => {
      drawn.user++;
      return real.UserMessage(props);
    },
    AssistantMessage: (props: Parameters<typeof real.AssistantMessage>[0]) => {
      drawn.assistant++;
      return real.AssistantMessage(props);
    },
  };
});

vi.mock("../ui/markdown", async (original) => {
  const real = await original<typeof import("../ui/markdown")>();
  return {
    ...real,
    parseMarkdown: (words: string) => {
      drawn.parsed++;
      return real.parseMarkdown(words);
    },
  };
});

describe("one streamed delta in a long session", () => {
  it("draws the live row and no other: no history row is drawn or parsed again", async () => {
    const t = setup();
    const body = bodyOf("streaming");
    const history = Array.from({ length: 400 }, (_, i) =>
      frame("message.completed", 10 + i, { id: `h${i}`, kind: i % 2 === 0 ? "user" : "assistant", text: i % 2 === 0 ? `question ${i}` : `Answer **${i}** with \`code\`.` }, { turn_id: `t${i >> 1}` }),
    );
    await settle(() =>
      void t.transport.last.send(
        snapshotFrame({ ...body, live: [], open_turn: "tlive", messages: history as never, head: 600 }),
        frame("session.state", 0, { state: "running", live: [], pending: [], head: 600, epoch: 0 }),
      ),
    );
    expect(all(t.el, "[data-row-kind]").length).toBeGreaterThanOrEqual(400);
    const delta = (n: number) => frame("message.delta", undefined, { id: "z", kind: "assistant", text: "ab", seq: n + 1, offset: n * 2, done: false }, { turn_id: "tlive" });
    await settle(() => void t.transport.last.send(delta(0)));
    const before = { ...drawn };
    await settle(() => void t.transport.last.send(delta(1)));
    await settle(() => void t.transport.last.send(delta(2)));
    expect(drawn.user - before.user).toBe(0);
    expect(drawn.assistant - before.assistant).toBe(0);
    // Each delta read the live text once, and nothing else.
    expect(drawn.parsed - before.parsed).toBe(2);
    expect(text(q(t.el, '[data-live="assistant"]'))).toBe("ababab");
  });

  it("still draws a row whose item changed", async () => {
    const t = setup();
    const body = bodyOf("streaming");
    await settle(() =>
      void t.transport.last.send(
        snapshotFrame({ ...body, live: [], messages: [frame("message.completed", 10, { id: "h0", kind: "assistant", text: "first" }, { turn_id: "t1" })] as never, head: 11 }),
        frame("session.state", 0, { state: "idle", live: [], pending: [], head: 11, epoch: 0 }),
      ),
    );
    const base = drawn.assistant;
    await settle(() => void t.transport.last.send(frame("message.completed", 12, { id: "h1", kind: "assistant", text: "second" }, { turn_id: "t1" })));
    // The new row drew; the old one did not.
    expect(drawn.assistant - base).toBe(1);
    expect(all(t.el, ".sv-prose").map(text)).toEqual(["first", "second"]);
  });
});
