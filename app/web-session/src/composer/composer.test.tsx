// The message box: one press sends once, and the draft is never lost.
import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import { all, box, btn, click, feed, frame, idle, key, KEY, mount, play, posts, q, settle, snapshotWith, text, typeText } from "./harness";
import { readDraft, writeDraft } from "./draft";
import { SAVE_AFTER_MS } from "./useDraft";

const sent = (m: ReturnType<typeof mount>) => posts(m, "message");
const hold = () => vi.fn(() => new Promise<never>(() => {}));
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe("sending", () => {
  it("posts the message once under one id, clears the box and the kept draft, and keeps the focus", async () => {
    const m = mount();
    await idle(m);
    document.body.append(m.el);
    typeText(m, "Please fix the build");
    await click(btn(m, "Send message"));
    expect(sent(m)).toHaveLength(1);
    expect(sent(m)[0]).toMatchObject({ text: "Please fix the build", mode: "queue", command_id: "id1", client_id: "id1" });
    expect(box(m).value).toBe("");
    expect(readDraft(KEY).text).toBe("");
    expect(window.localStorage.length).toBe(0);
    expect(document.activeElement).toBe(box(m));
  });

  it("sends once when the button is pressed twice in the same moment, without a complaint", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "double");
    const send = btn(m, "Send message") as HTMLButtonElement;
    await settle(() => {
      send.click();
      send.click();
    });
    expect(sent(m)).toHaveLength(1);
    expect(m.store.getState().outbox.entries).toHaveLength(1);
    expect(q(m.el, '[data-composer-reason="error"]')).toBeNull();
  });

  it("sends once on Enter, and a held Enter does not send again or type a blank line", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "enter");
    const first = key(m, { key: "Enter", keyCode: 13 });
    expect(first.defaultPrevented).toBe(true);
    await settle();
    typeText(m, "next draft");
    const held = key(m, { key: "Enter", keyCode: 13, repeat: true });
    expect(held.defaultPrevented).toBe(true);
    await settle();
    expect(sent(m)).toHaveLength(1);
    expect(box(m).value).toBe("next draft");
  });

  it("leaves Shift+Enter to the box for a new line", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "a");
    const event = key(m, { key: "Enter", keyCode: 13, shiftKey: true });
    expect(event.defaultPrevented).toBe(false);
    expect(sent(m)).toHaveLength(0);
  });

  it("does not send on the Enter that ends an input method composition", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "にほん");
    // Chrome and Firefox: the key press says it is part of a composition.
    expect(key(m, { key: "Enter", keyCode: 13, isComposing: true }).defaultPrevented).toBe(false);
    // Safari: the composition has ended, and the key press says keyCode 229.
    expect(key(m, { key: "Enter", keyCode: 229 }).defaultPrevented).toBe(false);
    // The box tracks the composition itself too.
    act(() => void box(m).dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })));
    expect(key(m, { key: "Enter", keyCode: 13 }).defaultPrevented).toBe(false);
    await settle();
    expect(sent(m)).toHaveLength(0);
    act(() => void box(m).dispatchEvent(new CompositionEvent("compositionend", { bubbles: true })));
    key(m, { key: "Enter", keyCode: 13 });
    await settle();
    expect(sent(m)).toHaveLength(1);
  });

  it("sends once when the form is submitted again by a re-render's replay (strict mode) or by a held key", async () => {
    const m = mount({ strict: true });
    await idle(m);
    const spy = hold();
    m.store.setState({ send: spy });
    typeText(m, "strict");
    const form = q(m.el, "form") as HTMLFormElement;
    await settle(() => {
      form.requestSubmit();
      form.requestSubmit();
    });
    await feed(m, frame("ping", undefined, { ts: 1, head: 1 })); // a re-render while the send is out
    await settle(() => form.requestSubmit());
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("refuses a press made while the same message is on its way from another instance of the box", async () => {
    const m = mount();
    await idle(m);
    const spy = hold();
    m.store.setState({ send: spy });
    typeText(m, "remount");
    await click(btn(m, "Send message"));
    m.remount(); // the box is drawn again, as when the view remounts, and finds the draft kept
    expect(box(m).value).toBe("remount");
    await click(btn(m, "Send message"));
    expect(spy).toHaveBeenCalledTimes(1);
    expect(text(q(m.el, '[data-composer-reason="error"]'))).toBe("Still sending your last message.");
  });

  it("locks the box while the message is being saved, with the draft in it", async () => {
    const m = mount();
    await idle(m);
    m.store.setState({ send: hold() });
    typeText(m, "saving");
    await click(btn(m, "Send message"));
    expect(box(m).readOnly).toBe(true);
    expect(box(m).value).toBe("saving");
    expect((btn(m, "Send message") as HTMLButtonElement).disabled).toBe(true);
  });

  it("does not send while the outbox is loading, and keeps the draft", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "early");
    act(() => m.store.setState({ outbox: { ...m.store.getState().outbox, loading: true } }));
    expect((btn(m, "Send message") as HTMLButtonElement).disabled).toBe(true);
    expect(text(q(m.el, '[data-composer-reason="blocked"]'))).toBe("Loading the sends kept on this device.");
    // The keyboard and the form bypass the button: the outbox itself refuses.
    key(m, { key: "Enter", keyCode: 13 });
    await settle(() => void (q(m.el, "form") as HTMLFormElement).requestSubmit());
    expect(sent(m)).toHaveLength(0);
    expect(m.store.getState().outbox.entries).toHaveLength(0);
    expect(box(m).value).toBe("early");
  });
});

describe("the draft", () => {
  it("stays in the box, in storage and on screen when the send is refused", async () => {
    const m = mount();
    await idle(m);
    m.store.setState({ send: () => Promise.resolve({ ok: false, reason: "gone", message: "This session no longer exists." } as never) });
    typeText(m, "do not lose me");
    await click(btn(m, "Send message"));
    expect(box(m).value).toBe("do not lose me");
    expect(text(q(m.el, '[data-composer-reason="error"]'))).toBe("This session no longer exists.");
    await wait(SAVE_AFTER_MS + 50);
    expect(readDraft(KEY).text).toBe("do not lose me");
    expect(box(m).readOnly).toBe(false);
  });

  it("stays when the store throws", async () => {
    const m = mount();
    await idle(m);
    m.store.setState({ send: () => Promise.reject(new Error("storage exploded")) });
    typeText(m, "still here");
    await click(btn(m, "Send message"));
    expect(box(m).value).toBe("still here");
    expect(text(q(m.el, '[data-composer-reason="error"]'))).toBe("storage exploded");
  });

  it("is written soon after typing, and comes back after a reload", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "half a thought");
    await wait(SAVE_AFTER_MS + 50);
    expect(window.localStorage.getItem(`swe.sv.draft.v1:${KEY}`)).toContain("half a thought");
    const again = mount(); // a new page: a new store and a new box, the same browser storage
    await idle(again);
    expect(box(again).value).toBe("half a thought");
  });

  it("is written when the box goes away, before the pause is over", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "just typed");
    act(() => void window.dispatchEvent(new Event("pagehide")));
    expect(readDraft(KEY).text).toBe("just typed");
  });

  it("is not erased by a send that settles after a newer draft was saved by a later box", async () => {
    const m = mount();
    await idle(m);
    let settleSend: (r: unknown) => void = () => {};
    m.store.setState({ send: () => new Promise((resolve) => (settleSend = resolve)) as never });
    typeText(m, "first");
    await click(btn(m, "Send message"));
    m.remount(); // the first box goes; a second one reads the draft that is still kept
    expect(box(m).value).toBe("first");
    typeText(m, "second, typed later");
    act(() => void window.dispatchEvent(new Event("pagehide")));
    expect(readDraft(KEY).text).toBe("second, typed later");
    await settle(() => settleSend({ ok: true, entry: {} })); // the first send is taken by the outbox only now
    expect(readDraft(KEY).text).toBe("second, typed later");
  });

  it("is written when the box is taken off the page before the pause is over", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "typed just now");
    act(() => void m.el.ownerDocument.defaultView?.dispatchEvent(new Event("noop")));
    m.remount(); // unmounts the old box at once
    expect(readDraft(KEY).text).toBe("typed just now");
  });

  it("is still there after the connection drops and comes back", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "keep this across a reconnect");
    await settle(() => m.environment.goOffline());
    await settle(() => m.environment.goOnline());
    await settle(() => m.timers.advance(60_000));
    expect(box(m).value).toBe("keep this across a reconnect");
  });

  it("says when it cannot be kept on this device", async () => {
    const m = mount();
    await idle(m);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    typeText(m, "no room");
    act(() => void window.dispatchEvent(new Event("pagehide")));
    await settle();
    expect(text(q(m.el, "[data-draft-note]"))).toContain("will not survive a reload");
    vi.restoreAllMocks();
  });

  it("is gone from storage after a send even when the stored copy was older than the box", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "older");
    await wait(SAVE_AFTER_MS + 50); // what the pause timer had written so far
    typeText(m, "newer words"); // not written yet
    await click(btn(m, "Send message"));
    expect(readDraft(KEY).text).toBe("");
    await wait(SAVE_AFTER_MS + 50);
    expect(readDraft(KEY).text).toBe("");
  });
});

const menuItem = (m: ReturnType<typeof mount>, id: string) => q(m.el, `[data-menu-item="${id}"]`) as HTMLButtonElement | null;
const menuOpen = (m: ReturnType<typeof mount>) => q(m.el, '[role="menu"]');
const rightClick = (target: Element) => settle(() => void target.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));

describe("while a reply is in progress", () => {
  it("offers Stop and Queue as icons; nothing of the kind when idle", async () => {
    const idleOne = mount();
    await idle(idleOne);
    expect(btn(idleOne, "Stop the reply")).toBeUndefined();
    expect(btn(idleOne, "Send message")).toBeDefined();
    const m = mount();
    await play(m, "streaming", 0, 3);
    expect(btn(m, "Stop the reply")).toBeDefined();
    expect(btn(m, "Queue message")).toBeDefined();
    expect(btn(m, "Send message")).toBeUndefined();
    expect(box(m).placeholder).toBe("Queue a message");
    expect(box(idleOne).placeholder).toBe("Ask anything");
  });

  it("draws no word in the bar: every button is a picture with a name and a tooltip", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    const form = q(m.el, "form[aria-label='Composer']") as HTMLElement;
    const buttons = all(form, "button") as HTMLButtonElement[];
    expect(buttons.length).toBeGreaterThanOrEqual(3);
    for (const button of buttons) {
      expect(button.textContent).toBe("");
      expect(button.getAttribute("aria-label")).toBeTruthy();
      expect(button.title).toBeTruthy();
      expect(button.querySelector("svg")).not.toBeNull();
    }
    // Send and Queue are different pictures.
    const idleOne = mount();
    await idle(idleOne);
    expect(btn(idleOne, "Send message")?.innerHTML).not.toBe(btn(m, "Queue message")?.innerHTML);
  });

  it("queues with a press on the main button", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    typeText(m, "after");
    await click(btn(m, "Queue message"));
    expect(sent(m)).toHaveLength(1);
    expect(sent(m)[0]).toMatchObject({ text: "after", mode: "queue" });
    expect(menuOpen(m)).toBeNull();
  });

  it("opens a menu with Queue and Send now on a right click, Queue first and in focus", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    typeText(m, "now");
    await rightClick(btn(m, "Queue message") as Element);
    expect(menuOpen(m)).not.toBeNull();
    expect(all(m.el, '[role="menuitem"]').map((i) => i.getAttribute("data-menu-item"))).toEqual(["queue", "steer"]);
    expect(document.activeElement).toBe(menuItem(m, "queue"));
    expect(text(menuItem(m, "steer"))).toContain("Send now");
    expect(text(menuItem(m, "steer"))).toContain("Sends this message ahead of the queue. Some agents stop the current reply to do it.");
    expect(sent(m)).toHaveLength(0); // the menu opening is not a send
    await click(menuItem(m, "steer"));
    expect(sent(m)).toHaveLength(1);
    expect(sent(m)[0]).toMatchObject({ text: "now", mode: "steer" });
    expect(menuOpen(m)).toBeNull();
  });

  it("opens the same menu from the keyboard, and Escape closes it with the focus back on the button", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    typeText(m, "keys");
    const main = btn(m, "Queue message") as HTMLButtonElement;
    main.focus();
    await settle(() => void main.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true, cancelable: true })));
    expect(menuOpen(m)).not.toBeNull();
    expect(document.activeElement).toBe(menuItem(m, "queue"));
    await settle(() => void menuItem(m, "queue")?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })));
    expect(document.activeElement).toBe(menuItem(m, "steer"));
    await settle(() => void menuItem(m, "steer")?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(menuOpen(m)).toBeNull();
    await settle();
    expect(document.activeElement).toBe(main);
    expect(sent(m)).toHaveLength(0);
  });

  it("opens the menu on a hold, and the release of that hold does not also queue the message", async () => {
    const m = mount({ props: { holdMs: 20 } });
    await play(m, "streaming", 0, 3);
    typeText(m, "held");
    const main = btn(m, "Queue message") as HTMLButtonElement;
    act(() => void main.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 })));
    await wait(60);
    expect(menuOpen(m)).not.toBeNull();
    act(() => void main.dispatchEvent(new MouseEvent("pointerup", { bubbles: true })));
    await click(main); // the click that follows the release
    expect(sent(m)).toHaveLength(0);
    await wait(120);
    // A press after that is a press again.
    await click(menuItem(m, "queue"));
    expect(sent(m)).toHaveLength(1);
  });

  it("does not open the menu on a short press", async () => {
    const m = mount({ props: { holdMs: 60 } });
    await play(m, "streaming", 0, 3);
    typeText(m, "short");
    const main = btn(m, "Queue message") as HTMLButtonElement;
    act(() => void main.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 })));
    act(() => void main.dispatchEvent(new MouseEvent("pointerup", { bubbles: true })));
    await click(main);
    await wait(100);
    expect(menuOpen(m)).toBeNull();
    expect(sent(m)).toHaveLength(1);
  });

  it("has no menu while idle: a hold or a right click does nothing special", async () => {
    const m = mount({ props: { holdMs: 20 } });
    await idle(m);
    typeText(m, "idle");
    const main = btn(m, "Send message") as HTMLButtonElement;
    act(() => void main.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 })));
    await wait(60);
    const context = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    await settle(() => void main.dispatchEvent(context));
    expect(menuOpen(m)).toBeNull();
    expect(context.defaultPrevented).toBe(false); // the browser's own menu is left alone
  });

  it("never makes the stopping item the one the menu opens on, and disables it, with the reason, on an agent that cannot steer", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    await feed(m, frame("session.caps", 40, { caps: { steer: false, plan_mode: false, rollback: false, fork: false, stop_task: false, options: [] } }));
    typeText(m, "hello");
    await rightClick(btn(m, "Queue message") as Element);
    expect(document.activeElement).toBe(menuItem(m, "queue"));
    expect(menuItem(m, "steer")?.getAttribute("aria-disabled")).toBe("true");
    expect(text(menuItem(m, "steer"))).toContain("This agent cannot be steered.");
    await click(menuItem(m, "steer"));
    expect(sent(m)).toHaveLength(0);
    expect(text(q(m.el, "[data-composer-reason]"))).toContain("cannot take a message while it is replying");
  });

  it("closes the menu when the reply ends under it", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    typeText(m, "x");
    await rightClick(btn(m, "Queue message") as Element);
    expect(menuOpen(m)).not.toBeNull();
    const turn = m.store.getState().session.openTurn ?? "";
    await feed(m, frame("turn.ended", 30, { status: "completed" }, { turn_id: turn }), frame("session.state", 31, { state: "idle" }));
    expect(menuOpen(m)).toBeNull();
  });

  it("stops once on a double press", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    m.transport.on("/control", () => ({ status: 200, body: { receipt: "accepted" } }), "POST");
    const stop = btn(m, "Stop the reply") as HTMLButtonElement;
    await settle(() => {
      stop.click();
      stop.click();
    });
    expect(posts(m, "control")).toHaveLength(1);
    expect(posts(m, "control")[0]).toMatchObject({ op: "interrupt" });
    expect(btn(m, "Stop the reply")?.title).toBe("Stopping");
  });

  it("disables Stop, with the reason, when the connection is down", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    await settle(() => m.environment.goOffline());
    expect((btn(m, "Stop the reply") as HTMLButtonElement).disabled).toBe(true);
    expect(btn(m, "Stop the reply")?.title).toBe("You are offline. Reconnect to do this.");
    await click(btn(m, "Stop the reply"));
    expect(posts(m, "control")).toHaveLength(0);
  });
});

describe("the attach menu", () => {
  it("opens on the plus with the ways this browser offers, and Escape closes it", async () => {
    const m = mount();
    await idle(m);
    await click(btn(m, "Attach"));
    expect(all(m.el, '[role="menuitem"]').map((i) => text(i))).toEqual(["Choose files"]); // no photo library on a desktop browser
    expect(document.activeElement).toBe(menuItem(m, "files"));
    await settle(() => void menuItem(m, "files")?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(menuOpen(m)).toBeNull();
  });

  it("offers the photo library where there is a touch screen, and opens the picker for images", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("coarse"), media: query, addEventListener() {}, removeEventListener() {} }));
    const m = mount();
    await idle(m);
    await click(btn(m, "Attach"));
    expect(all(m.el, '[role="menuitem"]').map((i) => text(i))).toEqual(["Choose files", "Photo library"]);
    const input = q(m.el, 'input[type="file"]') as HTMLInputElement;
    const opened = vi.spyOn(input, "click").mockImplementation(() => {});
    await click(menuItem(m, "photos"));
    expect(input.accept).toBe("image/*");
    expect(opened).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});

describe("offline, storage and read-only", () => {
  it("accepts a message while offline and says in the timeline it waits", async () => {
    const m = mount();
    await idle(m);
    await settle(() => m.environment.goOffline());
    typeText(m, "typed offline");
    await click(btn(m, "Send message"));
    expect(sent(m)).toHaveLength(0);
    const view = m.store.getState().sends[0];
    expect(view?.shown).toBe("waiting");
    expect(view?.reason).toBe("Waiting: you are offline. It will be sent when you are back.");
    expect(box(m).value).toBe("");
  });

  it("shows the outbox's note when storage will not survive a reload", async () => {
    const m = mount();
    await idle(m);
    expect(text(q(m.el, "[data-outbox-note]"))).toBe("no database in tests.");
  });

  it("is replaced by a bar with the reason when the session cannot be driven", async () => {
    const m = mount();
    await settle(() => void m.transport.last.send(...snapshotWith("default", { row: { ...(JSON.parse("{}") as object), key: KEY, control: "read_only", control_reason: "Started in a terminal." } })));
    expect(q(m.el, "textarea")).toBeNull();
    expect(text(q(m.el, '[data-composer="read-only"]'))).toBe("Started in a terminal.");
  });
});

describe("attachments", () => {
  const fileInput = (m: ReturnType<typeof mount>) => q(m.el, 'input[type="file"]') as HTMLInputElement;
  const choose = async (m: ReturnType<typeof mount>, ...names: string[]) => {
    const files = names.map((name) => new File(["abc"], name, { type: "text/plain" }));
    Object.defineProperty(fileInput(m), "files", { value: files, configurable: true });
    await settle(() => void fileInput(m).dispatchEvent(new Event("change", { bubbles: true })));
    await settle();
  };

  it("stages a file, shows it, and sends its ref with the message", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ref: "1-a-notes.txt", name: "notes.txt", size: 3 }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const m = mount();
    await idle(m);
    await choose(m, "notes.txt");
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0]?.[0]).toBe("/v1/sessions/claude%3Asession-1/attachments?name=notes.txt");
    expect(calls[0]?.[1].method).toBe("POST");
    expect(text(q(m.el, '[data-file="1-a-notes.txt"]'))).toBe("notes.txt");
    await click(btn(m, "Send message"));
    expect(sent(m)[0]).toMatchObject({ text: "", attachments: ["1-a-notes.txt"] });
    expect(all(m.el, "[data-file]")).toHaveLength(0);
    vi.unstubAllGlobals();
  });

  it("says why an upload failed, and does not stop a message without it", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { message: "unsupported file type .exe" } }), { status: 422 })));
    const m = mount();
    await idle(m);
    await choose(m, "tool.exe");
    expect(text(q(m.el, '[data-upload="failed"]'))).toContain("unsupported file type .exe");
    typeText(m, "no file then");
    expect((btn(m, "Send message") as HTMLButtonElement).disabled).toBe(false);
    vi.unstubAllGlobals();
  });

  it("takes at most ten files, however many are picked at once", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      return new Response(JSON.stringify({ ref: `1-a-f${calls.length}.txt`, name: `f${calls.length}.txt`, size: 3 }), { status: 200 });
    }));
    const m = mount();
    await idle(m);
    await choose(m, ...Array.from({ length: 13 }, (_, i) => `f${i}.txt`));
    expect(calls).toHaveLength(10);
    expect(all(m.el, "[data-file]")).toHaveLength(10);
    expect(text(q(m.el, '[data-composer-reason="error"]'))).toBe("A message takes at most 10 attachments.");
    vi.unstubAllGlobals();
  });

  it("holds the send back until an upload has finished", async () => {
    let finish: (r: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (finish = resolve))));
    const m = mount();
    await idle(m);
    typeText(m, "with a file");
    await choose(m, "slow.txt");
    expect((btn(m, "Send message") as HTMLButtonElement).disabled).toBe(true);
    expect(text(q(m.el, '[data-composer-reason="blocked"]'))).toBe("Waiting for the attachment to upload.");
    await settle(() => finish(new Response(JSON.stringify({ ref: "1-a-slow.txt", name: "slow.txt", size: 3 }), { status: 200 })));
    expect((btn(m, "Send message") as HTMLButtonElement).disabled).toBe(false);
    vi.unstubAllGlobals();
  });

  it("keeps staged files in the draft across a reload", async () => {
    writeDraft(KEY, { text: "with files", files: [{ ref: "1-a-x.txt", name: "x.txt", size: 3 }] });
    const m = mount();
    await idle(m);
    expect(text(q(m.el, '[data-file="1-a-x.txt"]'))).toBe("x.txt");
  });
});
