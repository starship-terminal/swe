// The queued tray: edit, remove and send now, as far as the hub lets a queue be changed.
import { describe, expect, it } from "vitest";

import { all, box, btn, click, feed, frame, mount, play, posts, q, settle, text, typeText, type Mounted } from "./harness";

const ok = () => ({ status: 200, body: { ok: true, receipt: "accepted" } });
const queue = (seq: number, ...items: { id: string; text: string; attachments?: string[] }[]) =>
  frame("queue.updated", seq, { items: items.map((i) => ({ command_id: i.id, text: i.text, by: "device-1", mode: "queue", ...(i.attachments ? { attachments: i.attachments } : {}) })) });

async function withQueue(): Promise<Mounted> {
  const m = mount();
  m.transport.on("/queue", ok, "POST");
  await play(m, "queued", 0, 3); // q1 "then lint it" queued behind an open approval
  await feed(m, frame("approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow" }, { turn_id: "t1" }), frame("session.state", 14, { state: "running" }));
  return m;
}
const tray = (m: Mounted) => q(m.el, "[data-tray]") as HTMLElement;

describe("the queued tray", () => {
  it("lists what the hub holds, and is absent when nothing is queued", async () => {
    const m = await withQueue();
    expect(text(tray(m))).toContain("Queued: 1 message, sent when the reply ends");
    expect(text(q(m.el, '[data-queued="q1"]'))).toContain("then lint it");
    await feed(m, queue(20));
    expect(q(m.el, "[data-tray]")).toBeNull();
  });

  it("removes a message once on a double press", async () => {
    const m = await withQueue();
    const remove = btn(m, "Remove queued message") as HTMLButtonElement;
    await settle(() => {
      remove.click();
      remove.click();
    });
    expect(posts(m, "queue")).toEqual([{ client_id: "q1", op: "cancel", command_id: "id1" }]);
  });

  it("sends a queued message now, which stops the reply, once", async () => {
    const m = await withQueue();
    const now = btn(m, "Stop the reply and send this now") as HTMLButtonElement;
    expect(now.title).toBe("Stops the current reply and sends this message now.");
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
    expect((btn(m, "Save") as HTMLButtonElement).disabled).toBe(true); // nothing changed yet
    await settle(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(editor, "then lint and format it");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(btn(m, "Save"));
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
    await click(btn(m, "Save"));
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
    for (const label of ["Edit queued message", "Stop the reply and send this now", "Remove queued message"]) {
      expect((btn(m, label) as HTMLButtonElement).disabled).toBe(true);
    }
    expect(text(q(m.el, '[data-tray-note="refusal"]'))).toBe("You are offline. Reconnect to do this.");
  });

  it("disables send-now, and says why, on an agent that cannot steer", async () => {
    const m = await withQueue();
    await feed(m, frame("session.caps", 40, { caps: { steer: false, plan_mode: false, rollback: false, fork: false, stop_task: false, options: [] } }));
    expect((btn(m, "Stop the reply and send this now") as HTMLButtonElement).disabled).toBe(true);
    expect(text(q(m.el, '[data-tray-note="no-steer"]'))).toContain("cannot stop a reply to send a message sooner");
    expect((btn(m, "Remove queued message") as HTMLButtonElement).disabled).toBe(false);
  });

  it("draws at most twenty, and says how many more there are", async () => {
    const m = await withQueue();
    await feed(m, queue(20, ...Array.from({ length: 25 }, (_, i) => ({ id: `q${i}`, text: `message ${i}` }))));
    expect(all(tray(m), "[data-queued]")).toHaveLength(20);
    expect(text(tray(m))).toContain("5 more queued messages not shown.");
  });

  it("offers no reorder: the hub has none", async () => {
    const m = await withQueue();
    expect(all(tray(m), "button").map((b) => b.getAttribute("aria-label") ?? text(b))).toEqual(["Queued: 1 message, sent when the reply ends", "Edit queued message", "Stop the reply and send this now", "Remove queued message"]);
  });
});
