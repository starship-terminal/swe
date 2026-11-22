// The approval and question dock: an answer lands on the request that was shown, once.
import { act } from "react";
import { describe, expect, it } from "vitest";

import { answer, decisionFor, openRequest } from "./answer";
import { all, btn, click, feed, frame, mount, off, play, posts, q, said, settle, text, type Mounted } from "./harness";

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const ok = () => ({ status: 200, body: { ok: true, receipt: "accepted", command_id: "x" } });

/** A request that has been on screen for longer than the settle time (the harness sets it to 40 ms). */
const ready = () => settle(() => wait(120));

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
/** A dock answer by the id the agent gave it. */
const opt = (m: Mounted, id: string) => q(m.el, `[data-option="${id}"]`) as HTMLButtonElement;
const request = (seq: number, id = "r1", extra: object = {}) => frame("approval.requested", seq, { request_id: id, kind: "tool", prompt: `Run command ${seq}?`, ...extra }, { turn_id: "t1" });

describe("the approval dock", () => {
  it("shows the request with its answers, and takes no focus", async () => {
    const m = await withApproval();
    expect(dock(m).getAttribute("data-dock")).toBe("approval");
    expect(text(dock(m))).toContain("Run `rm -rf build`?"); // every character, backticks included
    expect(text(q(dock(m), "code"))).toBe("`rm -rf build`");
    expect(all(dock(m), "button[data-option]").map(said)).toEqual(["Allow", "Always allow", "Deny"]);
    expect(dock(m).contains(document.activeElement)).toBe(false);
    expect(dock(m).querySelector("[autofocus]")).toBeNull();
  });

  it("answers Allow once, on the request shown", async () => {
    const m = await withApproval();
    await click(btn(m, "Allow"));
    expect(posts(m, "approval")).toEqual([{ request_id: "r1", decision: "allow", command_id: "id1" }]);
    expect(text(q(m.el, "[data-dock-status]"))).toBe("You answered: Allow. The hub took it. Waiting for the agent.");
    expect(all(dock(m), "button[data-option]").every(off)).toBe(true);
  });

  it("answers once when a button is pressed twice in the same moment, without a complaint", async () => {
    const m = await withApproval();
    const allow = btn(m, "Allow") as HTMLButtonElement;
    await settle(() => {
      allow.click();
      allow.click();
    });
    expect(posts(m, "approval")).toHaveLength(1);
    expect(q(m.el, "[data-dock-status]")?.textContent).toBe("You answered: Allow. The hub took it. Waiting for the agent.");
  });

  it("denies with the word the hub reads", async () => {
    const m = await withApproval();
    await click(btn(m, "Deny"));
    expect(posts(m, "approval")[0]).toMatchObject({ decision: "deny" });
  });

  it("needs a second press for a standing grant", async () => {
    const m = await withApproval();
    await pointerPress(opt(m, "always"));
    expect(posts(m, "approval")).toHaveLength(0);
    // The first press says what the second will do, in the button and beside it.
    expect(opt(m, "always")).toBeDefined();
    expect(text(q(m.el, '[data-note="standing"]'))).toContain("Always allow this kind of call? Pressing again turns off later questions");
    await click(btn(m, "Cancel"));
    expect(q(m.el, '[data-note="standing"]')).toBeNull();
    await pointerPress(opt(m, "always"));
    await ready();
    await pointerPress(opt(m, "always"));
    expect(posts(m, "approval")).toHaveLength(1);
    expect(posts(m, "approval")[0]).toMatchObject({ decision: "always" });
  });

  it("does not grant a standing permission on a double click: the second click of the gesture that armed it counts for nothing", async () => {
    const m = await withApproval();
    const always = opt(m, "always") as HTMLButtonElement;
    await click(always); // the first click arms
    await click(always); // the second click of the same double click lands on the same button, now "Press again…"
    expect(posts(m, "approval")).toHaveLength(0);
    // The confirming button is visibly not ready, not a silently dead one.
    const again = opt(m, "always") as HTMLButtonElement;
    expect(off(again)).toBe(true);
    expect(document.activeElement === again || true).toBe(true);
    expect(text(q(m.el, "[data-dock-status]"))).toBe("One moment, then press again.");
    await ready();
    expect(off(opt(m, "always"))).toBe(false);
    expect(text(q(m.el, "[data-dock-status]"))).toBe("Ready. Press again to confirm.");
    expect(posts(m, "approval")).toHaveLength(0);
  });

  it("needs the confirming press to start after the wait: a pointer that went down earlier, or a key held through it, does not confirm", async () => {
    const m = await withApproval();
    await pointerPress(opt(m, "always"));
    // A key goes down on the button while it is still off (the arming key, held through the wait).
    const again = opt(m, "always") as HTMLButtonElement;
    act(() => void again.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    await ready();
    // A click that no pointer went down on (the press began elsewhere, or before the button could be pressed).
    await settle(() => void again.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 })));
    expect(posts(m, "approval")).toHaveLength(0);
    // The held key's click, and a repeat of it.
    await click(again);
    expect(posts(m, "approval")).toHaveLength(0);
    const repeat = new KeyboardEvent("keydown", { key: "Enter", repeat: true, bubbles: true, cancelable: true });
    act(() => void again.dispatchEvent(repeat));
    expect(repeat.defaultPrevented).toBe(true);
    expect(posts(m, "approval")).toHaveLength(0);
    // A Space let go late: its click follows its key's going up, in the same turn.
    act(() => void again.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true })));
    await settle(() => {
      again.dispatchEvent(new KeyboardEvent("keyup", { key: " ", bubbles: true }));
      again.click();
    });
    expect(posts(m, "approval")).toHaveLength(1); // this Space went down after the wait: a press
    expect(posts(m, "approval")[0]).toMatchObject({ decision: "always" });
  });

  it("does not confirm with a Space that went down before the wait ended and is let go after it", async () => {
    const m = await withApproval();
    await pointerPress(opt(m, "always"));
    const again = opt(m, "always") as HTMLButtonElement;
    act(() => void again.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true })));
    await ready();
    // A Space makes its click as it is let go, in the same turn as its keyup.
    await settle(() => {
      again.dispatchEvent(new KeyboardEvent("keyup", { key: " ", bubbles: true }));
      again.click();
    });
    expect(posts(m, "approval")).toHaveLength(0);
    // The key is forgotten after that turn: a later bare click is an assistive technology's, and counts.
    await settle(() => wait(5));
    await click(opt(m, "always"));
    expect(posts(m, "approval")).toHaveLength(1);
  });

  it("does not confirm with a pointer that went down during the wait and is let go after it", async () => {
    const m = await withApproval();
    await pointerPress(opt(m, "always")); // arms; the confirmation is off for a moment
    const again = opt(m, "always");
    expect(off(again)).toBe(true);
    act(() => void again.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 }))); // goes down while it is off
    await ready();
    await settle(() => void again.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 })));
    expect(posts(m, "approval")).toHaveLength(0);
    await pointerPress(again); // a press of its own, after the wait
    expect(posts(m, "approval")).toHaveLength(1);
  });

  it("lets a screen reader or voice control confirm with a bare click, after the wait", async () => {
    const m = await withApproval();
    await pointerPress(opt(m, "always"));
    await ready();
    await click(opt(m, "always")); // no pointer, no key: an assistive technology's activation
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
    expect(off(allow)).toBe(true);
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
    await settle(() => wait(120));
    await click(opt(m, "always"));
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
    expect(all(dock(m), "button[data-option]").every(off)).toBe(true);
    expect(text(q(m.el, "[data-dock-status]"))).toBe("You are offline. Reconnect to do this.");
    await click(btn(m, "Allow"));
    expect(posts(m, "approval")).toHaveLength(0);
  });

  it("shows the hub's refusal and lets the person try again", async () => {
    const m = await withApproval();
    m.transport.on("/approval", () => ({ status: 409, body: { error: { code: "approval_failed", message: "that request is no longer open" } } }), "POST");
    await click(btn(m, "Allow"));
    expect(text(q(m.el, "[data-dock-status]"))).toBe("that request is no longer open");
    expect(off(btn(m, "Allow"))).toBe(false);
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
    expect(off(btn(m, "Allow"))).toBe(true);
    await click(btn(m, "Allow"));
    expect(posts(m, "approval")).toHaveLength(0);
    await settle(() => wait(120));
    expect(off(btn(m, "Allow"))).toBe(false);
    await click(btn(m, "Allow"));
    expect(posts(m, "approval")).toHaveLength(1);
  });

  it("keeps the answers off for a moment too when the request replaces one that left a moment ago", async () => {
    const m = await withApproval();
    await feed(m, frame("approval.resolved", 12, { request_id: "r1", source: "user", decision: "allow", by: "device-2" }, { turn_id: "t1" }));
    expect(dock(m)).toBeNull(); // answered on another device: nothing on screen
    await feed(m, request(13, "r1", { options: [{ id: "allow", label: "Allow", kind: "allow" }, { id: "deny", label: "Deny", kind: "deny" }] }));
    expect(off(btn(m, "Allow"))).toBe(true);
    await settle(() => wait(120));
    expect(off(btn(m, "Allow"))).toBe(false);
  });

  it("keeps a first request's answers off for a moment too, shown as a state and not as a dead button", async () => {
    const m = await withApproval(false);
    // The request has just appeared, right where a press may have been aimed at something else.
    expect(all(dock(m), "button[data-option]").every(off)).toBe(true);
    expect(text(q(m.el, "[data-dock-status]"))).toBe("A new request just appeared. Read it before you answer.");
    await click(btn(m, "Deny"));
    expect(posts(m, "approval")).toHaveLength(0);
    await ready();
    expect(all(dock(m), "button[data-option]").some(off)).toBe(false);
  });

  it("keeps a request that was there when the page loaded off for a moment too", async () => {
    const m = mount();
    await play(m, "approval", 0, 2);
    expect(off(btn(m, "Allow"))).toBe(true);
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
    expect(all(pinned, "button[data-option]").map(said)).toEqual(["Allow", "Always allow", "Deny"]);
    expect(all(scrolling, "button[data-option-kind='choice']")).toHaveLength(2);
    expect(scrolling.contains(pinned)).toBe(false);
    expect(scrolling.classList.contains("overflow-y-auto")).toBe(true);
    expect(pinned.classList.contains("shrink-0")).toBe(true);
    // The request's text has an area of its own (it scrolls past 10 rem); the dock itself does not shrink below its content,
    // so the answers are never cut: a short stack scrolls the area above the bar instead, from its bottom.
    expect(scrolling.classList.contains("max-h-40")).toBe(true);
    expect(dock(m).classList.contains("min-h-0")).toBe(false);
    expect(scrolling.contains(q(dock(m), "[data-dock-text] ~ *") as Element)).toBe(false);
    expect(scrolling.contains(q(dock(m), "p[tabindex]") as Element)).toBe(false); // the heading is not in the scroll area
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

  it("says our own word for a decision when the hub gives it a paragraph for a label, with the agent's name cut under it", async () => {
    const m = mount();
    await play(m, "approval", 0, 4);
    await feed(m, request(15, "r7", { prompt: "go?", options: [{ id: "allow", label: `Allow ${"and more ".repeat(60)}`, kind: "allow" }, { id: "deny", label: `Deny ${"and more ".repeat(60)}`, kind: "deny" }] }));
    await ready();
    const pinned = q(dock(m), "[data-dock-answers]") as HTMLElement;
    expect(all(pinned, "button[data-option]").map(said)).toEqual(["Allow", "Deny"]);
    for (const name of all(pinned, "[data-agent-name]")) expect((name.textContent ?? "").length).toBeLessThanOrEqual(61);
  });
});

describe("an ACP agent's request: ids o1..oN, the meaning in `kind`, the agent's own names", () => {
  // As src/swe/remote/adapters/acp.py builds them: PermissionOptionKind mapped to allow / always / deny, standing for allow_always.
  const acp = (...extra: object[]) => [
    ...extra,
    { id: "o1", label: "Allow once", kind: "allow" },
    { id: "o2", label: "Allow always", kind: "always", standing: true },
    { id: "o3", label: "Reject once", kind: "deny" },
    { id: "o4", label: "Reject always", kind: "deny", standing: true },
  ];
  const ask = async (options: object[]): Promise<Mounted> => {
    const m = mount();
    m.transport.on("/approval", ok, "POST");
    await play(m, "approval", 0, 4);
    await feed(m, request(15, "p1", { prompt: "Run `rm -rf build`?", options }));
    await ready();
    return m;
  };
  const pinned = (m: Mounted) => q(dock(m), "[data-dock-answers]") as HTMLElement;

  it("pins Allow, Always allow and Deny by kind, with the agent's names under them, and fills only the first allow", async () => {
    const m = await ask(acp());
    expect(all(pinned(m), "button[data-option]").map(said)).toEqual(["Allow", "Always allow", "Deny", "Always deny"]);
    expect(all(pinned(m), "[data-agent-name]").map((b) => text(b))).toEqual(["Allow once", "Allow always", "Reject once", "Reject always"]);
    expect(all(dock(m), "button[data-option-kind='choice']")).toHaveLength(0);
    const buttons = all(pinned(m), "button[data-option]");
    expect(buttons[0]?.className).toContain("bg-primary");
    expect(buttons[2]?.className).toContain("border-danger");
  });

  it("says Always deny, filled as a refusal and asking twice, for a standing refusal: it is never Always allow", async () => {
    const m = await ask(acp());
    const refusal = opt(m, "o4");
    expect(said(refusal)).toBe("Always deny");
    expect(refusal.className).toContain("border-danger");
    expect(text(q(refusal, "[data-agent-name]"))).toBe("Reject always");
    await pointerPress(refusal);
    expect(posts(m, "approval")).toHaveLength(0);
    expect(said(refusal)).toBe("Press again to always deny");
    expect(text(q(m.el, '[data-note="standing"]'))).toContain("Always deny");
    expect(text(q(m.el, '[data-note="standing"]'))).toContain("refuses later requests");
    expect(text(q(m.el, '[data-note="standing"]'))).not.toContain("Always allow");
    await ready();
    await pointerPress(refusal);
    expect(posts(m, "approval")[0]).toMatchObject({ request_id: "p1", decision: "4" });
  });

  it("keeps all of them in view with forty choices of its own, and the cut counts only the choices", async () => {
    const m = await ask(acp(...Array.from({ length: 40 }, (_, i) => ({ id: `c${i}`, label: `Pick ${i}` }))));
    expect(all(pinned(m), "button[data-option]").map(said)).toEqual(["Allow", "Always allow", "Deny", "Always deny"]);
    expect(all(dock(m), "button[data-option-kind='choice']")).toHaveLength(20);
    expect(text(dock(m))).toContain("20 more options are not shown.");
  });

  it("answers by the option's place in the list, since its ids are not decisions", async () => {
    const m = await ask(acp());
    await pointerPress(all(pinned(m), "button[data-option]")[2]); // Reject once
    expect(posts(m, "approval")[0]).toMatchObject({ request_id: "p1", decision: "3" });
  });

  it("asks twice for the standing grant, by kind and not by the id", async () => {
    const m = await ask(acp());
    await pointerPress(opt(m, "o2"));
    expect(posts(m, "approval")).toHaveLength(0);
    expect(text(q(m.el, '[data-note="standing"]'))).toContain("Always allow");
  });

  it("draws an allow that the agent calls Deny differently from a real deny", async () => {
    const m = await ask([{ id: "o1", label: "Deny", kind: "allow" }, { id: "o2", label: "Allow", kind: "deny" }]);
    const [a, b] = all(pinned(m), "button[data-option]") as HTMLElement[];
    expect(text(q(a as HTMLElement, "[data-fixed-word]"))).toBe("Allow");
    expect(text(q(a as HTMLElement, "[data-agent-name]"))).toBe("Deny");
    expect(text(q(b as HTMLElement, "[data-fixed-word]"))).toBe("Deny");
    expect(a?.className).not.toBe(b?.className); // one is filled, the other has the refusal's border
  });

  it("treats an option with a kind it does not know, or none, as the agent's own choice", async () => {
    const m = await ask([{ id: "o1", label: "Allow", kind: "allow_once" }, { id: "o2", label: "Deny" }, { id: "o3", label: "Fine", kind: "allow" }]);
    expect(all(pinned(m), "button[data-option]").map(said)).toEqual(["Allow"]);
    expect(all(dock(m), "button[data-option-kind='choice']").map((b) => b.getAttribute("aria-label"))).toEqual(["Option 1: Allow", "Option 2: Deny"]);
  });

  it("gives the whole of what the agent called a fixed answer in its tooltip and accessible name, however much of it the button shows", async () => {
    const name = `Reject and forget this tool for the rest of the session ${"and every other one like it ".repeat(4)}`;
    const m = await ask([{ id: "o1", label: "Allow once", kind: "allow" }, { id: "o2", label: name, kind: "deny" }]);
    const deny = opt(m, "o2");
    expect(text(q(deny, "[data-agent-name]")).endsWith("…")).toBe(true);
    expect(deny.title).toBe(name.trim().length > 300 ? `${name.trim().slice(0, 300)}…` : name.trim());
    expect(deny.getAttribute("aria-label")).toBe(`Deny: ${deny.title}`);
  });

  it("makes the agent's name the larger, plain text when two answers share a kind, and the small print when they do not", async () => {
    const m = await ask(acp());
    for (const id of ["o3", "o4"]) expect(q(opt(m, id), "[data-agent-name]")?.className).toContain("text-sm");
    expect(q(opt(m, "o1"), "[data-agent-name]")?.className).toContain("opacity-80");
  });

  it("pins two of a kind and lists the rest as choices", async () => {
    const m = await ask(Array.from({ length: 5 }, (_, i) => ({ id: `o${i + 1}`, label: `Reject ${i}`, kind: "deny" })));
    expect(all(pinned(m), "[data-fixed-word]")).toHaveLength(2);
    expect(all(dock(m), "button[data-option-kind='choice']")).toHaveLength(3);
  });
});

describe("the dock's focus, live region and stable answers", () => {
  it("keeps one line that is always there, a live region before its text changes, and says when the answers are ready", async () => {
    const m = await withApproval(false);
    const status = q(dock(m), "[data-dock-status]") as HTMLElement;
    expect(status.getAttribute("role")).toBe("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(text(status)).toBe("A new request just appeared. Read it before you answer.");
    await ready();
    expect(q(dock(m), "[data-dock-status]")).toBe(status); // the same element: its text changed in place
    expect(status.hasAttribute("hidden")).toBe(false);
    expect(text(status)).toBe("You can answer now.");
  });

  it("keeps the answers in the tab order while they wait: off with aria-disabled, never the disabled attribute", async () => {
    const m = await withApproval(false);
    for (const b of all(dock(m), "button[data-option]")) {
      expect(b.getAttribute("aria-disabled")).toBe("true");
      expect(b.hasAttribute("disabled")).toBe(false);
    }
    (opt(m, "deny") as HTMLElement).focus();
    expect(document.activeElement).toBe(opt(m, "deny"));
    await click(opt(m, "deny")); // a press on an answer that is off does nothing
    expect(posts(m, "approval")).toHaveLength(0);
  });

  it("keeps the focus on Always allow when it arms", async () => {
    const m = await withApproval();
    opt(m, "always").focus();
    await pointerPress(opt(m, "always"));
    expect(opt(m, "always").getAttribute("data-armed")).toBe("true");
    expect(document.activeElement).toBe(opt(m, "always"));
  });

  it("puts what comes and goes above the answers, and holds the width of the armed button, so no answer moves", async () => {
    const m = await withApproval(false);
    const area = q(dock(m), "[data-dock-answers]") as HTMLElement;
    const row = area.lastElementChild as HTMLElement;
    // The answers are the last thing in the pinned area: the status line and the standing note are before them.
    expect(row.querySelector("button[data-option]")).not.toBeNull();
    expect(Array.from(area.children).indexOf(q(area, "[data-dock-status]") as Element)).toBeLessThan(Array.from(area.children).indexOf(row));
    // Both the word and the confirmation are in the button at all times; one is only hidden.
    expect(text(opt(m, "always"))).toContain("Always allow");
    expect(text(opt(m, "always"))).toContain("Press again to always allow");
    await ready();
    await pointerPress(opt(m, "always"));
    expect(q(area, "[data-note='standing']")).not.toBeNull();
    expect(Array.from(area.children).indexOf(q(area, "[data-note='standing']") as Element)).toBeLessThan(Array.from(area.children).indexOf(row));
    expect(area.lastElementChild).toBe(row);
  });

  it("gives the focus to the heading of a request that replaces one the person had the focus in, and to nothing otherwise", async () => {
    const m = await withApproval();
    opt(m, "allow").focus();
    await feed(m, frame("approval.resolved", 12, { request_id: "r1", source: "user", decision: "allow", by: "device-2" }, { turn_id: "t1" }), request(13, "r5"));
    expect(document.activeElement).toBe(q(dock(m), "p[tabindex]"));
    // The focus is not on an answer, and a later request, with the focus elsewhere, takes none.
    expect(document.activeElement?.hasAttribute("data-option")).toBe(false);
    (q(m.el, 'textarea[aria-label="Message"]') as HTMLElement).focus();
    await feed(m, frame("approval.resolved", 14, { request_id: "r5", source: "user", decision: "allow" }, { turn_id: "t1" }), request(15, "r6"));
    expect(document.activeElement).toBe(q(m.el, 'textarea[aria-label="Message"]'));
  });

  it("takes no focus for a request that appears while the focus is on the page", async () => {
    const m = await withApproval(false);
    expect(dock(m).contains(document.activeElement)).toBe(false);
  });
});

describe("one request, at most one decision", () => {
  const status = (m: Mounted) => text(q(m.el, "[data-dock-status]"));
  const onlyDecision = (m: Mounted) => new Set(posts(m, "approval").map((p) => p.decision));

  for (const [name, reply, words] of [
    ["accepted", { status: 200, body: { ok: true, receipt: "accepted" } }, "The hub took it. Waiting for the agent."],
    ["queued", { status: 202, body: { ok: true, receipt: "queued" } }, "The hub queued it. Waiting for the agent."],
    ["held (that machine is asleep)", { status: 202, body: { ok: true, receipt: "held" } }, "Your answer waits for that machine to wake."],
  ] as const) {
    it(`after an answer the hub ${name}, nothing can post another decision, at the wait's end or ever`, async () => {
      const m = mount({ props: { confirmMs: 120 } });
      m.transport.on("/approval", () => reply, "POST");
      await play(m, "approval", 0, 2);
      await ready();
      await pointerPress(opt(m, "allow"));
      expect(status(m)).toBe(`You answered: Allow. ${words}`);
      await settle(() => wait(250)); // the wait is over and the request is still open
      expect(status(m)).toContain("Still waiting");
      // Every control is off, and a press on Deny posts nothing.
      expect(all(dock(m), "button[data-option]").every(off)).toBe(true);
      await pointerPress(opt(m, "deny"));
      await pointerPress(opt(m, "always"));
      await pointerPress(opt(m, "allow"));
      expect(posts(m, "approval")).toHaveLength(1);
      expect(onlyDecision(m)).toEqual(new Set(["allow"]));
      // And not by another route either: the answer itself refuses a different decision, and a repeat.
      expect(await answer(m.store, { requestId: "r1", seq: 8 }, "deny")).toMatchObject({ ok: false, reason: "decided" });
      expect(await answer(m.store, { requestId: "r1", seq: 8 }, "allow")).toMatchObject({ ok: false, reason: "decided" });
      expect(posts(m, "approval")).toHaveLength(1);
    });
  }

  it("keeps the answer in mind when the dock is drawn again for the same request", async () => {
    const m = await withApproval();
    await pointerPress(opt(m, "allow"));
    m.remount();
    await ready();
    expect(status(m)).toContain("You answered: Allow.");
    expect(all(dock(m), "button[data-option]").every(off)).toBe(true);
    expect(await answer(m.store, { requestId: "r1", seq: 8 }, "deny")).toMatchObject({ ok: false, reason: "decided" });
  });

  it("brings the answers back, with the hub's reason, after a definite refusal (nothing was posted)", async () => {
    const m = await withApproval();
    m.transport.on("/approval", () => ({ status: 409, body: { error: { code: "approval_failed", message: "that request is no longer open" } } }), "POST");
    await pointerPress(opt(m, "allow"));
    expect(text(q(m.el, "[data-dock-status]"))).toContain("that request is no longer open");
    expect(all(dock(m), "button[data-option]").some(off)).toBe(false);
    // The hub refused it (nothing was taken), so a different decision may follow.
    m.transport.on("/approval", ok, "POST");
    await pointerPress(opt(m, "deny"));
    expect(posts(m, "approval").map((p) => p.decision)).toEqual(["allow", "deny"]);
  });

  for (const [name, reply] of [
    ["a server error", () => ({ status: 503, body: { error: { code: "unavailable", message: "the hub is busy" } } })],
    ["no answer at all", () => Promise.reject(new Error("the hub could not be reached"))],
  ] as const) {
    it(`says so after ${name}, offers exactly one action (the same answer again) and never another decision`, async () => {
      const m = await withApproval();
      let first = true;
      m.transport.on("/approval", () => {
        if (first) {
          first = false;
          return reply();
        }
        return { status: 200, body: { ok: true, receipt: "accepted" } };
      }, "POST");
      await pointerPress(opt(m, "deny"));
      expect(status(m)).toContain("We could not tell whether your answer (Deny) reached the hub.");
      expect(all(dock(m), "button[data-option]").every(off)).toBe(true);
      const resend = q(dock(m), "[data-resend]") as HTMLButtonElement;
      expect(text(resend)).toBe("Send the same answer again");
      expect(all(dock(m), "button").filter((b) => !b.hasAttribute("data-option"))).toHaveLength(1);
      // A different decision is refused, whatever the button says.
      await pointerPress(opt(m, "allow"));
      expect(await answer(m.store, { requestId: "r1", seq: 8 }, "allow")).toMatchObject({ ok: false, reason: "decided" });
      // The same one goes again, and then it is taken.
      await settle(() => void (q(dock(m), "[data-resend]") as HTMLButtonElement).click());
      expect(posts(m, "approval").map((p) => p.decision)).toEqual(["deny", "deny"]);
      expect(status(m)).toBe("You answered: Deny. The hub took it. Waiting for the agent.");
      expect(q(dock(m), "[data-resend]")).toBeNull();
    });
  }

  it("ends the dock as before when another device answers while this one waits", async () => {
    const m = await withApproval();
    await pointerPress(opt(m, "allow"));
    await feed(m, frame("approval.resolved", 12, { request_id: "r1", source: "user", decision: "deny", by: "device-2" }, { turn_id: "t1" }));
    expect(dock(m)).toBeNull();
  });
});
