// The approval and question dock: an answer lands on the request that was shown, once.
import { act } from "react";
import { describe, expect, it } from "vitest";

import { answer, decisionFor, openRequest } from "./answer";
import { all, btn, click, feed, frame, mount, play, posts, q, settle, text, type Mounted } from "./harness";

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const ok = () => ({ status: 200, body: { ok: true, receipt: "accepted", command_id: "x" } });

/** A request that has been on screen for longer than the settle time (the harness sets it to 30 ms). */
const ready = () => settle(() => wait(60));

async function withApproval(settled = true): Promise<Mounted> {
  const m = mount();
  m.transport.on("/approval", ok, "POST");
  await play(m, "approval", 0, 2); // r1 (seq 8) is open: Allow, Always (standing), Deny
  if (settled) await ready();
  return m;
}

/** A press with a pointer: it goes down on the button, then the click comes (a real click has a `detail` of 1). */
function pointerPress(el: Element | undefined): Promise<void> {
  if (el === undefined) throw new Error("nothing to press");
  return settle(() => {
    el.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
  });
}

const dock = (m: Mounted) => q(m.el, "[data-dock]") as HTMLElement;
const request = (seq: number, id = "r1", extra: object = {}) => frame("approval.requested", seq, { request_id: id, kind: "tool", prompt: `Run command ${seq}?`, ...extra }, { turn_id: "t1" });

describe("the approval dock", () => {
  it("shows the request with its answers, and takes no focus", async () => {
    const m = await withApproval();
    expect(dock(m).getAttribute("data-dock")).toBe("approval");
    expect(text(dock(m))).toContain("Run `rm -rf build`?"); // every character, backticks included
    expect(text(q(dock(m), "code"))).toBe("`rm -rf build`");
    expect(all(dock(m), "button").map((b) => text(b))).toEqual(["Allow", "Always allow", "Deny"]);
    expect(dock(m).contains(document.activeElement)).toBe(false);
    expect(dock(m).querySelector("[autofocus]")).toBeNull();
  });

  it("answers Allow once, on the request shown", async () => {
    const m = await withApproval();
    await click(btn(m, "Allow"));
    expect(posts(m, "approval")).toEqual([{ request_id: "r1", decision: "allow", command_id: "id1" }]);
    expect(text(q(m.el, "[data-dock-status]"))).toBe("Answer sent. Waiting for the agent.");
    expect(all(dock(m), "button").every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
  });

  it("answers once when a button is pressed twice in the same moment, without a complaint", async () => {
    const m = await withApproval();
    const allow = btn(m, "Allow") as HTMLButtonElement;
    await settle(() => {
      allow.click();
      allow.click();
    });
    expect(posts(m, "approval")).toHaveLength(1);
    expect(q(m.el, "[data-dock-status]")?.textContent).toBe("Answer sent. Waiting for the agent.");
  });

  it("denies with the word the hub reads", async () => {
    const m = await withApproval();
    await click(btn(m, "Deny"));
    expect(posts(m, "approval")[0]).toMatchObject({ decision: "deny" });
  });

  it("needs a second press for a standing grant", async () => {
    const m = await withApproval();
    await pointerPress(btn(m, "Always allow"));
    expect(posts(m, "approval")).toHaveLength(0);
    // The first press says what the second will do, in the button and beside it.
    expect(btn(m, "Press again to always allow")).toBeDefined();
    expect(text(q(m.el, '[data-note="standing"]'))).toContain("Always allow this kind of call? Pressing again turns off later questions");
    await click(btn(m, "Cancel"));
    expect(q(m.el, '[data-note="standing"]')).toBeNull();
    await pointerPress(btn(m, "Always allow"));
    await ready();
    await pointerPress(btn(m, "Press again to always allow"));
    expect(posts(m, "approval")).toHaveLength(1);
    expect(posts(m, "approval")[0]).toMatchObject({ decision: "always" });
  });

  it("does not grant a standing permission on a double click: the second click of the gesture that armed it counts for nothing", async () => {
    const m = await withApproval();
    const always = btn(m, "Always allow") as HTMLButtonElement;
    await click(always); // the first click arms
    await click(always); // the second click of the same double click lands on the same button, now "Press again…"
    expect(posts(m, "approval")).toHaveLength(0);
    // The confirming button is visibly not ready, not a silently dead one.
    const again = btn(m, "Press again to always allow") as HTMLButtonElement;
    expect(again.disabled).toBe(true);
    expect(text(q(m.el, '[data-note="standing"]'))).toContain("One moment, then press again.");
    await ready();
    expect((btn(m, "Press again to always allow") as HTMLButtonElement).disabled).toBe(false);
    expect(posts(m, "approval")).toHaveLength(0);
  });

  it("needs the confirming press to start after the wait: a pointer that went down earlier, or a key held through it, does not confirm", async () => {
    const m = await withApproval();
    await pointerPress(btn(m, "Always allow"));
    await ready();
    const again = btn(m, "Press again to always allow") as HTMLButtonElement;
    // A click that no pointer went down on (the press began elsewhere, or before the button could be pressed).
    await settle(() => void again.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 })));
    expect(posts(m, "approval")).toHaveLength(0);
    // A key's click with no key going down on the button after the wait (a key held through it, a Space let go late).
    await click(again);
    expect(posts(m, "approval")).toHaveLength(0);
    // A repeat of a held key is kept from pressing the button.
    const repeat = new KeyboardEvent("keydown", { key: "Enter", repeat: true, bubbles: true, cancelable: true });
    act(() => void again.dispatchEvent(repeat));
    expect(repeat.defaultPrevented).toBe(true);
    // A key that goes down on it after the wait, once, is a press.
    await settle(() => {
      again.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
      again.click();
    });
    expect(posts(m, "approval")).toHaveLength(1);
    expect(posts(m, "approval")[0]).toMatchObject({ decision: "always" });
  });

  it("does not answer with a press that began before the request could be pressed", async () => {
    const m = await withApproval();
    // A click with no pointer going down on the button (the press began on what was under the dock a moment ago).
    await settle(() => void (btn(m, "Allow") as HTMLButtonElement).dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 })));
    expect(posts(m, "approval")).toHaveLength(0);
    await pointerPress(btn(m, "Allow"));
    expect(posts(m, "approval")).toHaveLength(1);
  });

  it("does not answer with a press that went down while the answers were still off, and is let go after", async () => {
    const m = await withApproval(false);
    const allow = btn(m, "Allow") as HTMLButtonElement;
    expect(allow.disabled).toBe(true);
    // A browser may report a pointer going down on a button that is off; it is no start for a press.
    act(() => void allow.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 })));
    await ready();
    await settle(() => void (btn(m, "Allow") as HTMLButtonElement).dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 })));
    expect(posts(m, "approval")).toHaveLength(0);
  });

  it("shows code spans in a request as code and changes no character: no bold, no italic, no link, no heading, no table", async () => {
    const m = mount();
    await play(m, "approval", 0, 4);
    const prompt = "Run `rm -rf build` **now**, see [docs](https://example.com) and [x](javascript:alert(1))\n# not a heading\n|a|b|\n|-|-|";
    await feed(m, request(15, "r7", { prompt }));
    const shown = q(dock(m), '[data-untrusted="prompt"]') as HTMLElement;
    expect(shown.textContent).toBe(prompt); // every character that arrived
    expect(text(q(shown, "code"))).toBe("`rm -rf build`");
    expect(q(shown, "strong, em, a, h1, h2, table")).toBeNull();
  });

  it("does not read stars in a command as italics: what is approved is what is shown", async () => {
    const m = mount();
    await play(m, "approval", 0, 4);
    await feed(m, request(15, "r7", { prompt: "Bash: rm *.log *.txt" }));
    const shown = q(dock(m), '[data-untrusted="prompt"]') as HTMLElement;
    expect(shown.textContent).toBe("Bash: rm *.log *.txt");
    expect(q(shown, "em")).toBeNull();
    expect(q(shown, "code")).toBeNull(); // no span was read out of the stars
  });

  it("names the tool when a standing grant asks to be confirmed", async () => {
    const m = mount();
    await play(m, "approval", 0, 4);
    await feed(
      m,
      frame("tool.started", 14, { call_id: "c1", name: "Bash", summary: "ls", input: { command: "ls" } }, { turn_id: "t1" }),
      request(15, "r8", { call_id: "c1", options: [{ id: "allow", label: "Allow", kind: "allow" }, { id: "always", label: "Always", kind: "allow", standing: true }] }),
    );
    await settle(() => wait(60));
    await click(btn(m, "Always allow"));
    expect(text(q(m.el, '[data-note="standing"]'))).toContain("Always allow Bash? Pressing again turns off");
  });

  it("removes the dock when the request is answered elsewhere", async () => {
    const m = await withApproval();
    await feed(m, frame("approval.resolved", 12, { request_id: "r1", source: "user", decision: "allow", by: "device-2" }, { turn_id: "t1" }));
    expect(dock(m)).toBeNull();
  });

  it("says how many more are waiting behind it", async () => {
    const m = await withApproval();
    await feed(m, request(20, "r5"));
    expect(text(dock(m))).toContain("1 more waiting");
    expect(dock(m).getAttribute("data-request-id")).toBe("r1");
  });

  it("is off, with the reason, while the connection is down", async () => {
    const m = await withApproval();
    await settle(() => m.environment.goOffline());
    expect(all(dock(m), "button").every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
    expect(text(q(m.el, "[data-dock-status]"))).toBe("You are offline. Reconnect to do this.");
    await click(btn(m, "Allow"));
    expect(posts(m, "approval")).toHaveLength(0);
  });

  it("shows the hub's refusal and lets the person try again", async () => {
    const m = await withApproval();
    m.transport.on("/approval", () => ({ status: 409, body: { error: { code: "approval_failed", message: "that request is no longer open" } } }), "POST");
    await click(btn(m, "Allow"));
    expect(text(q(m.el, "[data-dock-status]"))).toBe("that request is no longer open");
    expect((btn(m, "Allow") as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("an answer lands on the request that was shown", () => {
  it("is refused, before anything is posted, when a new request took the id by the time the press ran", async () => {
    const m = await withApproval();
    const allow = btn(m, "Allow") as HTMLButtonElement;
    // The agent restarts and asks again under the same id; the press runs before the view has drawn it.
    const session = m.store.getState().session;
    m.store.setState({ session: { ...session, pending: [request(30) as unknown as (typeof session.pending)[number]] } });
    allow.click();
    await settle();
    expect(posts(m, "approval")).toHaveLength(0);
  });

  it("is refused by answer() when the request changed, resolved, or never was", async () => {
    const m = await withApproval();
    expect(openRequest(m.store, "r1")?.seq).toBe(8);
    expect(await answer(m.store, { requestId: "r1", seq: 7 }, "allow")).toMatchObject({ ok: false, reason: "changed" });
    expect(await answer(m.store, { requestId: "nope", seq: 8 }, "allow")).toMatchObject({ ok: false, reason: "changed", message: "That request is no longer open. Nothing was sent." });
    expect(posts(m, "approval")).toHaveLength(0);
    await feed(m, frame("approval.resolved", 12, { request_id: "r1", source: "user", decision: "allow" }, { turn_id: "t1" }));
    expect(await answer(m.store, { requestId: "r1", seq: 8 }, "allow")).toMatchObject({ ok: false, reason: "changed" });
    expect(posts(m, "approval")).toHaveLength(0);
  });

  it("answers the latest request when one id is open twice", async () => {
    const m = await withApproval();
    const session = m.store.getState().session;
    const older = session.pending[0] as (typeof session.pending)[number];
    m.store.setState({ session: { ...session, pending: [older, request(30, "r1") as unknown as typeof older] } });
    expect(openRequest(m.store, "r1")?.seq).toBe(30);
    expect(await answer(m.store, { requestId: "r1", seq: 8 }, "allow")).toMatchObject({ ok: false, reason: "changed" });
    expect(posts(m, "approval")).toHaveLength(0);
  });

  it("takes one answer at a time for one request, whoever asks", async () => {
    const m = await withApproval();
    const first = answer(m.store, { requestId: "r1", seq: 8 }, "allow");
    const second = answer(m.store, { requestId: "r1", seq: 8 }, "allow");
    expect(await second).toMatchObject({ ok: false, reason: "busy" });
    expect(await first).toMatchObject({ ok: true });
    expect(posts(m, "approval")).toHaveLength(1);
  });

  it("keeps the answers off for a moment when a request replaces the one being read", async () => {
    const m = await withApproval();
    await feed(m, frame("approval.resolved", 12, { request_id: "r1", source: "user", decision: "allow" }, { turn_id: "t1" }), request(13, "r1", { options: [{ id: "allow", label: "Allow", kind: "allow" }, { id: "deny", label: "Deny", kind: "deny" }] }));
    expect(text(dock(m))).toContain("Run command 13?");
    expect((btn(m, "Allow") as HTMLButtonElement).disabled).toBe(true);
    await click(btn(m, "Allow"));
    expect(posts(m, "approval")).toHaveLength(0);
    await settle(() => wait(60));
    expect((btn(m, "Allow") as HTMLButtonElement).disabled).toBe(false);
    await click(btn(m, "Allow"));
    expect(posts(m, "approval")).toHaveLength(1);
  });

  it("keeps the answers off for a moment too when the request replaces one that left a moment ago", async () => {
    const m = await withApproval();
    await feed(m, frame("approval.resolved", 12, { request_id: "r1", source: "user", decision: "allow", by: "device-2" }, { turn_id: "t1" }));
    expect(dock(m)).toBeNull(); // answered on another device: nothing on screen
    await feed(m, request(13, "r1", { options: [{ id: "allow", label: "Allow", kind: "allow" }, { id: "deny", label: "Deny", kind: "deny" }] }));
    expect((btn(m, "Allow") as HTMLButtonElement).disabled).toBe(true);
    await settle(() => wait(60));
    expect((btn(m, "Allow") as HTMLButtonElement).disabled).toBe(false);
  });

  it("keeps a first request's answers off for a moment too, shown as a state and not as a dead button", async () => {
    const m = await withApproval(false);
    // The request has just appeared, right where a press may have been aimed at something else.
    expect(all(dock(m), "button").every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
    expect(text(q(m.el, "[data-dock-status]"))).toBe("A new request just appeared. Read it before you answer.");
    await click(btn(m, "Deny"));
    expect(posts(m, "approval")).toHaveLength(0);
    await ready();
    expect(all(dock(m), "button").some((b) => (b as HTMLButtonElement).disabled)).toBe(false);
  });

  it("keeps a request that was there when the page loaded off for a moment too", async () => {
    const m = mount();
    await play(m, "approval", 0, 2);
    expect((btn(m, "Allow") as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("a question", () => {
  const choice = request(16, "r2", { kind: "question" });
  const ask = (kind: "choice" | "text", options?: object[]) =>
    frame("input.requested", 16, { request_id: "r2", prompt: "Which branch?", kind, ...(options === undefined ? {} : { options }) }, { turn_id: "t1" });

  it("answers a choice by the number of the row, or by the decision word when the id is one", async () => {
    const m = mount();
    m.transport.on("/approval", ok, "POST");
    await play(m, "approval", 0, 4);
    await feed(m, ask("choice", [{ id: "main", label: "main" }, { id: "dev", label: "dev" }]));
    await ready();
    expect(dock(m).getAttribute("data-dock")).toBe("input");
    await click(btn(m, "Option 2: dev"));
    expect(posts(m, "approval")[0]).toMatchObject({ request_id: "r2", decision: "2" });
    expect(decisionFor({ id: "allow" }, 4)).toBe("allow");
    expect(decisionFor({ id: "x" }, 4)).toBe("5");
    void choice;
  });

  it("cannot be answered in words yet, says so, and can be skipped", async () => {
    const m = mount();
    m.transport.on("/approval", ok, "POST");
    await play(m, "approval", 0, 9);
    await ready();
    expect(dock(m).getAttribute("data-dock")).toBe("input");
    expect(text(q(m.el, '[data-note="text-answer"]'))).toContain("cannot send a typed answer yet");
    expect(all(dock(m), "button").map((b) => text(b))).toEqual(["Skip this question"]);
    await click(btn(m, "Skip this question"));
    expect(posts(m, "approval")[0]).toMatchObject({ request_id: "r2", decision: "deny" });
  });
});

describe("the dock's layout when an agent fills it with text", () => {
  const hostile = (id: string, extra: object = {}) => ({ id, label: `${id} ${"w".repeat(400)}`, ...extra });

  it("pins Allow, Always allow and Deny under the text, with the agent's own choices scrolling above them", async () => {
    const m = mount();
    await play(m, "approval", 0, 4);
    await feed(m, request(15, "r7", { prompt: "pick", options: [hostile("a"), hostile("b"), { id: "allow", label: "Allow", kind: "allow" }, { id: "always", label: "Always", kind: "allow", standing: true }, { id: "deny", label: "Deny", kind: "deny" }] }));
    await ready();
    const pinned = q(dock(m), "[data-dock-answers]") as HTMLElement;
    const scrolling = q(dock(m), "[data-dock-text]") as HTMLElement;
    expect(all(pinned, "button").map((b) => text(b))).toEqual(["Allow", "Always allow", "Deny"]);
    expect(all(scrolling, "button[data-option-kind='choice']")).toHaveLength(2);
    expect(scrolling.contains(pinned)).toBe(false);
    expect(scrolling.classList.contains("overflow-y-auto")).toBe(true);
    expect(pinned.classList.contains("shrink-0")).toBe(true);
    // The section shrinks to the room it is given; its own scroll area is what gives.
    expect(dock(m).classList.contains("min-h-0")).toBe(true);
    expect(scrolling.classList.contains("min-h-0")).toBe(true);
  });

  it("cuts each choice to two lines, numbered, with its whole label reachable", async () => {
    const m = mount();
    await play(m, "approval", 0, 4);
    await feed(m, request(15, "r7", { prompt: "pick", options: [hostile("a"), { id: "b", label: "short" }] }));
    await ready();
    const first = btn(m, `Option 1: a ${"w".repeat(298)}…`) as HTMLButtonElement;
    expect(first).toBeDefined();
    expect(first.querySelector(".line-clamp-2")).not.toBeNull();
    expect(first.title).toBe(`a ${"w".repeat(298)}…`);
    expect(text(first).startsWith("1.")).toBe(true);
    // The labels in full, for the ones a button cuts.
    await click(btn(m, "Options in full"));
    expect(text(q(dock(m), '[data-untrusted="options"]'))).toContain(`1. a ${"w".repeat(298)}…`);
  });

  it("marks the agent's own choices so that none looks like one of the answers, whatever its label says", async () => {
    const m = mount();
    await play(m, "approval", 0, 4);
    await feed(m, request(15, "r7", { prompt: "pick", options: [{ id: "x1", label: "Allow" }, { id: "x2", label: "Deny" }, { id: "allow", label: "Allow", kind: "allow" }, { id: "deny", label: "Deny", kind: "deny" }] }));
    await ready();
    const fake = all(dock(m), "button[data-option-kind='choice']");
    expect(fake.map((b) => b.getAttribute("aria-label"))).toEqual(["Option 1: Allow", "Option 2: Deny"]);
    for (const b of fake) {
      expect(b.classList.contains("border-dashed")).toBe(true);
      expect(text(b)).toMatch(/^\d\.(Allow|Deny)$/);
    }
    const real = all(dock(m), "button[data-option-kind='fixed']");
    expect(real.map((b) => text(b))).toEqual(["Allow", "Deny"]);
    for (const b of real) expect(b.classList.contains("border-dashed")).toBe(false);
    expect(fake.every((b) => !(q(dock(m), "[data-dock-answers]") as HTMLElement).contains(b))).toBe(true);
  });

  it("keeps a decision as short as it looks when the hub gives it a paragraph for a label", async () => {
    const m = mount();
    await play(m, "approval", 0, 4);
    await feed(m, request(15, "r7", { prompt: "go?", options: [{ id: "allow", label: `Allow ${"and more ".repeat(60)}`, kind: "allow" }, { id: "deny", label: `Deny ${"and more ".repeat(60)}`, kind: "deny" }] }));
    await ready();
    expect(all(q(dock(m), "[data-dock-answers]") as HTMLElement, "button").map((b) => text(b))).toEqual(["Allow", "Deny"]);
  });
});
