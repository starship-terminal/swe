// What an agent can put in a request or a queued message must cost no more than a few hundred nodes, and
// never change its letters, take the page down, or take down more than its own part.
import { describe, expect, it, vi } from "vitest";

import { all, btn, click, feed, frame, mount, play, q, settle, text, type Mounted } from "./harness";

vi.mock("./text", async (original) => {
  const real = await original<typeof import("./text")>();
  return {
    ...real,
    clip: (words: string, ...rest: number[]) => {
      if (words.includes("BOOM")) throw new RangeError("a drawing that failed");
      return real.clip(words, ...rest);
    },
  };
});

const NASTY = '<img src=x onerror="alert(1)"> **not bold** [a link](javascript:alert(1)) ';
const dock = (m: Mounted) => q(m.el, "[data-dock]") as HTMLElement;

async function ask(m: Mounted, data: object, tool?: object): Promise<void> {
  await play(m, "approval", 0, 4); // r1 answered; running
  await feed(m, ...(tool === undefined ? [] : [frame("tool.started", 14, tool, { turn_id: "t1" })]), frame("approval.requested", 15, { request_id: "r9", kind: "command", ...data }, { turn_id: "t1" }));
}

describe("a hostile request", () => {
  it("draws a megabyte of prompt as one cut text, reading marks in its first part only, and makes no markup from it", async () => {
    const m = mount();
    const prompt = NASTY.repeat(15_000);
    await ask(m, { prompt });
    const shown = q(dock(m), '[data-untrusted="prompt"]') as HTMLElement;
    // The part past the inline reader's reach is the text as it arrived, every letter of it.
    expect(shown.textContent?.endsWith(prompt.slice(2000, 6000))).toBe(true);
    expect(text(dock(m))).toContain(`${(prompt.length - 6000).toLocaleString("en-US")} more characters not shown.`);
    expect(all(dock(m), "*").length).toBeLessThan(200);
    expect(q(dock(m), "img")).toBeNull();
    expect(q(dock(m), "a")).toBeNull();
  });

  it("keeps every letter of a prompt with no marks in it", async () => {
    const m = mount();
    const prompt = "Delete build/ and dist/ (y/n)? ".repeat(1000);
    await ask(m, { prompt });
    expect((q(dock(m), '[data-untrusted="prompt"]') as HTMLElement).textContent).toBe(prompt.slice(0, 6000));
  });

  it("makes at most about a thousand nodes of a prompt that is all marks", async () => {
    const m = mount();
    await ask(m, { prompt: "`a`".repeat(5000) });
    expect(all(dock(m), "*").length).toBeLessThan(1500);
  });

  it("cuts a prompt of many short lines at 300 lines", async () => {
    const m = mount();
    await ask(m, { prompt: Array.from({ length: 2000 }, (_, i) => `line ${i}`).join("\n") });
    const shown = (q(dock(m), '[data-untrusted="prompt"]') as HTMLElement).textContent as string;
    expect(shown.split("\n")).toHaveLength(300);
  });

  it("draws at most twenty options, each cut, and says how many it left out", async () => {
    const m = mount();
    const options = Array.from({ length: 1000 }, (_, i) => ({ id: `row-${i}`, label: `${i}: ${"y".repeat(100_000)}` }));
    await ask(m, { prompt: "pick", options });
    const buttons = all(dock(m), "button[data-option]");
    expect(buttons).toHaveLength(20);
    expect(Math.max(...buttons.map((b) => (b.textContent ?? "").length))).toBeLessThanOrEqual(303);
    expect(text(dock(m))).toContain("980 more options are not shown.");
    expect(all(dock(m), "*").length).toBeLessThan(100);
  });

  it("answers an option by its place in the full list, even with the list cut", async () => {
    const m = mount();
    m.transport.on("/approval", () => ({ status: 200, body: { receipt: "accepted" } }), "POST");
    await ask(m, { prompt: "pick", options: Array.from({ length: 30 }, (_, i) => ({ id: `row-${i}`, label: `row ${i}` })) });
    await click(btn(m, "row 19"));
    expect(m.transport.posts("/approval")[0]).toMatchObject({ request_id: "r9", decision: "20" });
  });

  it("shows a tool's input only when it is opened, as one cut text", async () => {
    const m = mount();
    const input = { command: NASTY.repeat(5000), env: { A: "b".repeat(100_000) } };
    await ask(m, { prompt: "run it", call_id: "call-9" }, { call_id: "call-9", name: "Bash".repeat(500), summary: "x", input });
    expect(text(dock(m)).startsWith("Approval needed")).toBe(true);
    await click(btn(m, `Details: ${"Bash".repeat(30)}…`));
    const pre = all(dock(m), "pre");
    expect(pre).toHaveLength(1);
    expect((pre[0] as HTMLElement).textContent?.length).toBeLessThanOrEqual(6000);
    expect(pre[0]?.querySelector("*")?.tagName).toBe("CODE");
    expect(all(dock(m), "*").length).toBeLessThan(60);
  });

  it("survives an input too deep to write out", async () => {
    const m = mount();
    let deep: unknown = "end";
    for (let i = 0; i < 100_000; i++) deep = [deep];
    await ask(m, { prompt: "deep", call_id: "call-9" }, { call_id: "call-9", name: "Deep", summary: "deep call", input: deep });
    expect(text(dock(m))).toContain("deep");
    expect(dock(m)).not.toBeNull();
  });

  it("loses only the dock when it cannot be drawn, and says so", async () => {
    const m = mount();
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    await ask(m, { prompt: "BOOM" });
    spy.mockRestore();
    expect(text(q(m.el, '[data-composer-failed="dock"]'))).toBe("This request could not be shown. Answer it in the terminal or on another device.");
    expect(q(m.el, "form[aria-label='Composer']")).not.toBeNull();
    expect(q(m.el, "textarea")).not.toBeNull();
  });

  it("draws the next request after one that failed", async () => {
    const m = mount();
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    await ask(m, { prompt: "BOOM" });
    await feed(m, frame("approval.resolved", 16, { request_id: "r9", source: "user", decision: "deny" }, { turn_id: "t1" }), frame("approval.requested", 17, { request_id: "r10", kind: "command", prompt: "fine" }, { turn_id: "t1" }));
    spy.mockRestore();
    expect(text(dock(m))).toContain("fine");
  });
});

describe("a hostile queued message", () => {
  it("is cut, and one that cannot be drawn costs only its own row", async () => {
    const m = mount();
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    await play(m, "queued", 0, 3);
    await settle(() => void 0);
    const items = [
      { command_id: "a", text: "fine and short" },
      { command_id: "b", text: "BOOM" },
      { command_id: "c", text: NASTY.repeat(5000) },
    ];
    await feed(m, frame("queue.updated", 20, { items }));
    spy.mockRestore();
    expect(text(q(m.el, '[data-queued="a"]'))).toContain("fine and short");
    expect(text(q(m.el, '[data-composer-failed="queued"]'))).toBe("This queued message could not be shown.");
    expect((q(m.el, '[data-queued="c"] [data-untrusted="queued"]') as HTMLElement).textContent?.length).toBeLessThanOrEqual(2000);
    expect(q(m.el, '[data-queued="c"] img')).toBeNull();
  });
});
