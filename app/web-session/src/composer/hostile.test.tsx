// What an agent can put in a request or a queued message must cost no more than a few hundred nodes, and
// never change its letters, take the page down, or take down more than its own part.
import { describe, expect, it, vi } from "vitest";

import { all, btn, click, feed, frame, mount, play, q, said, settle, text, type Mounted } from "./harness";

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
  await settle(() => new Promise((resolve) => setTimeout(resolve, 120))); // past the settle time
}

describe("a hostile request", () => {
  it("draws a megabyte of prompt as one cut text, reading marks in its first part only, and makes no markup from it", async () => {
    const m = mount();
    const prompt = NASTY.repeat(15_000);
    await ask(m, { prompt });
    const shown = q(dock(m), '[data-untrusted="prompt"]') as HTMLElement;
    // Every character that arrived (up to the cut) is drawn: nothing is read away.
    expect(shown.textContent).toBe(prompt.slice(0, 6000));
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
    expect(Math.max(...buttons.map((b) => (b.textContent ?? "").length))).toBeLessThanOrEqual(312);
    expect(text(dock(m))).toContain("980 more options are not shown.");
    expect(all(dock(m), "*").length).toBeLessThan(200);
  });

  it("keeps Allow, Always allow and Deny in view however many choices come before them", async () => {
    const m = mount();
    m.transport.on("/approval", () => ({ status: 200, body: { receipt: "accepted" } }), "POST");
    const rows = Array.from({ length: 40 }, (_, i) => ({ id: `row-${i}`, label: `Row ${i}` }));
    const dupes = Array.from({ length: 300 }, () => ({ id: "deny", label: "Deny" }));
    await ask(m, { prompt: "pick", options: [...rows, { id: "allow", label: "Allow", kind: "allow" }, { id: "always", label: "Always", kind: "allow", standing: true }, { id: "deny", label: "Deny", kind: "deny" }, ...dupes] });
    const pinned = q(dock(m), "[data-dock-answers]") as HTMLElement;
    expect(all(pinned, "button[data-option]").map(said)).toEqual(["Allow", "Always allow", "Deny"]);
    // Only the agent's own choices are cut: 20 shown, the rest (40 rows and 300 more Deny) counted.
    expect(all(dock(m), "button[data-option-kind='choice']")).toHaveLength(20);
    expect(text(dock(m))).toContain("320 more options are not shown.");
    // Deny sends the word, by the first Deny, not by its place in the list.
    await click(btn(m, "Deny"));
    expect(m.transport.posts("/approval")[0]).toMatchObject({ request_id: "r9", decision: "deny" });
  });

  it("answers an option by its place in the full list, even with the list cut", async () => {
    const m = mount();
    m.transport.on("/approval", () => ({ status: 200, body: { receipt: "accepted" } }), "POST");
    await ask(m, { prompt: "pick", options: Array.from({ length: 30 }, (_, i) => ({ id: `row-${i}`, label: `row ${i}` })) });
    await click(btn(m, "Option 20: row 19"));
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

  it("does not write a tool's input out while Details is closed", async () => {
    const m = mount();
    let written = 0;
    const input = { command: "ls", toJSON() { written++; return { command: "ls" }; } };
    await ask(m, { prompt: "run it", call_id: "call-9" }, { call_id: "call-9", name: "Bash", summary: "ls", input });
    expect(written).toBe(0);
    await click(btn(m, "Details: Bash"));
    expect(written).toBeGreaterThan(0);
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
    await click(q(m.el, "[data-tray] button[aria-controls]"));
    spy.mockRestore();
    expect(text(q(m.el, '[data-queued="a"]'))).toContain("fine and short");
    expect(text(q(m.el, '[data-composer-failed="queued"]'))).toBe("This queued message could not be shown.");
    expect((q(m.el, '[data-queued="c"] [data-untrusted="queued"]') as HTMLElement).textContent?.length).toBeLessThanOrEqual(2000);
    expect(q(m.el, '[data-queued="c"] img')).toBeNull();
  });

  it("draws a message again once the queue changed and it is fine", async () => {
    const m = mount();
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    await play(m, "queued", 0, 3);
    await settle(() => void 0);
    await feed(m, frame("queue.updated", 20, { items: [{ command_id: "b", text: "BOOM" }] }));
    await click(q(m.el, "[data-tray] button[aria-controls]"));
    expect(q(m.el, '[data-composer-failed="queued"]')).not.toBeNull();
    // The same message, edited on another device, is fine now: its row is tried again.
    await feed(m, frame("queue.updated", 21, { items: [{ command_id: "b", text: "fixed" }] }));
    spy.mockRestore();
    expect(q(m.el, '[data-composer-failed="queued"]')).toBeNull();
    expect(text(q(m.el, '[data-queued="b"]'))).toContain("fixed");
  });

  it("draws the whole tray again once the queue changed, after a failure that took it down", async () => {
    const m = mount();
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    await play(m, "queued", 0, 3);
    await settle(() => void 0);
    // The tray's own count line throws on a queue that is not a list.
    const session = m.store.getState().session;
    m.store.setState({ session: { ...session, queue: { length: 1, map: undefined, some: undefined } as never } });
    await settle(() => void 0);
    expect(q(m.el, '[data-composer-failed="tray"]')).not.toBeNull();
    m.store.setState({ session: { ...session, queue: [{ command_id: "z", text: "back again" }] } });
    await settle(() => void 0);
    spy.mockRestore();
    expect(q(m.el, '[data-composer-failed="tray"]')).toBeNull();
    expect(q(m.el, "[data-tray]")).not.toBeNull();
  });
});
