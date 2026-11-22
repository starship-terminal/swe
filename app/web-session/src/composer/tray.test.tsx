// The queued tray: edit, remove and send now, as far as the hub lets a queue be changed.
import { describe, expect, it } from "vitest";

import { all, box, btn, click, feed, frame, mount, play, posts, q, settle, text, typeText, type Mounted } from "./harness";

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const ok = () => ({ status: 200, body: { ok: true, receipt: "accepted" } });
const queue = (seq: number, ...items: { id: string; text: string; attachments?: string[] }[]) =>
  frame("queue.updated", seq, { items: items.map((i) => ({ command_id: i.id, text: i.text, by: "device-1", mode: "queue", ...(i.attachments ? { attachments: i.attachments } : {}) })) });

async function withQueue(settleMs?: number): Promise<Mounted> {
  const m = mount(settleMs === undefined ? {} : { props: { settleMs } });
  m.transport.on("/queue", ok, "POST");
  await play(m, "queued", 0, 3); // q1 "then lint it" queued behind an open approval
  await feed(m, frame("approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow" }, { turn_id: "t1" }), frame("session.state", 14, { state: "running" }));
  return openTray(m);
}
const tray = (m: Mounted) => q(m.el, "[data-tray]") as HTMLElement;
/** The strip above the bar opens the list upward; the list is not made until then. */
const strip = (m: Mounted) => q(tray(m), "button[aria-controls]") as HTMLButtonElement;
async function openTray(m: Mounted): Promise<Mounted> {
  await click(strip(m));
  return m;
}

describe("the queued tray", () => {
  it("lists what the hub holds, and is absent when nothing is queued", async () => {
    const m = await withQueue();
    expect(text(strip(m))).toBe("Queued: 1 message");
    expect(strip(m).getAttribute("aria-expanded")).toBe("true");
    expect(text(q(m.el, '[data-queued="q1"]'))).toContain("then lint it");
    await feed(m, queue(20));
    expect(q(m.el, "[data-tray]")).toBeNull();
  });

  it("takes a message out in two presses, the second after a wait and a press of its own: a double click removes nothing", async () => {
    const m = await withQueue(300); // a wait long enough that a slow run cannot end it before the checks
    const remove = btn(m, "Remove queued message") as HTMLButtonElement;
    await click(remove); // the first press arms
    expect(posts(m, "queue")).toHaveLength(0);
    const confirm = btn(m, "Press again to remove") as HTMLButtonElement;
    // The second click of a double click lands on the new button: it is visibly not ready, and does nothing.
    expect(confirm.getAttribute("aria-disabled")).toBe("true");
    expect(document.activeElement).toBe(confirm); // the focus went with the button that was pressed
    expect(text(q(m.el, "[data-remove-confirm]"))).toContain("One moment, then press again.");
    await click(confirm);
    expect(posts(m, "queue")).toHaveLength(0);
    await settle(() => wait(340));
    expect((btn(m, "Press again to remove") as HTMLButtonElement).getAttribute("aria-disabled")).toBeNull();
    // A click whose pointer did not go down on it is not a press of it.
    await settle(() => void (btn(m, "Press again to remove") as HTMLButtonElement).dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 })));
    expect(posts(m, "queue")).toHaveLength(0);
    // A pointer that goes down on it after the wait is.
    await settle(() => {
      const again = btn(m, "Press again to remove") as HTMLButtonElement;
      again.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
      again.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
      again.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 2 }));
    });
    expect(posts(m, "queue")).toEqual([{ client_id: "q1", op: "cancel", command_id: "id1" }]);
  });

  it("lets the person keep a message they started to remove, with the focus back on the trash", async () => {
    const m = await withQueue();
    await click(btn(m, "Remove queued message"));
    await click(btn(m, "Keep it"));
    await settle(() => wait(5));
    expect(document.activeElement).toBe(btn(m, "Remove queued message"));
    expect(q(m.el, "[data-remove-confirm]")).toBeNull();
    expect(posts(m, "queue")).toHaveLength(0);
  });

  it("puts the focus back on More when its menu is closed with Escape", async () => {
    const m = await withQueue();
    const more = btn(m, "More for queued message") as HTMLButtonElement;
    more.focus();
    await click(more);
    const menu = q(m.el, '[role="menu"]') as HTMLElement;
    expect(document.activeElement).toBe(menu);
    await settle(() => void menu.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
    await settle(() => wait(5));
    expect(q(m.el, '[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(btn(m, "More for queued message"));
  });

  it("sends a queued message now, which stops the reply, from its menu and once", async () => {
    const m = await withQueue();
    expect(q(m.el, '[role="menu"]')).toBeNull(); // not a button of its own
    await click(btn(m, "More for queued message"));
    const now = q(m.el, '[data-menu-item="steer"]') as HTMLButtonElement;
    expect(text(now)).toContain("Send now");
    expect(text(now)).toContain("Sends this message ahead of the queue.");
    await settle(() => {
      now.click();
      now.click();
    });
    expect(posts(m, "queue")).toEqual([{ client_id: "q1", op: "steer", command_id: "id1" }]);
  });

  it("edits a message and posts the new words", async () => {
    const m = await withQueue();
    await click(btn(m, "Edit queued message"));
    const editor = q(tray(m), "textarea") as HTMLTextAreaElement;
    expect(editor.value).toBe("then lint it");
    expect((btn(m, "Save the edit") as HTMLButtonElement).disabled).toBe(true); // nothing changed yet
    await settle(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(editor, "then lint and format it");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(btn(m, "Save the edit"));
    expect(posts(m, "queue")).toEqual([{ client_id: "q1", op: "edit", command_id: "id1", text: "then lint and format it" }]);
    expect(q(tray(m), "textarea")).toBeNull();
  });

  it("keeps the editor, with the hub's reason, when a save is refused", async () => {
    const m = await withQueue();
    m.transport.on("/queue", () => ({ status: 409, body: { error: { code: "no_such", message: "that message is no longer queued" } } }), "POST");
    await click(btn(m, "Edit queued message"));
    const editor = q(tray(m), "textarea") as HTMLTextAreaElement;
    await settle(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(editor, "changed");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(btn(m, "Save the edit"));
    expect(text(q(tray(m), '[role="alert"]'))).toBe("that message is no longer queued");
    expect((q(tray(m), "textarea") as HTMLTextAreaElement).value).toBe("changed");
  });

  it("does not drop an edit when its message leaves the queue: the words are offered back", async () => {
    const m = await withQueue();
    await click(btn(m, "Edit queued message"));
    const editor = q(tray(m), "textarea") as HTMLTextAreaElement;
    await settle(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(editor, "then lint it, strictly");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await feed(m, queue(20)); // it was sent meanwhile
    const orphan = q(m.el, "[data-orphan]") as HTMLElement;
    expect(text(orphan)).toContain("left the queue before your edit was saved");
    expect(text(orphan)).toContain("then lint it, strictly");
    await click(btn(m, "Move to the message box"));
    expect(box(m).value).toBe("then lint it, strictly");
    expect(q(m.el, "[data-orphan]")).toBeNull();
  });

  it("adds an edit it has to give back after what is already in the box", async () => {
    const m = await withQueue();
    typeText(m, "typing");
    await click(btn(m, "Edit queued message"));
    const editor = q(tray(m), "textarea") as HTMLTextAreaElement;
    await settle(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(editor, "edited");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await feed(m, queue(20));
    await click(btn(m, "Move to the message box"));
    expect(box(m).value).toBe("typing\n\nedited");
  });

  it("is off, with the reason, while the connection is down", async () => {
    const m = await withQueue();
    await settle(() => m.environment.goOffline());
    for (const label of ["Edit queued message", "More for queued message", "Remove queued message"]) {
      expect((btn(m, label) as HTMLButtonElement).disabled).toBe(true);
    }
    expect(text(q(m.el, '[data-tray-note="refusal"]'))).toBe("You are offline. Reconnect to do this.");
  });

  it("disables send-now, and says why, on an agent that cannot steer", async () => {
    const m = await withQueue();
    await feed(m, frame("session.caps", 40, { caps: { steer: false, plan_mode: false, rollback: false, fork: false, stop_task: false, options: [] } }));
    await click(btn(m, "More for queued message"));
    const now = q(m.el, '[data-menu-item="steer"]') as HTMLButtonElement;
    expect(now.getAttribute("aria-disabled")).toBe("true");
    expect(text(now)).toContain("This agent cannot be steered.");
    await click(now);
    expect(posts(m, "queue")).toHaveLength(0);
    // The one sentence about it is the bar's, not the tray's.
    expect(q(m.el, '[data-tray-note="no-steer"]')).toBeNull();
    expect((btn(m, "Remove queued message") as HTMLButtonElement).disabled).toBe(false);
  });

  it("says that an agent cannot send ahead of the queue once, not in the bar and again in the tray", async () => {
    const m = await withQueue();
    await feed(m, frame("session.caps", 40, { caps: { steer: false, plan_mode: false, rollback: false, fork: false, stop_task: false, options: [] } }));
    typeText(m, "and bump the version");
    const said = (text(m.el).match(/cannot send a message ahead of the queue/g) ?? []).length;
    expect(said).toBe(1);
  });

  it("draws at most twenty, and says how many more there are", async () => {
    const m = await withQueue();
    await feed(m, queue(20, ...Array.from({ length: 25 }, (_, i) => ({ id: `q${i}`, text: `message ${i}` }))));
    expect(all(tray(m), "[data-queued]")).toHaveLength(20);
    expect(text(tray(m))).toContain("5 more queued messages not shown.");
  });

  it("is a strip above the bar that opens upward into a list with its own height and scroll", async () => {
    // Layout contract: jsdom has no layout, so this reads the classes and the order; the screens show the result.
    const m = await withQueue();
    const list = q(tray(m), "ul") as HTMLElement;
    expect(list.classList.contains("max-h-44")).toBe(true);
    expect(list.classList.contains("overflow-y-auto")).toBe(true);
    const above = tray(m).compareDocumentPosition(q(m.el, "form[aria-label='Composer']") as HTMLElement);
    expect(above & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy(); // the tray is before the bar
    // The strip is below its list, so the list grows upward from it.
    expect((q(tray(m), "#sv-queue-list") as HTMLElement).compareDocumentPosition(strip(m)) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const stack = q(m.el, "[data-composer-above]") as HTMLElement;
    expect(stack.classList.contains("overflow-y-auto")).toBe(true);
    expect((stack.parentElement as HTMLElement).classList.contains("max-h-1/2")).toBe(true);
  });

  it("starts closed, as a strip, and opens on a press", async () => {
    const m = mount();
    m.transport.on("/queue", ok, "POST");
    await play(m, "queued", 0, 3);
    expect(strip(m).getAttribute("aria-expanded")).toBe("false");
    expect(q(tray(m), "[data-queued]")).toBeNull(); // not made until opened
    await click(strip(m));
    expect(q(tray(m), "[data-queued]")).not.toBeNull();
  });

  it("has Edit and Remove as icons with names, and no visible words", async () => {
    const m = await withQueue();
    for (const label of ["Edit queued message", "Remove queued message", "More for queued message"]) {
      const button = btn(m, label) as HTMLButtonElement;
      expect(button.querySelector("svg")).not.toBeNull();
      expect(button.textContent).toBe("");
      expect(button.title).toBe(label);
    }
  });

  it("offers no reorder: the hub has none", async () => {
    const m = await withQueue();
    expect(all(tray(m), "button").map((b) => b.getAttribute("aria-label") ?? text(b))).toEqual(["Edit queued message", "Remove queued message", "More for queued message", "Queued: 1 message"]);
  });
});

describe("an edit while another device changes the message", () => {
  async function editing(): Promise<Mounted> {
    const m = await withQueue();
    await click(btn(m, "Edit queued message"));
    const editor = q(tray(m), "textarea") as HTMLTextAreaElement;
    await settle(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(editor, "then lint it twice");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    return m;
  }

  it("says the message changed, shows the new text, and does not save over it unseen", async () => {
    const m = await editing();
    await feed(m, queue(20, { id: "q1", text: "then lint and test it" }));
    const conflict = q(m.el, "[data-edit-conflict]") as HTMLElement;
    expect(text(conflict)).toContain("changed on another device while you were editing");
    expect(text(conflict)).toContain("then lint and test it");
    expect((btn(m, "Save the edit") as HTMLButtonElement).disabled).toBe(true);
    await click(btn(m, "Save the edit"));
    expect(posts(m, "queue")).toHaveLength(0);
  });

  it("lets the person keep their own words, which then save", async () => {
    const m = await editing();
    await feed(m, queue(20, { id: "q1", text: "then lint and test it" }));
    await click(btn(m, "Keep my words"));
    expect(q(m.el, "[data-edit-conflict]")).toBeNull();
    expect((q(tray(m), "textarea") as HTMLTextAreaElement).value).toBe("then lint it twice");
    await click(btn(m, "Save the edit"));
    expect(posts(m, "queue")).toEqual([{ client_id: "q1", op: "edit", command_id: "id1", text: "then lint it twice" }]);
  });

  it("lets the person take the new text instead", async () => {
    const m = await editing();
    await feed(m, queue(20, { id: "q1", text: "then lint and test it" }));
    await click(btn(m, "Use the new text"));
    expect((q(tray(m), "textarea") as HTMLTextAreaElement).value).toBe("then lint and test it");
    expect(q(m.el, "[data-edit-conflict]")).toBeNull();
  });

  it("keeps the person's words, and says only what it knows when the message left the queue meanwhile", async () => {
    const m = await editing();
    await feed(m, queue(20));
    const orphan = q(m.el, "[data-orphan]") as HTMLElement;
    expect(orphan.getAttribute("data-orphan-sent")).toBe("false");
    expect(text(orphan)).toContain("left the queue before your edit was saved: it is being sent, or it was taken out on another device");
    expect(text(orphan)).toContain("then lint it twice");
  });

  it("says it was sent once the timeline shows the message the same command made", async () => {
    const m = await editing();
    await feed(m, queue(20), frame("message.completed", 21, { id: "u9", kind: "user", text: "then lint it" }, { turn_id: "t2", command_id: "q1" }));
    const orphan = q(m.el, "[data-orphan]") as HTMLElement;
    expect(orphan.getAttribute("data-orphan-sent")).toBe("true");
    expect(text(orphan)).toContain("This message was sent before your edit was saved, so your edit did not change it");
    expect(text(orphan)).toContain("then lint it twice");
  });
});
