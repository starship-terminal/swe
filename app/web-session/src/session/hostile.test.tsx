// One bad reply must not take the view with it. The markdown reader caps its nesting; and if a row still
// fails to draw, only that row is lost.
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { all, bodyOf, frame, q, setup, settle, text } from "./test-harness";
import { snapshotFrame } from "../core/test-support";
import { RowBoundary, TimelineBoundary } from "./RowBoundary";

vi.mock("../ui/markdown", async (original) => {
  const real = await original<typeof import("../ui/markdown")>();
  return {
    ...real,
    // A reply that breaks the drawing code in a way nobody has found yet.
    parseMarkdown: (words: string) => {
      if (words.includes("BOOM")) throw new RangeError("a reader that failed");
      return real.parseMarkdown(words);
    },
  };
});

const replies = (...messages: [string, string][]) => {
  const body = bodyOf("default");
  return [
    snapshotFrame({ ...body, head: 98 }),
    frame("session.state", 0, { state: "idle", live: [], pending: [], head: 98, epoch: 0 }),
    ...messages.map(([id, words], index) => frame("message.completed", 99 + index, { id, kind: "assistant", text: words }, { turn_id: "t1" })),
  ];
};

/** React logs a caught render error; the tests expect it. */
async function quietly<T>(work: () => Promise<T>): Promise<T> {
  const spy = vi.spyOn(console, "error").mockImplementation(() => {});
  try {
    return await work();
  } finally {
    spy.mockRestore();
  }
}

describe("a hostile reply", () => {
  it("still draws the timeline when a reply is ten thousand quote markers deep", async () => {
    const t = setup();
    await settle(() => void t.transport.last.send(...replies(["mx", ">".repeat(10_000) + " hi"])));
    expect(q(t.el, '[role="log"]')).not.toBeNull();
    expect(text(q(t.el, '[role="log"]'))).toContain("hi");
  });

  it("loses only the row that cannot be drawn, and says so, leaving the rest of the timeline and the header", async () => {
    const t = setup();
    await quietly(() => settle(() => void t.transport.last.send(...replies(["a", "before"], ["b", "BOOM"], ["c", "after"]))));
    const log = q(t.el, '[role="log"]') as HTMLElement;
    expect(text(q(log, '[data-row="failed"]'))).toBe("This item could not be shown.");
    expect(all(log, '[data-row="failed"]')).toHaveLength(1);
    expect(text(log)).toContain("before");
    expect(text(log)).toContain("after");
    expect(text(q(t.el, 'nav[aria-label="Breadcrumb"]'))).toBe("project/hello");
  });
});

function Thrower({ fail }: { fail: boolean }) {
  if (fail) throw new Error("no");
  return <p>fine</p>;
}

describe("the boundaries", () => {
  it("tries a row that failed again when what it shows changes", async () => {
    await quietly(async () => {
      const el = document.createElement("div");
      document.body.append(el);
      const root = createRoot(el);
      await act(async () => root.render(<RowBoundary resetKey="v1"><Thrower fail /></RowBoundary>));
      expect(text(el)).toBe("This item could not be shown.");
      await act(async () => root.render(<RowBoundary resetKey="v1"><Thrower fail={false} /></RowBoundary>));
      expect(text(el)).toBe("This item could not be shown.");
      await act(async () => root.render(<RowBoundary resetKey="v2"><Thrower fail={false} /></RowBoundary>));
      expect(text(el)).toBe("fine");
      await act(async () => root.unmount());
      el.remove();
    });
  });

  it("shows a line in the timeline's place when the timeline itself fails", async () => {
    await quietly(async () => {
      const el = document.createElement("div");
      document.body.append(el);
      const root = createRoot(el);
      await act(async () => root.render(<TimelineBoundary><Thrower fail /></TimelineBoundary>));
      expect(text(q(el, '[data-state="failed"]'))).toContain("The timeline could not be shown");
      await act(async () => root.unmount());
      el.remove();
    });
  });
});
