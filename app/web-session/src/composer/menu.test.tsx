// The menus' key rules: the key that opens a menu, or a repeat of it, chooses nothing, and nothing that sends,
// stops, steers or removes is where the focus starts.
import { act } from "react";
import { describe, expect, it } from "vitest";

import { btn, click, feed, frame, mount, play, posts, q, settle, type Mounted } from "./harness";

const key = (type: "keydown" | "keyup", init: KeyboardEventInit): KeyboardEvent => new KeyboardEvent(type, { bubbles: true, cancelable: true, ...init });

async function withQueue(): Promise<Mounted> {
  const m = mount();
  m.transport.on("/queue", () => ({ status: 200, body: { ok: true } }), "POST");
  await play(m, "queued", 0, 3);
  await feed(m, frame("approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow" }, { turn_id: "t1" }), frame("session.state", 14, { state: "running" }), frame("session.caps", 40, { caps: { steer: true, plan_mode: false, rollback: false, fork: false, stop_task: false, options: [] } }));
  await click(q(m.el, "[data-tray] button[aria-controls]"));
  return m;
}

describe("a menu that holds only a steer (a queued message's)", () => {
  it("opens with the focus on the menu, not on the steer, so a held or a second Enter does not steer", async () => {
    const m = await withQueue();
    const more = btn(m, "More for queued message") as HTMLButtonElement;
    more.focus();
    // Enter goes down on the button; the button's click (no pointer) opens the menu while the key is still down.
    act(() => void more.dispatchEvent(key("keydown", { key: "Enter" })));
    await settle(() => void more.click());
    const menu = q(m.el, '[role="menu"]') as HTMLElement;
    expect(menu).not.toBeNull();
    expect(document.activeElement).toBe(menu);
    const item = q(m.el, '[data-menu-item="steer"]') as HTMLButtonElement;
    // The key repeats: a repeat is kept from pressing anything.
    const repeat = key("keydown", { key: "Enter", repeat: true });
    act(() => void (document.activeElement as HTMLElement).dispatchEvent(repeat));
    expect(repeat.defaultPrevented).toBe(true);
    // Its click, if one came (no pointer), is ignored while the opening key is still down.
    await settle(() => void item.click());
    expect(posts(m, "queue")).toHaveLength(0);
    // The key is let go; a press after that is a press again.
    act(() => void document.dispatchEvent(key("keyup", { key: "Enter" })));
    await settle(() => void item.click());
    expect(posts(m, "queue")).toEqual([{ client_id: "q1", op: "steer", command_id: "id1" }]);
  });

  it("is chosen by a pointer even while a key is down", async () => {
    const m = await withQueue();
    const more = btn(m, "More for queued message") as HTMLButtonElement;
    act(() => void more.dispatchEvent(key("keydown", { key: "Enter" })));
    await settle(() => void more.click());
    await settle(() => void (q(m.el, '[data-menu-item="steer"]') as HTMLElement).dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 })));
    expect(posts(m, "queue")).toHaveLength(1);
  });

  it("moves the focus onto the item with the arrow keys", async () => {
    const m = await withQueue();
    await click(btn(m, "More for queued message"));
    const menu = q(m.el, '[role="menu"]') as HTMLElement;
    expect(document.activeElement).toBe(menu);
    await settle(() => void menu.dispatchEvent(key("keydown", { key: "ArrowDown" })));
    expect(document.activeElement).toBe(q(m.el, '[data-menu-item="steer"]'));
  });
});

describe("the send menu", () => {
  it("opens on Queue, the safe item, and a held Enter on it queues nothing", async () => {
    const m = mount({ props: {} });
    m.transport.on("/message", () => ({ status: 202, body: { receipt: "queued" } }), "POST");
    await play(m, "streaming", 0, 3);
    const box = q(m.el, 'textarea[aria-label="Message"]') as HTMLTextAreaElement;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(box, "hello");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const primary = btn(m, "Queue message") as HTMLButtonElement;
    primary.focus();
    await settle(() => void primary.dispatchEvent(key("keydown", { key: "ArrowUp" })));
    expect(document.activeElement).toBe(q(m.el, '[data-menu-item="queue"]'));
    // An Enter held from before the menu opened, repeating on Queue, sends nothing.
    const repeat = key("keydown", { key: "Enter", repeat: true });
    act(() => void (document.activeElement as HTMLElement).dispatchEvent(repeat));
    expect(repeat.defaultPrevented).toBe(true);
    expect(posts(m, "message")).toHaveLength(0);
  });
});
